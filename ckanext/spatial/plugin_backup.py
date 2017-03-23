import os
import re
import mimetypes
from logging import getLogger

from pylons import config



from ckan import plugins as p

from ckan.lib.helpers import json


def check_geoalchemy_requirement():
    '''Checks if a suitable geoalchemy version installed

       Checks if geoalchemy2 is present when using CKAN >= 2.3, and raises
       an ImportError otherwise so users can upgrade manually.
    '''

    msg = ('This version of ckanext-spatial requires {0}. ' +
           'Please install it by running `pip install {0}`.\n' +
           'For more details see the "Troubleshooting" section of the ' +
           'install documentation')

    if p.toolkit.check_ckan_version(min_version='2.3'):
        try:
            import geoalchemy2
        except ImportError:
            raise ImportError(msg.format('geoalchemy2'))
    else:
        try:
            import geoalchemy
        except ImportError:
            raise ImportError(msg.format('geoalchemy'))

check_geoalchemy_requirement()

log = getLogger(__name__)


def package_error_summary(error_dict):
    ''' Do some i18n stuff on the error_dict keys '''

    def prettify(field_name):
        field_name = re.sub('(?<!\w)[Uu]rl(?!\w)', 'URL',
                            field_name.replace('_', ' ').capitalize())
        return p.toolkit._(field_name.replace('_', ' '))

    summary = {}
    for key, error in error_dict.iteritems():
        if key == 'resources':
            summary[p.toolkit._('Resources')] = p.toolkit._('Package resource(s) invalid')
        elif key == 'extras':
            summary[p.toolkit._('Extras')] = p.toolkit._('Missing Value')
        elif key == 'extras_validation':
            summary[p.toolkit._('Extras')] = error[0]
        else:
            summary[p.toolkit._(prettify(key))] = error[0]
    return summary

class SpatialMetadata(p.SingletonPlugin):

    p.implements(p.IPackageController, inherit=True)
    p.implements(p.IConfigurable, inherit=True)
    p.implements(p.IConfigurer, inherit=True)
    p.implements(p.ITemplateHelpers, inherit=True)

    def configure(self, config):
        from ckanext.spatial.model.package_extent import setup as setup_model

        if not p.toolkit.asbool(config.get('ckan.spatial.testing', 'False')):
            log.debug('Setting up the spatial model')
            setup_model()

    def update_config(self, config):
        ''' Set up the resource library, public directory and
        template directory for all the spatial extensions
        '''
        p.toolkit.add_public_directory(config, 'public')
        p.toolkit.add_template_directory(config, 'templates')
        p.toolkit.add_resource('public', 'ckanext-spatial')

        # Add media types for common extensions not included in the mimetypes
        # module
        mimetypes.add_type('application/json', '.geojson')
        mimetypes.add_type('application/gml+xml', '.gml')

    def create(self, package):
        self.check_spatial_extra(package)

    def edit(self, package):
        self.check_spatial_extra(package)

    def check_spatial_extra(self,package):
        '''
        For a given package, looks at the spatial extent (as given in the
        extra "spatial" in GeoJSON format) and records it in PostGIS.
        '''
        from ckanext.spatial.lib import save_package_extent

        if not package.id:
            log.warning('Couldn\'t store spatial extent because no id was provided for the package')
            return

        # TODO: deleted extra
        for extra in package.extras_list:
            if extra.key == 'spatial':
                if extra.state == 'active' and extra.value:
                    try:
                        log.debug('Received: %r' % extra.value)
                        geometry = json.loads(extra.value)
                    except ValueError,e:
                        error_dict = {'spatial':[u'Error decoding JSON object: %s' % str(e)]}
                        raise p.toolkit.ValidationError(error_dict, error_summary=package_error_summary(error_dict))
                    except TypeError,e:
                        error_dict = {'spatial':[u'Error decoding JSON object: %s' % str(e)]}
                        raise p.toolkit.ValidationError(error_dict, error_summary=package_error_summary(error_dict))

                    try:
                        save_package_extent(package.id,geometry)

                    except ValueError,e:
                        error_dict = {'spatial':[u'Error creating geometry: %s' % str(e)]}
                        raise p.toolkit.ValidationError(error_dict, error_summary=package_error_summary(error_dict))
                    except Exception, e:
                        if bool(os.getenv('DEBUG')):
                            raise
                        error_dict = {'spatial':[u'Error: %s' % str(e)]}
                        raise p.toolkit.ValidationError(error_dict, error_summary=package_error_summary(error_dict))

                elif (extra.state == 'active' and not extra.value) or extra.state == 'deleted':
                    # Delete extent from table
                    save_package_extent(package.id,None)

                break


    def delete(self, package):
        from ckanext.spatial.lib import save_package_extent
        save_package_extent(package.id,None)

    ## ITemplateHelpers

    def get_helpers(self):
        from ckanext.spatial import helpers as spatial_helpers
        return {
                'get_reference_date' : spatial_helpers.get_reference_date,
                'get_responsible_party': spatial_helpers.get_responsible_party,
                'get_common_map_config' : spatial_helpers.get_common_map_config,
                }

