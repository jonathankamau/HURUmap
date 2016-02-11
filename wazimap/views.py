import requests
from itertools import chain
import json

from django.conf import settings
from django.utils.safestring import SafeString
from django.utils.module_loading import import_string
from django.http import HttpResponse, Http404, HttpResponseBadRequest
from django.views.generic import View, TemplateView
from django.shortcuts import redirect

from census.views import GeographyDetailView as BaseGeographyDetailView, LocateView as BaseLocateView, render_json_to_response
from census.utils import LazyEncoder

from wazimap.geo import geo_data
from wazimap.profiles import enhance_api_data
# TODO: XXX
# TODO: move all this into wazimap.data.{utils, geo, tables, etc.}
from wazimap.data.models.tables import get_datatable, DATA_TABLES
from wazimap.data.utils import LocationNotFound
from wazimap.data.download import generate_download_bundle, supported_formats


def render_json_error(message, status_code=400):
    """ Utility method for rendering a view's data to JSON response.
    """
    result = json.dumps({'error': message}, indent=4)
    response = HttpResponse(result, content_type='application/javascript')
    response.status_code = status_code
    return response


class GeographyDetailView(BaseGeographyDetailView):
    def dispatch(self, *args, **kwargs):
        self.geo_id = self.kwargs.get('geography_id', None)

        try:
            self.geo_level, self.geo_code = self.geo_id.split('-', 1)
            self.geo = geo_data.get_geography(self.geo_code, self.geo_level)
        except (ValueError, LocationNotFound):
            raise Http404

        # check slug
        if kwargs.get('slug') or self.geo.slug:
            if kwargs['slug'] != self.geo.slug:
                kwargs['slug'] = self.geo.slug
                url = '/profiles/%s-%s-%s' % (self.geo_level, self.geo_code, self.geo.slug)
                return redirect(url, permanent=True)

        # Skip the parent class's logic completely and go back to basics
        return TemplateView.dispatch(self, *args, **kwargs)

    def get_context_data(self, *args, **kwargs):
        page_context = {}

        # load the profile
        profile_method = settings.WAZIMAP.get('profile_builder', None)
        profile_name = settings.WAZIMAP.get('default_profile', 'default')

        if not profile_method:
            raise ValueError("You must define WAZIMAP.profile_builder in settings.py")
        profile_method = import_string(profile_method)
        profile_data = profile_method(self.geo_code, self.geo_level, profile_name)

        profile_data['geography'] = self.geo.as_dict_deep()

        profile_data = enhance_api_data(profile_data)
        page_context.update(profile_data)

        profile_data_json = SafeString(json.dumps(profile_data, cls=LazyEncoder))

        page_context.update({
            'profile_data_json': profile_data_json
        })

        # is this a head-to-head view?
        page_context['head2head'] = 'h2h' in self.request.GET

        return page_context

    def get_geography(self, geo_id):
        # stub this out to prevent the subclass for calling out to CR
        pass


class GeographyJsonView(GeographyDetailView):
    """ Return geo profile data as json. """
    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        return HttpResponse(context['profile_data_json'], mimetype='application/javascript')


class PlaceSearchJson(View):
    def get(self, request, *args, **kwargs):
        if 'q' in request.GET:
            search_term = request.GET['q']
            geo_levels = request.GET.get('geolevels', None)
            return render_json_to_response(
                {'results': geo_data.get_locations(search_term, geo_levels)}
            )

        return HttpResponseBadRequest('"q" parameter is required')


class WardSearchProxy(View):

    def get(self, request, *args, **kwargs):
        try:
            resp = requests.get('http://wards.code4sa.org',
                                params={'address': request.GET['address'],
                                        'database': 'wards_2011'})
            if resp.status_code != 200:
                return HttpResponseBadRequest()
            elif resp.text.strip().startswith('{'):
                return HttpResponse(self.pad_content(request, '[]'),
                                    mimetype='application/javascript')
            return HttpResponse(self.pad_content(request, resp.text),
                                mimetype='application/javascript')
        except (KeyError, AttributeError):
            return HttpResponseBadRequest()

    def pad_content(self, request, content):
        if 'callback' in request.GET:
            return '%s(%s);' % (request.GET['callback'], content)
        return content


