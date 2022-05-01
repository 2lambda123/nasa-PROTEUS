import os
import shutil
import tempfile
import logging
from osgeo import gdal, osr

def save_as_cog(filename, scratch_dir = '.', logger = None):
    """Save (overwrite) a GeoTIFF file as a cloud-optimized GeoTIFF.

       Parameters
       ----------
       filename: str
              GeoTIFF to be saved as a cloud-optimized GeoTIFF
       scratch_dir: str (optional)
              Temporary Directory

    """
    if logger is None:
        logger = logging.getLogger('proteus')

    logger.info('COG step 1: add overviews')
    gdal_ds = gdal.Open(filename, 1)
    gdal_ds.BuildOverviews('NEAREST', [2, 4, 8, 16, 32, 64, 128], gdal.TermProgress_nocb)
    del gdal_ds  # close the dataset (Python object and pointers)
    external_overview_file = filename + '.ovr'
    if os.path.isfile(external_overview_file):
        os.path.remove(external_overview_file)

    logger.info('COG step 2: save as COG')
    temp_file = tempfile.NamedTemporaryFile(
                    dir=scratch_dir, suffix='.tif').name

    gdal_translate_options = ['TILED=YES',
                              'BLOCKXSIZE=1024',
                              'BLOCKYSIZE=1024',
                              'COMPRESS=DEFLATE',
                              'PREDICTOR=2',
                              'COPY_SRC_OVERVIEWS=YES']
    gdal.Translate(temp_file, filename,
                   creationOptions=gdal_translate_options)

    shutil.move(temp_file, filename)

    logger.info('COG step 3: validate')
    try:
        from proteus.extern.validate_cloud_optimized_geotiff import main as validate_cog
    except ModuleNotFoundError:
        logger.info('WARNING could not import module validate_cloud_optimized_geotiff')
        return

    argv = ['--full-check=yes', filename]
    validate_cog_ret = validate_cog(argv)
    if validate_cog_ret == 0:
        logger.info(f'file "{filename}" is a valid cloud optimized'
                    ' GeoTIFF')
    else:
        logger.warning(f'file "{filename}" is NOT a valid cloud'
                       f' optimized GeoTIFF!')

def get_geographic_boundaries_from_mgrs_tile(mgrs_tile_name, verbose=False):

    import mgrs
    mgrs_obj = mgrs.MGRS()
    lower_left_utm_coordinate = mgrs_obj.MGRSToUTM(mgrs_tile_name)
    utm_zone = lower_left_utm_coordinate[0]
    is_northern = lower_left_utm_coordinate[1] == 'N'
    x_min = lower_left_utm_coordinate[2]
    y_min = lower_left_utm_coordinate[3]

    # create UTM spatial reference
    utm_coordinate_system = osr.SpatialReference()
    utm_coordinate_system.SetWellKnownGeogCS("WGS84")
    utm_coordinate_system.SetUTM(utm_zone, is_northern)

    # create geographic (lat/lon) spatial reference
    wgs84_coordinate_system = osr.SpatialReference()
    wgs84_coordinate_system.SetWellKnownGeogCS("WGS84")

    # create transformation of coordinates from UTM to geographic (lat/lon)
    transformation = osr.CoordinateTransformation(utm_coordinate_system,
                                                  wgs84_coordinate_system)

    # compute boundaries
    elevation = 0
    lat_min = None
    lat_max = None
    lon_min = None
    lon_max = None

    for offset_x_multiplier in range(2):
        for offset_y_multiplier in range(2):

            x = x_min + offset_x_multiplier * 109.8 * 1000
            y = y_min + offset_y_multiplier * 109.8 * 1000
            lon, lat, z = transformation.TransformPoint(x, y, elevation)

            if verbose:
                print('')
                print('x:', x)
                print('y:', y)
                print('lon:', lon)
                print('lat:', lat)

            if lat_min is None or lat_min > lat:
                lat_min = lat
            if lat_max is None or lat_max < lat:
                lat_max = lat
            if lon_min is None or lon_min > lon:
                lon_min = lon
            if lon_max is None or lon_max < lon:
                lon_max = lon

    if verbose:
        print('')
        print('lat_min:', lat_min)
        print('lat_max:', lat_max)
        print('lon_min:', lon_min)
        print('lon_max:', lon_max)
        print('')

    return lat_min, lat_max, lon_min, lon_max