class SpatialQuery(p.SingletonPlugin):

    p.implements(p.IRoutes, inherit=True)
    p.implements(p.IPackageController, inherit=True)
    p.implements(p.IConfigurable, inherit=True)

    search_backend = None

    def configure(self, config):

        self.search_backend = config.get('ckanext.spatial.search_backend', 'postgis')
        if self.search_backend != 'postgis' and not p.toolkit.check_ckan_version('2.0.1'):
            msg = 'The Solr backends for the spatial search require CKAN 2.0.1 or higher. ' + \
                  'Please upgrade CKAN or select the \'postgis\' backend.'
            raise p.toolkit.CkanVersionException(msg)

    def before_map(self, map):

        map.connect('api_spatial_query', '/api/2/search/{register:dataset|package}/geo',
            controller='ckanext.spatial.controllers.api:ApiController',
            action='spatial_query')
        return map

    def before_index(self, pkg_dict):
        import shapely

        if pkg_dict.get('extras_spatial', None) and self.search_backend in ('solr', 'solr-spatial-field'):
            try:
                geometry = json.loads(pkg_dict['extras_spatial'])
            except ValueError, e:
                log.error('Geometry not valid GeoJSON, not indexing')
                return pkg_dict

            if self.search_backend == 'solr':
                # Only bbox supported for this backend
                if not (geometry['type'] == 'Polygon'
                   and len(geometry['coordinates']) == 1
                   and len(geometry['coordinates'][0]) == 5):
                    log.error('Solr backend only supports bboxes, ignoring geometry {0}'.format(pkg_dict['extras_spatial']))
                    return pkg_dict

                coords = geometry['coordinates']
                pkg_dict['maxy'] = max(coords[0][2][1], coords[0][0][1])
                pkg_dict['miny'] = min(coords[0][2][1], coords[0][0][1])
                pkg_dict['maxx'] = max(coords[0][2][0], coords[0][0][0])
                pkg_dict['minx'] = min(coords[0][2][0], coords[0][0][0])
                pkg_dict['bbox_area'] = (pkg_dict['maxx'] - pkg_dict['minx']) * \
                                        (pkg_dict['maxy'] - pkg_dict['miny'])

            elif self.search_backend == 'solr-spatial-field':
                wkt = None

                # Check potential problems with bboxes
                if geometry['type'] == 'Polygon' \
                   and len(geometry['coordinates']) == 1 \
                   and len(geometry['coordinates'][0]) == 5:

                    # Check wrong bboxes (4 same points)
                    xs = [p[0] for p in geometry['coordinates'][0]]
                    ys = [p[1] for p in geometry['coordinates'][0]]

                    if xs.count(xs[0]) == 5 and ys.count(ys[0]) == 5:
                        wkt = 'POINT({x} {y})'.format(x=xs[0], y=ys[0])
                    else:
                        # Check if coordinates are defined counter-clockwise,
                        # otherwise we'll get wrong results from Solr
                        lr = shapely.geometry.polygon.LinearRing(geometry['coordinates'][0])
                        if not lr.is_ccw:
                            lr.coords = list(lr.coords)[::-1]
                        polygon = shapely.geometry.polygon.Polygon(lr)
                        wkt = polygon.wkt

                if not wkt:
                    shape = shapely.geometry.asShape(geometry)
                    if not shape.is_valid:
                        log.error('Wrong geometry, not indexing')
                        return pkg_dict
                    wkt = shape.wkt

                pkg_dict['spatial_geom'] = wkt


        return pkg_dict

    def before_search(self, search_params):
        from ckanext.spatial.lib import  validate_bbox
        from ckan.lib.search import SearchError

        if search_params.get('extras', None) and search_params['extras'].get('ext_bbox', None):

            bbox = validate_bbox(search_params['extras']['ext_bbox'])
            if not bbox:
                raise SearchError('Wrong bounding box provided')

            # Adjust easting values
            while (bbox['minx'] < -180):
                bbox['minx'] += 360
                bbox['maxx'] += 360
            while (bbox['minx'] > 180):
                bbox['minx'] -= 360
                bbox['maxx'] -= 360

            if self.search_backend == 'solr':
                search_params = self._params_for_solr_search(bbox, search_params)
            elif self.search_backend == 'solr-spatial-field':
                search_params = self._params_for_solr_spatial_field_search(bbox, search_params)
            elif self.search_backend == 'postgis':
                search_params = self._params_for_postgis_search(bbox, search_params)

        return search_params

    def _params_for_solr_search(self, bbox, search_params):
        '''
        This will add the following parameters to the query:

            defType - edismax (We need to define EDisMax to use bf)
            bf - {function} A boost function to influence the score (thus
                 influencing the sorting). The algorithm can be basically defined as:

                    2 * X / Q + T

                 Where X is the intersection between the query area Q and the
                 target geometry T. It gives a ratio from 0 to 1 where 0 means
                 no overlap at all and 1 a perfect fit

             fq - Adds a filter that force the value returned by the previous
                  function to be between 0 and 1, effectively applying the
                  spatial filter.

        '''

        variables =dict(
            x11=bbox['minx'],
            x12=bbox['maxx'],
            y11=bbox['miny'],
            y12=bbox['maxy'],
            x21='minx',
            x22='maxx',
            y21='miny',
            y22='maxy',
            area_search = abs(bbox['maxx'] - bbox['minx']) * abs(bbox['maxy'] - bbox['miny'])
        )

        bf = '''div(
                   mul(
                   mul(max(0, sub(min({x12},{x22}) , max({x11},{x21}))),
                       max(0, sub(min({y12},{y22}) , max({y11},{y21})))
                       ),
                   2),
                   add({area_search}, mul(sub({y22}, {y21}), sub({x22}, {x21})))
                )'''.format(**variables).replace('\n','').replace(' ','')

        search_params['fq_list'] = ['{!frange incl=false l=0 u=1}%s' % bf]

        search_params['bf'] = bf
        search_params['defType'] = 'edismax'

        return search_params

    def _params_for_solr_spatial_field_search(self, bbox, search_params):
        '''
        This will add an fq filter with the form:

            +spatial_geom:"Intersects({minx} {miny} {maxx} {maxy})

        '''
        search_params['fq_list'] = search_params.get('fq_list', [])
        search_params['fq_list'].append('+spatial_geom:"Intersects({minx} {miny} {maxx} {maxy})"'
                                     .format(minx=bbox['minx'],miny=bbox['miny'],maxx=bbox['maxx'],maxy=bbox['maxy']))

        return search_params

    def _params_for_postgis_search(self, bbox, search_params):
        from ckanext.spatial.lib import   bbox_query, bbox_query_ordered
        from ckan.lib.search import SearchError

        # Note: This will be deprecated at some point in favour of the
        # Solr 4 spatial sorting capabilities
        if search_params.get('sort') == 'spatial desc' and \
           p.toolkit.asbool(config.get('ckanext.spatial.use_postgis_sorting', 'False')):
            if search_params['q'] or search_params['fq']:
                raise SearchError('Spatial ranking cannot be mixed with other search parameters')
                # ...because it is too inefficient to use SOLR to filter
                # results and return the entire set to this class and
                # after_search do the sorting and paging.
            extents = bbox_query_ordered(bbox)
            are_no_results = not extents
            search_params['extras']['ext_rows'] = search_params['rows']
            search_params['extras']['ext_start'] = search_params['start']
            # this SOLR query needs to return no actual results since
            # they are in the wrong order anyway. We just need this SOLR
            # query to get the count and facet counts.
            rows = 0
            search_params['sort'] = None # SOLR should not sort.
            # Store the rankings of the results for this page, so for
            # after_search to construct the correctly sorted results
            rows = search_params['extras']['ext_rows'] = search_params['rows']
            start = search_params['extras']['ext_start'] = search_params['start']
            search_params['extras']['ext_spatial'] = [
                (extent.package_id, extent.spatial_ranking) \
                for extent in extents[start:start+rows]]
        else:
            extents = bbox_query(bbox)
            are_no_results = extents.count() == 0

        if are_no_results:
            # We don't need to perform the search
            search_params['abort_search'] = True
        else:
            # We'll perform the existing search but also filtering by the ids
            # of datasets within the bbox
            bbox_query_ids = [extent.package_id for extent in extents]

            q = search_params.get('q','').strip() or '""'
            new_q = '%s AND ' % q if q else ''
            new_q += '(%s)' % ' OR '.join(['id:%s' % id for id in bbox_query_ids])

            search_params['q'] = new_q

        return search_params

    def after_search(self, search_results, search_params):
        from ckan.lib.search import PackageSearchQuery

        # Note: This will be deprecated at some point in favour of the
        # Solr 4 spatial sorting capabilities

        if search_params.get('extras', {}).get('ext_spatial') and \
           p.toolkit.asbool(config.get('ckanext.spatial.use_postgis_sorting', 'False')):
            # Apply the spatial sort
            querier = PackageSearchQuery()
            pkgs = []
            for package_id, spatial_ranking in search_params['extras']['ext_spatial']:
                # get package from SOLR
                pkg = querier.get_index(package_id)['data_dict']
                pkgs.append(json.loads(pkg))
            search_results['results'] = pkgs
        return search_results

class HarvestMetadataApi(p.SingletonPlugin):
    '''
    Harvest Metadata API
    (previously called "InspireApi")

    A way for a user to view the harvested metadata XML, either as a raw file or
    styled to view in a web browser.
    '''
    p.implements(p.IRoutes)

    def before_map(self, route_map):
        controller = "ckanext.spatial.controllers.api:HarvestMetadataApiController"

        # Showing the harvest object content is an action of the default
        # harvest plugin, so just redirect there
        route_map.redirect('/api/2/rest/harvestobject/{id:.*}/xml',
            '/harvest/object/{id}',
            _redirect_code='301 Moved Permanently')

        route_map.connect('/harvest/object/{id}/original', controller=controller,
                          action='display_xml_original')

        route_map.connect('/harvest/object/{id}/html', controller=controller,
                          action='display_html')
        route_map.connect('/harvest/object/{id}/html/original', controller=controller,
                          action='display_html_original')

        # Redirect old URL to a nicer and unversioned one
        route_map.redirect('/api/2/rest/harvestobject/:id/html',
           '/harvest/object/{id}/html',
            _redirect_code='301 Moved Permanently')

        return route_map

    def after_map(self, route_map):
        return route_map