class LocateView(BaseLocateView):
    def get_context_data(self, *args, **kwargs):
        page_context = {}
        lat = self.request.GET.get('lat', None)
        lon = self.request.GET.get('lon', None)

        if lat and lon:
            places = geo_data.get_locations_from_coords(latitude=lat, longitude=lon)
            page_context.update({
                'location': {
                    'lat': lat,
                    'lon': lon
                },
                'places': places
            })

        return page_context


class DataAPIView(View):
    """
    View that provides an API for census table information, mimicking that
    of the Censusreporter API described at https://github.com/censusreporter/census-api#get-10datashowacs

    An example call:

    http://api.censusreporter.org/1.0/data/show/latest?table_ids=B17001&geo_ids=04000US36%2C01000US
    """

    def get(self, request, *args, **kwargs):
        try:
            self.geo_ids = request.GET.get('geo_ids', '').split(',')
            self.data_geos, self.info_geos = self.get_geos(self.geo_ids)
        except LocationNotFound as e:
            return render_json_error(e.message, 404)

        try:
            self.table_ids = request.GET.get('table_ids', '').split(',')
            self.tables = [get_datatable(t) for t in self.table_ids]
        except KeyError as e:
            return render_json_error('Unknown table: %s' % e.message, 404)

        if kwargs.get('action') == 'show':
            return self.show(request)
        if kwargs.get('action') == 'download':
            return self.download(request)

    def show(self, request):
        dataset = ', '.join(sorted(list(set(t.dataset_name for t in self.tables))))
        years = ', '.join(sorted(list(set(t.year for t in self.tables))))

        data = self.get_data(self.data_geos, self.tables)

        return render_json_to_response({
            'release': {
                'name': dataset,
                'years': years,
            },
            'tables': dict((t.id.upper(), t.as_dict()) for t in self.tables),
            'data': data,
            'geography': dict((g.geoid, g.as_dict()) for g in chain(self.data_geos, self.info_geos)),
        })

    def download(self, request):
        fmt = request.GET.get('format', 'csv')
        if fmt not in supported_formats:
            response = HttpResponse('Unspported format %s. Supported formats: %s' % (fmt, ', '.join(supported_formats.keys())))
            response.status_code = 400
            return response

        data = self.get_data(self.data_geos, self.tables)

        content, fname, mime_type = generate_download_bundle(self.tables, self.data_geos, self.geo_ids, data, fmt)

        response = HttpResponse(content, content_type=mime_type)
        response['Content-Disposition'] = 'attachment; filename="%s"' % fname

        return response

    def get_geos(self, geo_ids):
        """
        Return a tuple (data_geos, info_geos) of geo objects,
        where data_geos or geos we should get data for, and info_geos
        are geos that we only need to return geo info/metadata for.
        """
        data_geos = []
        info_geos = []

        for geo_id in geo_ids:
            # either country-KE or level|country-KE, which indicates
            # we must break country-KE into +levels+
            if '-' not in geo_id:
                raise LocationNotFound('Invalid geo id: %s' % geo_id)

            level, code = geo_id.split('-', 1)

            if '|' in level:
                # break geo down further
                split_level, level = level.split('|', 1)
                geo = geo_data.get_geography(code, level)
                info_geos.append(geo)
                try:
                    data_geos.extend(geo.split_into(split_level))
                except ValueError:
                    raise LocationNotFound('Invalid geo level: %s' % split_level)

            else:
                # normal geo
                data_geos.append(geo_data.get_geography(code, level))

        return data_geos, info_geos

    def get_data(self, geos, tables):
        data = {}

        for table in tables:
            for geo_id, table_data in table.raw_data_for_geos(geos).iteritems():
                data.setdefault(geo_id, {})[table.id.upper()] = table_data

        return data


class TableAPIView(View):
    """
    View that lists data tables.
    """

    def get(self, request, *args, **kwargs):
        return render_json_to_response([t.as_dict(columns=False) for t in DATA_TABLES.itervalues()])


class AboutView(TemplateView):
    template_name = 'about.html'


class GeographyCompareView(TemplateView):
    template_name = 'profile/head2head.html'

    def get_context_data(self, geo_id1, geo_id2):
        page_context = {
            'geo_id1': geo_id1,
            'geo_id2': geo_id2,
        }

        try:
            level, code = geo_id1.split('-', 1)
            page_context['geo1'] = geo_data.get_geography(code, level)

            level, code = geo_id2.split('-', 1)
            page_context['geo2'] = geo_data.get_geography(code, level)
        except (ValueError, LocationNotFound):
            raise Http404

        return page_context
