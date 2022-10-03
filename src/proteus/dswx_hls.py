import sys
import logging
import tempfile
import os
import glob
import numpy as np
import argparse
import yamale
import datetime
from collections import OrderedDict
from ruamel.yaml import YAML as ruamel_yaml
from osgeo.gdalconst import GDT_Float32
from osgeo import gdal, osr
from proteus.core import save_as_cog

PRODUCT_VERSION = '0.5'

landcover_mask_type = 'standard'

COMPARE_DSWX_HLS_PRODUCTS_ERROR_TOLERANCE = 1e-6

UINT8_FILL_VALUE = 255

'''
Extra margin to accomodate use of any interpolation method and other
operations taking place near the edge of the DEM
'''
DEM_MARGIN_IN_PIXELS = 50

logger = logging.getLogger('dswx_hls')

l30_v1_band_dict = {'blue': 'band02',
                    'green': 'band03',
                    'red': 'band04',
                    'nir': 'band05',
                    'swir1': 'band06',
                    'swir2': 'band07',
                    'qa': 'QA'}

s30_v1_band_dict = {'blue': 'band02',
                    'green': 'band03',
                    'red': 'band04',
                    'nir': 'band8A',
                    'swir1': 'band11',
                    'swir2': 'band12',
                    'qa': 'QA'}

l30_v2_band_dict = {'blue': 'B02',
                    'green': 'B03',
                    'red': 'B04',
                    'nir': 'B05',
                    'swir1': 'B06',
                    'swir2': 'B07',
                    'qa': 'Fmask'}

s30_v2_band_dict = {'blue': 'B02',
                    'green': 'B03',
                    'red': 'B04',
                    'nir': 'B8A',
                    'swir1': 'B11',
                    'swir2': 'B12',
                    'qa': 'Fmask'}

DIAGNOSTIC_LAYER_NO_DATA_DECIMAL = 0b100000
DIAGNOSTIC_LAYER_NO_DATA_BINARY_REPR = 100000

interpreted_dswx_band_dict = {
    0b00000 : 0,  # (Not Water)
    0b00001 : 0,
    0b00010 : 0,
    0b00100 : 0,
    0b01000 : 0,
    0b01111 : 1,  # (Water - High Confidence)
    0b10111 : 1,
    0b11011 : 1,
    0b11101 : 1,
    0b11110 : 1,
    0b11111 : 1,
    0b00111 : 2,  # (Water - Moderate Confidence)
    0b01011 : 2,
    0b01101 : 2,
    0b01110 : 2,
    0b10011 : 2,
    0b10101 : 2,
    0b10110 : 2,
    0b11001 : 2,
    0b11010 : 2,
    0b11100 : 2,
    0b11000 : 3,  # (Potential Wetland)
    0b00011 : 4,  #(Low Confidence Water or Wetland)
    0b00101 : 4,
    0b00110 : 4,
    0b01001 : 4,
    0b01010 : 4,
    0b01100 : 4,
    0b10000 : 4,
    0b10001 : 4,
    0b10010 : 4,
    0b10100 : 4
}

FLAG_COLLAPSE_WTR_CLASSES = True
FLAG_CLIP_NEGATIVE_REFLECTANCE = True
FLAG_COMPUTE_AVERAGE_SENSING_TIME = False

# Not-water classes
WTR_NOT_WATER = 0

# Water classes

WTR_COLLAPSED_OPEN_WATER = 1
WTR_COLLAPSED_PARTIAL_SURFACE_WATER = 2
WTR_UNCOLLAPSED_HIGH_CONF_WATER = 1
WTR_UNCOLLAPSED_MODERATE_CONF_WATER = 2
WTR_UNCOLLAPSED_POTENTIAL_WETLAND = 3
WTR_UNCOLLAPSED_LOW_CONF_WATER = 4

FIRST_UNCOLLAPSED_WATER_CLASS = 1
LAST_UNCOLLAPSED_WATER_CLASS = 4

# Cloud/cloud-shadow masked classes
WTR_CLOUD_MASKED = 9

# Shadow mask
SHAD_NOT_MASKED = 1
SHAD_MASKED = 0

# Other classes
BWTR_WATER = 1
CONF_NOT_WATER = 99
CONF_CLOUD_MASKED = 254

'''
Internally, DSWx-HLS has 4 water classes derived from
USGS DSWe:

1. High-confidence water;
2. Moderate-confidence water;
3. Potential wetland;
4. Low-confidence water or wetland.

These classes are collapsed into 2 classes when DSWx-HLS
WTR layers are saved:
1. Open water;
2. Partial surface water.
'''
collapse_wtr_classes_dict = {
    WTR_NOT_WATER: WTR_NOT_WATER,
    WTR_UNCOLLAPSED_HIGH_CONF_WATER: WTR_COLLAPSED_OPEN_WATER,
    WTR_UNCOLLAPSED_MODERATE_CONF_WATER: WTR_COLLAPSED_OPEN_WATER,
    WTR_UNCOLLAPSED_POTENTIAL_WETLAND: WTR_COLLAPSED_PARTIAL_SURFACE_WATER,
    WTR_UNCOLLAPSED_LOW_CONF_WATER: WTR_COLLAPSED_PARTIAL_SURFACE_WATER,
    WTR_CLOUD_MASKED: WTR_CLOUD_MASKED,
    UINT8_FILL_VALUE: UINT8_FILL_VALUE
}

'''
Dictionary containing the mapping from the output 2-water classes
(after collapsing) to confidence values in percent
'''
wtr_confidence_dict = {
    WTR_NOT_WATER: CONF_NOT_WATER,
    WTR_COLLAPSED_OPEN_WATER: 85,
    WTR_COLLAPSED_PARTIAL_SURFACE_WATER: 70,
    WTR_CLOUD_MASKED: CONF_CLOUD_MASKED,
    UINT8_FILL_VALUE: UINT8_FILL_VALUE
}

'''
Dictionary containing the mapping from the original 4-water classes
(before collapsing) to confidence values in percent
'''
wtr_confidence_non_collapsed_dict = {
    WTR_NOT_WATER: CONF_NOT_WATER,
    WTR_UNCOLLAPSED_HIGH_CONF_WATER: 95,
    WTR_UNCOLLAPSED_MODERATE_CONF_WATER: 80,
    WTR_UNCOLLAPSED_POTENTIAL_WETLAND: 70,
    WTR_UNCOLLAPSED_LOW_CONF_WATER: 60,
    WTR_CLOUD_MASKED: CONF_CLOUD_MASKED,
    UINT8_FILL_VALUE: UINT8_FILL_VALUE
}

collapsable_layers_list = ['WTR', 'WTR-1', 'WTR-2']

band_description_dict = {
    'WTR': 'Water classification (WTR)',
    'BWTR': 'Binary Water (BWTR)',
    'CONF': 'TBD Confidence (CONF)',
    'DIAG': 'Diagnostic layer (DIAG)',
    'WTR-1': 'Interpretation of diagnostic layer into water classes (WTR-1)',
    'WTR-2': 'Interpreted layer refined using land cover and terrain shadow testing (WTR-2)',
    'LAND': 'Land cover classification (LAND)',
    'SHAD': 'Terrain shadow layer (SHAD)',
    'CLOUD': 'Cloud/cloud-shadow classification (CLOUD)',
    'DEM': 'Digital elevation model (DEM)'}

layer_names_to_args_dict = {
    'WTR': 'output_interpreted_band',
    'BWTR': 'output_binary_water',
    'CONF': 'output_confidence_layer',
    'DIAG': 'output_diagnostic_layer',
    'WTR-1': 'output_non_masked_dswx',
    'WTR-2': 'output_shadow_masked_dswx',
    'LAND': 'output_landcover',
    'SHAD': 'output_shadow_layer',
    'CLOUD': 'output_cloud_mask',
    'DEM': 'output_dem_layer',
    'RGB': 'output_rgb_file',
    'INFRARED_RGB': 'output_infrared_rgb_file'}


METADATA_FIELDS_TO_COPY_FROM_HLS_LIST = ['SPATIAL_COVERAGE',
                                         'CLOUD_COVERAGE',
                                         'MEAN_SUN_AZIMUTH_ANGLE',
                                         'MEAN_SUN_ZENITH_ANGLE',
                                         'MEAN_VIEW_AZIMUTH_ANGLE',
                                         'MEAN_VIEW_ZENITH_ANGLE',
                                         'NBAR_SOLAR_ZENITH',
                                         'ACCODE']

# landcover constants
dswx_hls_landcover_classes_dict = {
    # first year 2000, last 2099: classes 0-99
    'low_intensity_developed_offset': 0,

    # first year 2000, last 2099: classes 100-199
    'high_intensity_developed_offset': 100,

    # other classes
    'water': 200,
    'evergreen_forest': 201,

    # fill value (not masked)
    'fill_value': UINT8_FILL_VALUE}

'''
Dict of landcover threshold list:
  [evergreen, low-intensity developed, high-intensity developed, water/wetland]
'''
landcover_threshold_dict = {"standard": [6, 3, 7, 3],
                            "water heavy": [6, 3, 7, 1]}

MIN_SLOPE_ANGLE = -5
MAX_SUN_LOCAL_INC_ANGLE = 40


class HlsThresholds:
    """
    Placeholder for HLS reflectance thresholds for generating DSWx-HLS products

    Attributes
    ----------
    wigt : float
        Modified Normalized Difference Wetness Index (MNDWI) Threshold
    awgt : float
        Automated Water Extent Shadow Threshold
    pswt_1_mndwi : float
        Partial Surface Water Test-1 MNDWI Threshold
    pswt_1_nir : float
        Partial Surface Water Test-1 NIR Threshold
    pswt_1_swir1 : float
        Partial Surface Water Test-1 SWIR1 Threshold
    pswt_1_ndvi : float
        Partial Surface Water Test-1 NDVI Threshold
    pswt_2_mndwi : float
        Partial Surface Water Test-2 MNDWI Threshold
    pswt_2_blue : float
        Partial Surface Water Test-2 Blue Threshold
    pswt_2_nir : float
        Partial Surface Water Test-2 NIR Threshold
    pswt_2_swir1 : float
        Partial Surface Water Test-2 SWIR1 Threshold
    pswt_2_swir2 : float
        Partial Surface Water Test-2 SWIR2 Threshold
    lcmask_nir : float
        Land Cover Mask based test Near Infrared
    """
    def __init__(self):

        self.wigt = None
        self.awgt = None
        self.pswt_1_mndwi = None
        self.pswt_1_nir = None
        self.pswt_1_swir1 = None
        self.pswt_1_ndvi = None
        self.pswt_2_mndwi = None
        self.pswt_2_blue = None
        self.pswt_2_nir = None
        self.pswt_2_swir1 = None
        self.pswt_2_swir2 = None
        self.lcmask_nir = None


def get_dswx_hls_cli_parser():
    parser = argparse.ArgumentParser(
        description='Generate a DSWx-HLS product from an HLS product',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    # Inputs
    parser.add_argument('input_list',
                        type=str,
                        nargs='+',
                        help='Input YAML run configuration file or HLS product file(s)')

    parser.add_argument('--dem',
                        dest='dem_file',
                        type=str,
                        help='Input digital elevation model (DEM)')

    parser.add_argument('--dem-description',
                        dest='dem_description',
                        type=str,
                        help='Description for the input digital elevation'
                        ' model (DEM)')

    parser.add_argument('-c',
                        '--landcover',
                        dest='landcover_file',
                        type=str,
                        help='Input Copernicus Land Cover'
                        ' Discrete-Classification-map 100m')

    parser.add_argument('--landcover-description',
                        dest='landcover_description',
                        type=str,
                        help='Description for the input Copernicus Land Cover'
                        ' Discrete-Classification-map 100m')

    parser.add_argument('-w',
                        '--worldcover',
                        dest='worldcover_file',
                        type=str,
                        help='Input ESA WorldCover 10m')

    parser.add_argument('--worldcover-description',
                        dest='worldcover_description',
                        type=str,
                        help='Description for the input ESA WorldCover 10m')


    # Outputs
    parser.add_argument('-o',
                        '--output-file',
                        dest='output_file',
                        type=str,
                        help='Output DSWx-HLS product (GeoTIFF)')

    parser.add_argument('--wtr',
                        '--interpreted-band',
                        dest='output_interpreted_band',
                        type=str,
                        help='Output interpreted DSWx layer (GeoTIFF)')

    parser.add_argument('--output-rgb',
                        '--output-rgb-file',
                        dest='output_rgb_file',
                        type=str,
                        help='Output RGB reflectance file (GeoTIFF)'
                        ' copied from input HLS product.')

    parser.add_argument('--output-infrared-rgb',
                        '--output-infrared-rgb-file',
                        dest='output_infrared_rgb_file',
                        type=str,
                        help='Output infrared SWIR-1, NIR, and Red RGB'
                        '-color-composition GeoTIFF file')

    parser.add_argument('--bwtr'
                        '--output-binary-water',
                        dest='output_binary_water',
                        type=str,
                        help='Output binary water mask (GeoTIFF)')

    parser.add_argument('--conf'
                        '--output-confidence-layer',
                        dest='output_confidence_layer',
                        type=str,
                        help='Output confidence layer (GeoTIFF)')

    parser.add_argument('--diag',
                        '--output-diagnostic-layer',
                        dest='output_diagnostic_layer',
                        type=str,
                        help='Output diagnostic test layer file (GeoTIFF)')

    parser.add_argument('--wtr-1',
                        '--output-non-masked-dswx',
                        dest='output_non_masked_dswx',
                        type=str,
                        help='Output non-masked DSWx layer file (GeoTIFF)')

    parser.add_argument('--wtr-2',
                        '--output-shadow-masked-dswx',
                        dest='output_shadow_masked_dswx',
                        type=str,
                        help='Output GeoTIFF file with interpreted layer'
                        ' refined using land cover and terrain shadow testing')

    parser.add_argument('--land',
                        '--output-land',
                        dest='output_landcover',
                        type=str,
                        help='Output landcover classification file (GeoTIFF)')

    parser.add_argument('--shad',
                        '--output-shadow-layer',
                        dest='output_shadow_layer',
                        type=str,
                        help='Output terrain shadow layer file (GeoTIFF)')

    parser.add_argument('--cloud'
                        '--output-cloud-mask',
                        dest='output_cloud_mask',
                        type=str,
                        help='Output cloud/cloud-shadow classification file'
                        ' (GeoTIFF)')

    parser.add_argument('--out-dem'
                        '--output-digital-elevation-model',
                        '--output-elevation-layer',
                        dest='output_dem_layer',
                        type=str,
                        help='Output elevation layer file (GeoTIFF)')

    parser.add_argument('--browse'
                        '--output-browse-image',
                        dest='output_browse_image',
                        type=str,
                        help='Output browse image file (png)')

    # Parameters
    parser.add_argument('--bheight'
                        '--browse-image-height',
                        dest='browse_image_height',
                        type=int,
                        help='Height in pixels for browse image PNG')

    parser.add_argument('--bwidth'
                        '--browse-image-width',
                        dest='browse_image_width',
                        type=int,
                        help='Width in pixels for browse image PNG')

    parser.add_argument('--offset-and-scale-inputs',
                        dest='flag_offset_and_scale_inputs',
                        action='store_true',
                        default=False,
                        help='Offset and scale HLS inputs before processing')

    parser.add_argument('--scratch-dir',
                        '--temp-dir',
                        '--temporary-dir',
                        dest='scratch_dir',
                        type=str,
                        help='Scratch (temporary) directory')

    parser.add_argument('--pid',
                        '--product-id',
                        dest='product_id',
                        type=str,
                        help='Product ID that will be saved in the output'
                        "product's metadata")

    parser.add_argument('--debug',
                        dest='flag_debug',
                        action='store_true',
                        default=False,
                        help='Activate debug mode')

    parser.add_argument('--log',
                        '--log-file',
                        dest='log_file',
                        type=str,
                        help='Log file')

    parser.add_argument('--full-log-format',
                        dest='full_log_formatting',
                        action='store_true',
                        default=False,
                        help='Enable full formatting of log messages')

    return parser


def _get_prefix_str(flag_same, flag_all_ok):
    flag_all_ok[0] = flag_all_ok[0] and flag_same
    return '[OK]   ' if flag_same else '[FAIL] '


def compare_dswx_hls_products(file_1, file_2):
    if not os.path.isfile(file_1):
        print(f'ERROR file not found: {file_1}')
        return False

    if not os.path.isfile(file_2):
        print(f'ERROR file not found: {file_2}')
        return False

    flag_all_ok = [True]

    # TODO: compare projections ds.GetProjection()
    layer_gdal_dataset_1 = gdal.Open(file_1, gdal.GA_ReadOnly)
    geotransform_1 = layer_gdal_dataset_1.GetGeoTransform()
    metadata_1 = layer_gdal_dataset_1.GetMetadata()
    nbands_1 = layer_gdal_dataset_1.RasterCount

    layer_gdal_dataset_2 = gdal.Open(file_2, gdal.GA_ReadOnly)
    geotransform_2 = layer_gdal_dataset_2.GetGeoTransform()
    metadata_2 = layer_gdal_dataset_2.GetMetadata()
    nbands_2 = layer_gdal_dataset_2.RasterCount

    # compare number of bands
    flag_same_nbands =  nbands_1 == nbands_2
    flag_same_nbands_str = _get_prefix_str(flag_same_nbands, flag_all_ok)
    prefix = ' ' * 7
    print(f'{flag_same_nbands_str}Comparing number of bands')
    if not flag_same_nbands:
        print(prefix + f'Input 1 has {nbands_1} bands and input 2'
              f' has {nbands_2} bands')
        return False

    # compare array values
    print('Comparing DSWx bands...')
    for b in range(1, nbands_1 + 1):
        gdal_band_1 = layer_gdal_dataset_1.GetRasterBand(b)
        gdal_band_2 = layer_gdal_dataset_2.GetRasterBand(b)
        image_1 = gdal_band_1.ReadAsArray()
        image_2 = gdal_band_2.ReadAsArray()
        flag_bands_are_equal = np.allclose(
            image_1, image_2, atol=COMPARE_DSWX_HLS_PRODUCTS_ERROR_TOLERANCE)
        flag_bands_are_equal_str = _get_prefix_str(flag_bands_are_equal,
                                                   flag_all_ok)
        print(f'{flag_bands_are_equal_str}     Band {b} -'
              f' {gdal_band_1.GetDescription()}"')
        if not flag_bands_are_equal:
            _print_first_value_diff(image_1, image_2, prefix)

    # compare geotransforms
    flag_same_geotransforms = np.array_equal(geotransform_1, geotransform_2)
    flag_same_geotransforms_str = _get_prefix_str(flag_same_geotransforms,
                                                  flag_all_ok)
    print(f'{flag_same_geotransforms_str}Comparing geotransform')
    if not flag_same_geotransforms:
        print(prefix + f'* input 1 geotransform with content "{geotransform_1}"'
              f' differs from input 2 geotransform with content'
              f' "{geotransform_2}".')

    # compare metadata
    metadata_error_message, flag_same_metadata = \
        _compare_dswx_hls_metadata(metadata_1, metadata_2)

    flag_same_metadata_str = _get_prefix_str(flag_same_metadata,
                                             flag_all_ok)
    print(f'{flag_same_metadata_str}Comparing metadata')

    if not flag_same_metadata:
        print(prefix + metadata_error_message)

    return flag_all_ok[0]


def _compare_dswx_hls_metadata(metadata_1, metadata_2):
    """
    Compare DSWx-HLS products' metadata

       Parameters
       ----------
       metadata_1 : dict
            Metadata of the first DSWx-HLS product
       metadata_2: dict
            Metadata of the second
    """
    metadata_error_message = None
    flag_same_metadata = len(metadata_1.keys()) == len(metadata_2.keys())
    if not flag_same_metadata:
        metadata_error_message = (
            f'* input 1 metadata has {len(metadata_1.keys())} entries'
            f' whereas input 2 metadata has {len(metadata_2.keys())} entries.')

        set_1_m_2 = set(metadata_1.keys()) - set(metadata_2.keys())
        if len(set_1_m_2) > 0:
            metadata_error_message += (' Input 1 metadata has extra entries'
                                       ' with keys:'
                                       f' {", ".join(set_1_m_2)}.')
        set_2_m_1 = set(metadata_2.keys()) - set(metadata_1.keys())
        if len(set_2_m_1) > 0:
            metadata_error_message += (' Input 2 metadata has extra entries'
                                       ' with keys:'
                                       f' {", ".join(set_2_m_1)}.')
    else:
        for k1, v1, in metadata_1.items():
            if k1 not in metadata_2.keys():
                 flag_same_metadata = False
                 metadata_error_message = (
                     f'* the metadata key {k1} is present in'
                     ' but it is not present in input 2')
                 break
            if k1 == 'PROCESSING_DATETIME':
                # Processing datetimes are expected to be different from
                # input 1 and 2
                continue
            if metadata_2[k1] != v1:
                 flag_same_metadata = False
                 metadata_error_message = (
                     f'* contents of metadata key {k1} from'
                     f' input 1 has value "{v1}" whereas the same key in'
                     f' input 2 metadata has value "{metadata_2[k1]}"')
                 break
    return metadata_error_message, flag_same_metadata


def _print_first_value_diff(image_1, image_2, prefix):
    """
    Print first value difference between two images.

       Parameters
       ----------
       image_1 : numpy.ndarray
            First input image
       image_2: numpy.ndarray
            Second input image
       prefix: str
            Prefix to the message printed to the user
    """
    flag_error_found = False
    for i in range(image_1.shape[0]):
        for j in range(image_1.shape[1]):
            if (abs(image_1[i, j] - image_2[i, j]) >
                    COMPARE_DSWX_HLS_PRODUCTS_ERROR_TOLERANCE):
                continue
            print(prefix + f'     * input 1 has value'
                  f' "{image_1[i, j]}" in position'
                  f' (x: {j}, y: {i})'
                  f' whereas input 2 has value "{image_2[i, j]}"'
                  ' in the same position.')
            flag_error_found = True
            break
        if flag_error_found:
            break


def decimate_by_summation(image, size_y, size_x):
    """Decimate an array by summation using a window of size 
       `size_y` by `size_x`.

       Parameters
       ----------
       image: numpy.ndarray
              Input image
       size_y: int
              Number of looks in the Y-direction (row)
       size_y: int
              Number of looks in the X-direction (column)

       Returns
       -------
       out_image : numpy.ndarray
              Output image

    """
    for i in range(size_y):
        for j in range(size_x):
            image_slice = image[i::size_y, j::size_x]
            if i == 0 and j == 0:
                current_image = np.copy(image_slice)
                out_image = np.zeros_like(current_image)
            else:
                current_image[0:image_slice.shape[0],
                              0:image_slice.shape[1]] = \
                                image_slice
            out_image += current_image
    return out_image

def _update_landcover_array(conglomerate_array, agg_sum, threshold,
        classification_val):
    flat_agg = agg_sum.reshape(-1)
    for position, value in enumerate(flat_agg):
        if value >= threshold:
            conglomerate_array[position] = classification_val
    return


def create_landcover_mask(copernicus_landcover_file,
                          worldcover_file, output_file, scratch_dir,
                          mask_type, geotransform, projection, length, width,
                          dswx_metadata_dict = None, output_files_list = None,
                          temp_files_list = None):
    """
    Create landcover mask LAND combining Copernicus Global Land Service
    (CGLS) Land Cover Layers collection 3 at 100m and ESA WorldCover 10m.

       Parameters
       ----------
       copernicus_landcover_file : str
            Copernicus Global Land Service (CGLS) Land Cover Layer file
            collection 3 at 100m
       worldcover_file : str
            ESA WorldCover map file
       output_file : str
            Output landcover mask (LAND layer)
       scratch_dir : str
              Temporary directory
       mask_type : str
              Mask type. Options: "Standard" and "Water Heavy"
       geotransform: numpy.ndarray
              Geotransform describing the DSWx-HLS product geolocation
       projection: str
              DSWx-HLS product's projection
       length: int
              DSWx-HLS product's length (number of lines)
       width: int
              DSWx-HLS product's width (number of columns)
       dswx_metadata_dict: dict (optional)
              Metadata dictionary that will store band metadata 
       output_files_list: list (optional)
              Mutable list of output files
       temp_files_list: list (optional)
              Mutable list of temporary files
    """
    if not os.path.isfile(copernicus_landcover_file):
        logger.error(f'ERROR file not found: {copernicus_landcover_file}')
        return

    if not os.path.isfile(worldcover_file):
        logger.error(f'ERROR file not found: {worldcover_file}')
        return

    logger.info(f'Copernicus landcover 100 m file: {copernicus_landcover_file}')
    logger.info(f'World cover 10 m file: {worldcover_file}')

    # Reproject Copernicus land cover
    copernicus_landcover_reprojected_file = os.path.join(
        scratch_dir, 'copernicus_reprojected.tif')
    copernicus_landcover_array = _warp(copernicus_landcover_file,
        geotransform, projection,
        length, width, scratch_dir, resample_algorithm='nearest',
        relocated_file=copernicus_landcover_reprojected_file,
        temp_files_list=temp_files_list)
    temp_files_list.append(copernicus_landcover_reprojected_file)

    # Reproject ESA Worldcover 10m from geographic (lat/lon) to MGRS (UTM) 10m
    geotransform_up_3 = list(geotransform)
    geotransform_up_3[1] = geotransform[1] / 3  # dx / 3
    geotransform_up_3[5] = geotransform[5] / 3  # dy / 3
    length_up_3 = 3 * length
    width_up_3 = 3 * width
    worldcover_reprojected_up_3_file = os.path.join(
        scratch_dir, 'worldcover_reprojected_up_3.tif')
    worldcover_array_up_3 = _warp(worldcover_file, geotransform_up_3,
        projection, length_up_3, width_up_3, scratch_dir,
        resample_algorithm='nearest',
        relocated_file=worldcover_reprojected_up_3_file,
        temp_files_list=temp_files_list)
    temp_files_list.append(worldcover_reprojected_up_3_file)

    # Set multilooking parameters
    size_y = 3
    size_x = 3

    # Create water mask
    logger.info(f'creating water mask')
    # WorldCover class 80: permanent water bodies
    # WorldCover class 90: herbaceous wetland
    water_binary_mask = np.where((worldcover_array_up_3 == 80) |
                                 (worldcover_array_up_3 == 90), 1, 0)
    water_aggregate_sum = decimate_by_summation(water_binary_mask,
                                              size_y, size_x)
    del water_binary_mask

    # Create urban-areas mask
    logger.info(f'creating urban-areas mask')
    # WorldCover class 50: built-up
    urban_binary_mask = np.where((worldcover_array_up_3 == 50) , 1, 0)
    urban_aggregate_sum = decimate_by_summation(urban_binary_mask,
                                                size_y, size_x)
    del urban_binary_mask

    # Create vegetation mask
    logger.info(f'creating vegetation mask')
    # WorldCover class 10: tree cover
    tree_binary_mask  = np.where((worldcover_array_up_3 == 10) , 1, 0)
    del worldcover_array_up_3
    tree_aggregate_sum = decimate_by_summation(tree_binary_mask,
                                               size_y, size_x)
    del tree_binary_mask

    logger.info(f'combining masks')
    tree_aggregate_sum = np.where(copernicus_landcover_array == 111,
                                  tree_aggregate_sum, 0)

    # create array filled with 30000
    landcover_fill_value = \
        dswx_hls_landcover_classes_dict['fill_value']
    hierarchy_combined = np.full(water_aggregate_sum.reshape(-1).shape,
        landcover_fill_value, dtype=np.uint8)

    # load threshold list according to `mask_type`
    threshold_list = landcover_threshold_dict[mask_type.lower()]

    # aggregate sum value of 7/9 or higher is called tree
    evergreen_forest_class = \
        dswx_hls_landcover_classes_dict['evergreen_forest']
    _update_landcover_array(hierarchy_combined, tree_aggregate_sum,
                            threshold_list[0], evergreen_forest_class)

    # majority of pixels are urban
    year = datetime.date.today().year - 2000
    low_intensity_developed_class = \
        (dswx_hls_landcover_classes_dict['low_intensity_developed_offset'] +
         year)
    _update_landcover_array(hierarchy_combined, urban_aggregate_sum,
                            threshold_list[1], low_intensity_developed_class)

    # high density urban at 7/9 or higher
    high_intensity_developed_class = \
        (dswx_hls_landcover_classes_dict['high_intensity_developed_offset'] +
         year)
    _update_landcover_array(hierarchy_combined, urban_aggregate_sum,
                            threshold_list[2], high_intensity_developed_class)

    # water where 1/3 or more pixels
    water_class = \
        dswx_hls_landcover_classes_dict['water']
    _update_landcover_array(hierarchy_combined, water_aggregate_sum,
                            threshold_list[3], water_class)
    
    hierarchy_combined = hierarchy_combined.reshape(water_aggregate_sum.shape)

    ctable = _get_landcover_mask_ctable()
    
    description = band_description_dict['LAND']

    if output_file is not None:
        _save_array(hierarchy_combined, output_file,
                    dswx_metadata_dict, geotransform,
                    projection, description = description,
                    scratch_dir=scratch_dir,
                    output_files_list = output_files_list,
                    ctable=ctable)

    return hierarchy_combined


def _is_landcover_class_evergreen(landcover_mask):
    """Return boolean mask identifying areas marked as evergreen.

       Parameters
       ----------
       landcover_mask: numpy.ndarray
              Landcover mask

       Returns
       -------
       evergreen_mask : numpy.ndarray
              Evergreen mask
    """ 
    evergreen_forest_class = \
        dswx_hls_landcover_classes_dict['evergreen_forest']
    return landcover_mask == evergreen_forest_class

def _is_landcover_class_water_or_wetland(landcover_mask):
    """Return boolean mask identifying areas marked as water or wetland.

       Parameters
       ----------
       landcover_mask: numpy.ndarray
              Landcover mask

       Returns
       -------
       evergreen_mask : numpy.ndarray
              Water or wetland mask
    """ 
    water_class = \
        dswx_hls_landcover_classes_dict['water']
    return landcover_mask == water_class

def _is_landcover_class_low_intensity_developed(landcover_mask):
    """Return boolean mask identifying areas marked as low-intensity
    developed.

       Parameters
       ----------
       landcover_mask: numpy.ndarray
              Landcover mask

       Returns
       -------
       low_intensity_developed_mask : numpy.ndarray
              Low-intensity developed
    """ 
    low_intensity_developed_class_offset = \
        dswx_hls_landcover_classes_dict['low_intensity_developed_offset']
    low_intensity_developed_mask = (
        (landcover_mask >= low_intensity_developed_class_offset) & 
        (landcover_mask < low_intensity_developed_class_offset + 100))
    return low_intensity_developed_mask

def _is_landcover_class_high_intensity_developed(landcover_mask):
    """Return boolean mask identifying areas marked as high-intensity
    developed.

       Parameters
       ----------
       landcover_mask: numpy.ndarray
              Landcover mask

       Returns
       -------
       high_intensity_developed_mask : numpy.ndarray
              High-intensity developed
    """ 
    high_intensity_developed_class_offset = \
        dswx_hls_landcover_classes_dict['high_intensity_developed_offset']
    high_intensity_developed_mask = \
        ((landcover_mask >= high_intensity_developed_class_offset) &
         (landcover_mask < high_intensity_developed_class_offset + 100))
    return high_intensity_developed_mask


def _apply_landcover_and_shadow_masks(interpreted_layer, nir,
        landcover_mask, shadow_layer, hls_thresholds):
    """Apply landcover and shadow masks onto interpreted layer

       Parameters
       ----------
       interpreted_layer: numpy.ndarray
              Interpreted layer
       nir: numpy.ndarray
              Near infrared (NIR) channel
       landcover_mask: numpy.ndarray
              Landcover mask
       shadow_layer: numpy.ndarray
              Shadow mask
       hls_thresholds:
              HLS reflectance thresholds for generating DSWx-HLS products

       Returns
       -------
       landcover_shadow_masked_dswx : numpy.ndarray
              Shadow-masked interpreted layer
    """

    landcover_shadow_masked_dswx = interpreted_layer.copy()

    # apply shadow mask - shadows are set to 0 (not water)
    if shadow_layer is not None and landcover_mask is None:
        logger.info('applying shadow mask:')
        to_mask_ind = np.where((shadow_layer == SHAD_MASKED) &
            ((interpreted_layer >= FIRST_UNCOLLAPSED_WATER_CLASS) |
             (interpreted_layer <= LAST_UNCOLLAPSED_WATER_CLASS)))
        landcover_shadow_masked_dswx[to_mask_ind] = WTR_NOT_WATER

    elif shadow_layer is not None:
        logger.info('applying shadow mask (with landcover):')
        to_mask_ind = np.where((shadow_layer == SHAD_MASKED) &
            (~_is_landcover_class_water_or_wetland(landcover_mask)) &
            ((interpreted_layer >= FIRST_UNCOLLAPSED_WATER_CLASS) &
             (interpreted_layer <= LAST_UNCOLLAPSED_WATER_CLASS)))
        landcover_shadow_masked_dswx[to_mask_ind] = WTR_NOT_WATER

    if landcover_mask is None:
        return landcover_shadow_masked_dswx

    logger.info('applying landcover mask:')

    # Check landcover (evergreen)
    to_mask_ind = np.where(
        _is_landcover_class_evergreen(landcover_mask) &
        (nir > hls_thresholds.lcmask_nir) &
         ((interpreted_layer == WTR_UNCOLLAPSED_POTENTIAL_WETLAND) |
          (interpreted_layer == WTR_UNCOLLAPSED_LOW_CONF_WATER)))
    landcover_shadow_masked_dswx[to_mask_ind] = WTR_NOT_WATER

    # Check landcover (low intensity developed)
    to_mask_ind = np.where(
        _is_landcover_class_low_intensity_developed(landcover_mask) &
        (nir > hls_thresholds.lcmask_nir) &
         ((interpreted_layer == WTR_UNCOLLAPSED_POTENTIAL_WETLAND) |
          (interpreted_layer == WTR_UNCOLLAPSED_LOW_CONF_WATER)))
    landcover_shadow_masked_dswx[to_mask_ind] = WTR_NOT_WATER

    # Check landcover (high intensity developed)
    to_mask_ind = np.where(
        _is_landcover_class_high_intensity_developed(landcover_mask) &
        ((interpreted_layer >= FIRST_UNCOLLAPSED_WATER_CLASS) &
         (interpreted_layer <= LAST_UNCOLLAPSED_WATER_CLASS)))
    landcover_shadow_masked_dswx[to_mask_ind] = WTR_NOT_WATER

    return landcover_shadow_masked_dswx



def _get_interpreted_dswx_ctable(
        flag_collapse_wtr_classes = FLAG_COLLAPSE_WTR_CLASSES):
    """Create and return GDAL RGB color table for DSWx-HLS
       surface water interpreted layers.

       flag_collapse_wtr_classes: bool
            Flag that indicates if interpreted layer contains
            collapsed classes following the standard DSWx-HLS product
            water classes

       Returns
       -------
       dswx_ctable : GDAL ColorTable object
            GDAL color table for DSWx-HLS surface water interpreted layers
    """
    # create color table
    dswx_ctable = gdal.ColorTable()

    # set color for each value

    # White - Not water
    dswx_ctable.SetColorEntry(WTR_NOT_WATER, (255, 255, 255))

    if flag_collapse_wtr_classes:
        # Blue - Open water
        dswx_ctable.SetColorEntry(WTR_COLLAPSED_OPEN_WATER,
                                  (0, 0, 255)) 
        # Green - Partial surface water
        dswx_ctable.SetColorEntry(WTR_COLLAPSED_PARTIAL_SURFACE_WATER,
                                  (0, 255, 0))
    else:
        # Blue - Water (high confidence)
        dswx_ctable.SetColorEntry(WTR_UNCOLLAPSED_HIGH_CONF_WATER,
                                  (0, 0, 255)) 
        # Light blue - Water (moderate conf.)
        dswx_ctable.SetColorEntry(WTR_UNCOLLAPSED_MODERATE_CONF_WATER,
                                  (0, 127, 255))
        # Dark green - Potential wetland
        dswx_ctable.SetColorEntry(WTR_UNCOLLAPSED_POTENTIAL_WETLAND,
                                  (0, 127, 0))
        # Green - Low confidence water or wetland
        dswx_ctable.SetColorEntry(WTR_UNCOLLAPSED_LOW_CONF_WATER,
                                  (0, 255, 0))

    # Gray - QA masked
    dswx_ctable.SetColorEntry(WTR_CLOUD_MASKED, (127, 127, 127))

    # Black - Fill value
    dswx_ctable.SetColorEntry(UINT8_FILL_VALUE, (0, 0, 0, 255))

    return dswx_ctable


def _get_cloud_mask_ctable():
    """Create and return GDAL RGB color table for DSWx-HLS cloud/cloud-shadow mask.

       Returns
       -------
       dswx_ctable : GDAL ColorTable object
            GDAL color table for DSWx-HLS cloud/cloud-shadow mask.
    """
    # create color table
    mask_ctable = gdal.ColorTable()

    # set color for each value
    # - Mask cloud shadow bit (0)
    # - Mask snow/ice bit (1)
    # - Mask cloud bit (2)

    # White - Not masked
    mask_ctable.SetColorEntry(0, (255, 255, 255))
    # Dark gray - Cloud shadow
    mask_ctable.SetColorEntry(1, (64, 64, 64))
    # Cyan - snow/ice
    mask_ctable.SetColorEntry(2, (0, 255, 255))
    # Blue - Cloud shadow and snow/ice
    mask_ctable.SetColorEntry(3, (0, 0, 255))
    # Light gray - Cloud
    mask_ctable.SetColorEntry(4, (192, 192, 192))
    # Gray - Cloud and cloud shadow
    mask_ctable.SetColorEntry(5, (127, 127, 127))
    # Magenta - Cloud and snow/ice
    mask_ctable.SetColorEntry(6, (255, 0, 255))
    # Light blue - Cloud, cloud shadow, and snow/ice
    mask_ctable.SetColorEntry(7, (127, 127, 255))
    # Black - Fill value
    mask_ctable.SetColorEntry(UINT8_FILL_VALUE, (0, 0, 0, 255))
    return mask_ctable


def _get_landcover_mask_ctable():
    """Create and return GDAL RGB color table for DSWx-HLS landcover mask.

       Returns
       -------
       dswx_ctable : GDAL ColorTable object
            GDAL color table for DSWx-HLS landcover mask.
    """
    # create color table
    mask_ctable = gdal.ColorTable()

    fill_value = \
        dswx_hls_landcover_classes_dict['fill_value']
    evergreen_forest_class = \
        dswx_hls_landcover_classes_dict['evergreen_forest']
    water_class = \
        dswx_hls_landcover_classes_dict['water']
    low_intensity_developed_class_offset = \
        dswx_hls_landcover_classes_dict['low_intensity_developed_offset']
    high_intensity_developed_class_offset = \
        dswx_hls_landcover_classes_dict['high_intensity_developed_offset']

    # White - Not masked (fill_value)
    mask_ctable.SetColorEntry(fill_value, (255, 255, 255))

    # Green - Evergreen forest class
    mask_ctable.SetColorEntry(evergreen_forest_class, (0, 255, 0))

    # Blue - Water class
    mask_ctable.SetColorEntry(water_class, (0, 0, 255))

    # Magenta - Low intensity developed
    for i in range(100):
        mask_ctable.SetColorEntry(low_intensity_developed_class_offset + i,
                                  (255, 0, 255))

    # Red - High intensity developed
    for i in range(100):
        mask_ctable.SetColorEntry(high_intensity_developed_class_offset + i, 
                                  (255, 0, 0))

    return mask_ctable

def _compute_otsu_threshold(image, is_normalized = True):
    """Compute Otsu threshold
       source: https://learnopencv.com/otsu-thresholding-with-opencv/

       Parameters
       ----------
       image: numpy.ndarray
              Input image
       is_normalized: bool (optional)
              Flag to inform the function if input image is normalized

       Returns
       -------
       binary_array : numpy.ndarray
            Binary array after thresholding input image with Otsu's threshold
    """
    # Set total number of bins in the histogram
    bins_num = 256

    # Get the image histogram
    hist, bin_edges = np.histogram(image, bins=bins_num)

    # Get normalized histogram if it is required
    if is_normalized:
        hist = np.divide(hist.ravel(), hist.max())

    # Calculate centers of bins
    bin_mids = (bin_edges[:-1] + bin_edges[1:]) / 2.

    # Iterate over all thresholds (indices) and get the probabilities w1(t), w2(t)
    weight1 = np.cumsum(hist)
    weight2 = np.cumsum(hist[::-1])[::-1]

    # Get the class means mu0(t)
    mean1 = np.cumsum(hist * bin_mids) / weight1
    # Get the class means mu1(t)
    mean2 = (np.cumsum((hist * bin_mids)[::-1]) / weight2[::-1])[::-1]

    inter_class_variance = weight1[:-1] * weight2[1:] * (mean1[:-1] - mean2[1:]) ** 2

    # Maximize the inter_class_variance function val
    index_of_max_val = np.argmax(inter_class_variance)

    threshold = bin_mids[:-1][index_of_max_val]
    logger.info(f"Otsu's algorithm implementation thresholding result: {threshold}")

    return image > threshold


def generate_interpreted_layer(diagnostic_layer):
    """Generate interpreted layer from diagnostic test band

       Parameters
       ----------
       diagnostic_layer: numpy.ndarray
              Diagnostic test band

       Returns
       -------
       interpreted_layer : numpy.ndarray
            Interpreted layer
    """
    logger.info('step 2 - get interpreted DSWX band')
    shape = diagnostic_layer.shape
    interpreted_layer = np.full(shape, 255, dtype = np.uint8)

    for key, value in interpreted_dswx_band_dict.items():
        interpreted_layer[diagnostic_layer == key] = value

    return interpreted_layer


def _get_binary_water_layer(interpreted_water_layer):
    """Generate binary water layer from interpreted water layer

       Parameters
       ----------
       interpreted_water_layer: numpy.ndarray
              Interpreted water layer

       Returns
       -------
       binary_water_layer : numpy.ndarray
            Binary water layer
    """
    # fill value
    binary_water_layer = np.full_like(interpreted_water_layer,
                                      UINT8_FILL_VALUE)

    # water classes
    binary_water_layer[interpreted_water_layer == WTR_NOT_WATER] = \
        WTR_NOT_WATER

    # water classes: 1 to 4
    for class_value in range(1, 5):
        binary_water_layer[interpreted_water_layer == class_value] = BWTR_WATER

    # Q/A masked
    binary_water_layer[interpreted_water_layer == WTR_CLOUD_MASKED] = \
        WTR_CLOUD_MASKED

    return binary_water_layer


def _get_confidence_layer(interpreted_layer,
        flag_collapse_wtr_classes = False):
    """
       Generate confidence layer from interpreted water layer

       Parameters
       ----------
       interpreted_layer: numpy.ndarray
              Interpreted water layer

       flag_collapse_wtr_classes: bool
            Flag that indicates if interpreted layer contains
            collapsed classes following the standard DSWx-HLS product
            water classes

       Returns
       -------
       confidence_layer : numpy.ndarray
            Confidence layer
    """
    if flag_collapse_wtr_classes:
        confidence_layer_classes = wtr_confidence_dict
    else:
        confidence_layer_classes = wtr_confidence_non_collapsed_dict
    confidence_layer = np.full_like(interpreted_layer, UINT8_FILL_VALUE)
    for original_value, new_value in confidence_layer_classes.items():
        confidence_layer[interpreted_layer == original_value] = new_value
    return confidence_layer


def _compute_diagnostic_tests(blue, green, red, nir, swir1, swir2,
                              hls_thresholds):
    """Compute diagnost tests over reflectance channels: Blue,
    Green, Red, NIR, SWIR-1, and SWIR-2, and return
    diagnostic test band

       Parameters
       ----------
       blue: numpy.ndarray
              Blue channel
       green: numpy.ndarray
              Green channel
       red: numpy.ndarray
              Red channel
       nir: numpy.ndarray
              Near infrared (NIR) channel
       swir1: numpy.ndarray
              Short-wave infrared 1 (SWIR-1) channel
       swir2: numpy.ndarray
              Short-wave infrared 2 (SWIR-2) channel
       hls_thresholds:
              HLS reflectance thresholds for generating DSWx-HLS products

       Returns
       -------
       diagnostic_layer : numpy.ndarray
            Diagnostic test band
    """

    # Modified Normalized Difference Wetness Index (MNDWI)
    mndwi = (green - swir1)/(green + swir1)

    # Multi-band Spectral Relationship Visible (MBSRV)
    mbsrv = green + red

    # Multi-band Spectral Relationship Near-Infrared (MBSRN)
    mbsrn = nir + swir1

    # Automated Water Extent Shadow (AWESH)
    awesh = blue + (2.5 * green) - (1.5 * mbsrn) - (0.25 * swir2)

    # Normalized Difference Vegetation Index (NDVI)
    ndvi = (nir - red) / (nir + red)

    # Diagnostic test band
    shape = blue.shape
    diagnostic_layer = np.zeros(shape, dtype = np.uint16)

    logger.info('step 1 - compute diagnostic tests')
    # Surface water tests (see [1, 2])

    # Test 1 (open water test, more conservative)
    diagnostic_layer[mndwi > hls_thresholds.wigt] += 1

    # Test 2 (open water test)
    diagnostic_layer[mbsrv > mbsrn] += 2

    # Test 3 (open water test)
    diagnostic_layer[awesh > hls_thresholds.awgt] += 4

    # Test 4 (partial surface water test)
    ind = np.where((mndwi > hls_thresholds.pswt_1_mndwi) &
                   (swir1 < hls_thresholds.pswt_1_swir1) &
                   (nir < hls_thresholds.pswt_1_nir) &
                   (ndvi < hls_thresholds.pswt_1_ndvi))
    diagnostic_layer[ind] += 8

    # Test 5 (partial surface water test)
    ind = np.where((mndwi > hls_thresholds.pswt_2_mndwi) &
                   (blue < hls_thresholds.pswt_2_blue) &
                   (swir1 < hls_thresholds.pswt_2_swir1) &
                   (swir2 < hls_thresholds.pswt_2_swir2) &
                   (nir < hls_thresholds.pswt_2_nir))
    diagnostic_layer[ind] += 16
   
    return diagnostic_layer


def _compute_mask_and_filter_interpreted_layer(
        unmasked_interpreted_water_layer, qa_band):
    """Compute cloud/cloud-shadow mask and filter interpreted water layer

       Parameters
       ----------
       unmasked_interpreted_water_layer: numpy.ndarray
              Cloud-unmasked interpreted water layer
       qa_band: numpy ndarray
              HLS Q/A band

       Returns
       -------
       masked_interpreted_water_layer : numpy.ndarray
              Cloud-masked interpreted water layer
    """
    shape = unmasked_interpreted_water_layer.shape
    masked_interpreted_water_layer = unmasked_interpreted_water_layer.copy()
    mask = np.zeros(shape, dtype = np.uint8)

    '''
    QA band - Landsat 8
    BITS:
    0 - Cirrus (reserved but not used)
    1 - Cloud (*)
    2 - Adjacent to cloud/shadow
    3 - Cloud shadow (*)
    4 - Snow/ice (*)
    5 - Water
    6-7 - Aerosol quality:
          00 - Climatology aerosol
          01 - Low aerosol
          10 - Moderate aerosol
          11 - High aerosol

    (*) set output as 9
    '''

    for i in range(shape[0]):
        for j in range(shape[1]):

            # Check QA cloud shadow bit (3) => bit 0
            if np.bitwise_and(2**3, qa_band[i, j]):
                mask[i, j] += 1

            # Check QA cloud bit (1) => bit 2
            if np.bitwise_and(2**1, qa_band[i, j]):
                mask[i, j] += 4

            if mask[i, j] != 0:
                masked_interpreted_water_layer[i, j] = WTR_CLOUD_MASKED

            # Check QA snow bit (4) => bit 1
            if np.bitwise_and(2**4, qa_band[i, j]):
                mask[i, j] += 2
                masked_interpreted_water_layer[i, j] = 0

    return mask, masked_interpreted_water_layer


def _get_avg_sensing_time(sensing_time_str):
    """
       Compute average sensing time

       Parameters
       ----------
       sensing_time_str: str
              String containing the list of sensing times separated by ";"

       Returns
       -------
       average_sensing_time_string: str
              Average sensing time
    """
    sensing_time_list = [d.strip() for d in
                         sensing_time_str.split(';')]

    if len(sensing_time_list) == 1:
        return sensing_time_list[0]

    timestamp_sum = 0
    for sensing_time in sensing_time_list:
        # datetime parses microseconds but not nanoseconds
        sensing_time_splitted = sensing_time.split('.')
        sensing_time_splitted[1] = sensing_time_splitted[1][0:6]
        sensing_time_microseconds = '.'.join(
            sensing_time_splitted)+'Z'
        dt_object = datetime.datetime.strptime(
            sensing_time_microseconds, "%Y-%m-%dT%H:%M:%S.%fZ")
        timestamp_sum += dt_object.timestamp()
    timestamp_avg = timestamp_sum / len(sensing_time_list)
    datetime_avg = datetime.datetime.fromtimestamp(timestamp_avg)
    datetime_avg_str = datetime_avg.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return datetime_avg_str


def _load_hls_from_file(filename, image_dict, offset_dict, scale_dict,
                        dswx_metadata_dict, key,
                        flag_offset_and_scale_inputs, flag_debug = False,
                        band_suffix = None):
    """Load HLS band from file into memory

       Parameters
       ----------
       filename: str
              Filename containing HLS band
       image_dict: dict
              Image dictionary that will store HLS band array
       offset_dict: dict
              Offset dictionary that will store band offset
       scale_dict: dict
              Scale dictionary that will store band scaling factor
       dswx_metadata_dict: dict
              Metadata dictionary that will store band metadata
       key: str
              Name of the band (e.g., "blue", "green", "swir1", etc)
       flag_offset_and_scale_inputs: bool
              Flag to indicate if the band should be offseted and scaled
       flag_debug: bool (optional)
              Flag to indicate if execution is for debug purposes. If so,
              only a subset of the image will be loaded into memory
       band_suffix: str (optional)
              Indicate band suffix that should be removed from file
              name to extract band name

       Returns
       -------
       flag_success : bool
              Flag indicating if band was successfuly loaded into memory
    """
    layer_gdal_dataset = gdal.Open(filename, gdal.GA_ReadOnly)
    if layer_gdal_dataset is None:
        return None

    if 'hls_dataset_name' not in image_dict.keys():
        hls_dataset_name = os.path.splitext(os.path.basename(filename))[0]
        if band_suffix:
            hls_dataset_name = hls_dataset_name.replace(f'.{band_suffix}', '')
        image_dict['hls_dataset_name'] = hls_dataset_name

    offset = 0.0
    scale_factor = 1.

    metadata = layer_gdal_dataset.GetMetadata()

    if 'SPACECRAFT_NAME' not in dswx_metadata_dict.keys():
        for k, v in metadata.items():
            if k.upper() in METADATA_FIELDS_TO_COPY_FROM_HLS_LIST:
                dswx_metadata_dict[k.upper()] = v
            elif (k.upper() == 'LANDSAT_PRODUCT_ID' or
                    k.upper() == 'PRODUCT_URI'):
                dswx_metadata_dict['SENSOR_PRODUCT_ID'] = v
            elif (k.upper() == 'SENSING_TIME' and
                    FLAG_COMPUTE_AVERAGE_SENSING_TIME):
                dswx_metadata_dict['SENSING_TIME'] = \
                    _get_avg_sensing_time(v)
            elif k.upper() == 'SENSING_TIME':
                dswx_metadata_dict['SENSING_TIME'] = v

        sensor = None

        # HLS Sentinel metadata contain attribute SPACECRAFT_NAME
        if 'SPACECRAFT_NAME' in metadata:
            spacecraft_name = metadata['SPACECRAFT_NAME'].upper()
            if 'SENTINEL' not in spacecraft_name and 'LANDSAT' not in spacecraft_name:
                logger.info(f'ERROR the platform "{spacecraft_name}" is not supported')
                return False

        # HLS Landsat metadata contain attribute SENSOR
        elif 'SENSOR' in metadata:
            sensor = metadata['SENSOR']
            if ('OLI' in sensor and
                    'SENSOR_PRODUCT_ID' in dswx_metadata_dict.keys() and
                    'LC' in dswx_metadata_dict['SENSOR_PRODUCT_ID']):
                sensor_product_id = dswx_metadata_dict['SENSOR_PRODUCT_ID']
                landsat_sat_num_index = sensor_product_id.find('LC')
                landsat_sat_num = int(sensor_product_id[
                    landsat_sat_num_index+2:landsat_sat_num_index+4])
                spacecraft_name = f'Landsat-{landsat_sat_num}'
            else:
                logger.info(f'ERROR the sensor "{sensor}" is not supported')
                return False

        # Otherwise, could not find HLS Sentinel or Landsat metadata
        else:
            logger.info('ERROR could not determine the platorm from metadata')
            return False

        dswx_metadata_dict['SPACECRAFT_NAME'] = spacecraft_name
        if sensor is not None:
            # Sensor may be in the form: "OLI_TIRS; OLI_TIRS"
            sensor_name = None
            flag_all_same = True
            for s in sensor.split(';'):
                current_sensor_name = s.strip()
                if sensor_name is None:
                    sensor_name = current_sensor_name
                    continue
                if sensor_name != current_sensor_name:
                    flag_all_same = False
                    break
            if flag_all_same:
                dswx_metadata_dict['SENSOR'] = sensor_name
            else:
                dswx_metadata_dict['SENSOR'] = sensor
        elif 'SENTINEL' in spacecraft_name:
            dswx_metadata_dict['SENSOR'] = 'MSI'
        else:
            dswx_metadata_dict['SENSOR'] = 'OLI_TIRS'

    if key == 'qa':
        if flag_debug:
            logger.info('reading in debug mode')
            image_dict[key] = layer_gdal_dataset.ReadAsArray(
                xoff=0, yoff=0, xsize=1000, ysize=1000)
        else:
            image_dict[key] = layer_gdal_dataset.ReadAsArray()
    else:
        for metadata_key, metadata_value in metadata.items():
            if metadata_key == 'add_offset':
                offset = float(metadata_value)
            elif metadata_key == 'scale_factor':
               scale_factor = float(metadata_value)
        if flag_debug:
            logger.info('reading in debug mode')
            image = layer_gdal_dataset.ReadAsArray(
                xoff=0, yoff=0, xsize=1000, ysize=1000)
        else:
            image = layer_gdal_dataset.ReadAsArray()
        if FLAG_CLIP_NEGATIVE_REFLECTANCE:
            image = np.clip(image, 1, None)
        if flag_offset_and_scale_inputs:
            image = scale_factor * (np.asarray(image, dtype=np.float32) -
                                    offset)
        image_dict[key] = image

    # save offset and scale factor into corresponding dictionaries
    offset_dict[key] = offset
    scale_dict[key] = scale_factor

    if 'geotransform' not in image_dict.keys():
        image_dict['geotransform'] = \
            layer_gdal_dataset.GetGeoTransform()
        image_dict['projection'] = \
            layer_gdal_dataset.GetProjection()
        band = layer_gdal_dataset.GetRasterBand(1)
        image_dict['fill_data'] = band.GetNoDataValue()
        image_dict['length'] = image_dict[key].shape[0]
        image_dict['width'] = image_dict[key].shape[1]

    return True


def _load_hls_product_v1(filename, image_dict, offset_dict,
                         scale_dict, dswx_metadata_dict,
                         flag_offset_and_scale_inputs,
                         flag_debug = False):
    """Load a HLS (v.1) product (all required bands) from file
       into memory

       Parameters
       ----------
       filename: str
              Filename containing HLS product
       image_dict: dict
              Image dictionary that will store HLS product's arrays
       offset_dict: dict
              Offset dictionary that will store product's offsets
       scale_dict: dict
              Scale dictionary that will store product's scaling factor
       dswx_metadata_dict: dict
              Metadata dictionary that will store product's metadata
       flag_offset_and_scale_inputs: bool
              Flag to indicate if bands should be offseted and scaled
       flag_debug (optional)
              Flag to indicate if execution is for debug purposes. If so,
              only a subset of the product will be loaded into memory

       Returns
       -------
       flag_success : bool
              Flag indicating if band was successfuly loaded into memory
    """
    if isinstance(filename, list):
        filename = filename[0]

    logger.info('loading HLS v.1.x layers:')
    for key in l30_v1_band_dict.keys():

        logger.info(f'    {key}')

        # Sensor is undertermined (first band) or LANDSAT
        if ('SPACECRAFT_NAME' not in dswx_metadata_dict.keys() or
                'LANDSAT' in dswx_metadata_dict['SPACECRAFT_NAME'].upper()):
            band_name = l30_v1_band_dict[key]
        else:
            band_name = s30_v1_band_dict[key]

        band_ref = f'HDF4_EOS:EOS_GRID:"{filename}":Grid:{band_name}'
        success = _load_hls_from_file(band_ref, image_dict, offset_dict,
                                      scale_dict, dswx_metadata_dict,
                                      key, flag_offset_and_scale_inputs,
                                      flag_debug = flag_debug)
        if not success:
            return False

    return True


def _load_hls_product_v2(file_list, image_dict, offset_dict,
                         scale_dict, dswx_metadata_dict,
                         flag_offset_and_scale_inputs, flag_debug = False):
    """Load a HLS (v.2) product (all required bands) from a list of files
       into memory

       Parameters
       ----------
       file_list: str
              File list containing HLS product
       image_dict: dict
              Image dictionary that will store HLS product's arrays
       offset_dict: dict
              Offset dictionary that will store product's offsets
       scale_dict: dict
              Scale dictionary that will store product's scaling factor
       dswx_metadata_dict: dict
              Metadata dictionary that will store product's metadata
       flag_offset_and_scale_inputs: bool
              Flag to indicate if bands should be offseted and scaled
       flag_debug (optional)
              Flag to indicate if execution is for debug purposes. If so,
              only a subset of the product will be loaded into memory

       Returns
       -------
       flag_success : bool
              Flag indicating if band was successfuly loaded into memory
    """
    logger.info('loading HLS v.2.0 layers:')
    for key in l30_v2_band_dict.keys():

        logger.info(f'    {key}')

        # Sensor is undertermined (first band) or LANDSAT
        if ('SPACECRAFT_NAME' not in dswx_metadata_dict.keys() or
                'LANDSAT' in dswx_metadata_dict['SPACECRAFT_NAME'].upper()):
            band_name = l30_v2_band_dict[key]
        else:
            band_name = s30_v2_band_dict[key]

        for filename in file_list:
            if band_name + '.tif' in filename:
                break
        else:
            logger.info(f'ERROR band {key} not found within list of input'
                        ' file(s)')
            return
        success = _load_hls_from_file(filename, image_dict, offset_dict,
                                      scale_dict, dswx_metadata_dict,
                                      key, flag_offset_and_scale_inputs,
                                      flag_debug = flag_debug,
                                      band_suffix = band_name)
        if not success:
            return False

    return True

def _get_binary_mask_ctable():
    """
       Get binary mask RGB color table

       Returns
       -------
       binary_mask_ctable : gdal.ColorTable
              Binary mask RGB color table
    """
    # create color table
    binary_mask_ctable = gdal.ColorTable()
    # Masked
    binary_mask_ctable.SetColorEntry(SHAD_MASKED, (64, 64, 64))
    # Not masked
    binary_mask_ctable.SetColorEntry(SHAD_NOT_MASKED, (255, 255, 255))
    # Black - Fill value
    binary_mask_ctable.SetColorEntry(UINT8_FILL_VALUE, (0, 0, 0, 255))
    return binary_mask_ctable


def _get_binary_water_ctable():
    """Get binary water RGB color table

       Returns
       -------
       binary_water_ctable : gdal.ColorTable
              Binary water RGB color table
    """
    # create color table
    binary_water_ctable = gdal.ColorTable()
    # No water
    binary_water_ctable.SetColorEntry(WTR_NOT_WATER, (255, 255, 255))
    # Water
    binary_water_ctable.SetColorEntry(BWTR_WATER, (0, 0, 255))
    # Gray - QA masked
    binary_water_ctable.SetColorEntry(WTR_CLOUD_MASKED, (127, 127, 127))
    # Black - Fill value
    binary_water_ctable.SetColorEntry(UINT8_FILL_VALUE, (0, 0, 0, 255))
    return binary_water_ctable


def _get_confidence_layer_ctable():
    """
       Get confidence layer RGB color table

       Returns
       -------
       confidence_layer_ctable : gdal.ColorTable
              Confidence layer color table
    """
    # create color table
    confidence_layer_ctable = gdal.ColorTable()
    
    # color gradient from white to blue
    for conf_value in range(101):
        conf_value_255 = int(float(conf_value) * 255 // 100)
        confidence_layer_ctable.SetColorEntry(
            conf_value, (255 - conf_value_255,
                         255 - conf_value_255,
                         255))

    # White - Not water
    confidence_layer_ctable.SetColorEntry(CONF_NOT_WATER, (255, 255, 255))

    # Gray - QA masked
    confidence_layer_ctable.SetColorEntry(CONF_CLOUD_MASKED, (127, 127, 127))

    # Black - Fill value
    confidence_layer_ctable.SetColorEntry(UINT8_FILL_VALUE, (0, 0, 0, 255))
    return confidence_layer_ctable

def _collapse_wtr_classes(interpreted_layer):
    """
       Collapse interpreted layer classes onto final DSWx-HLS
        product WTR classes

       Parameters
       ----------
       interpreted_layer: np.ndarray
              Interpreted layer

       Returns
       -------
       collapsed_interpreted_layer: np.ndarray
              Interpreted layer with collapsed classes
    """
    collapsed_interpreted_layer = np.full_like(interpreted_layer,
                                               UINT8_FILL_VALUE)
    for original_value, new_value in collapse_wtr_classes_dict.items():
        collapsed_interpreted_layer[interpreted_layer == original_value] = \
            new_value
    return collapsed_interpreted_layer


def save_dswx_product(wtr, output_file, dswx_metadata_dict, geotransform,
                      projection, scratch_dir='.', output_files_list = None,
                      description = None,
                      flag_collapse_wtr_classes = FLAG_COLLAPSE_WTR_CLASSES,
                      **dswx_processed_bands):
    """Save DSWx-HLS product

       Parameters
       ----------
       wtr: numpy.ndarray
              Water classification layer WTR
       output_file: str
              Output filename
       dswx_metadata_dict: dict
              Metadata dictionary to be written into the DSWx-HLS product
       geotransform: numpy.ndarray
              Geotransform describing the DSWx-HLS product geolocation
       projection: str
              DSWx-HLS product's projection
       scratch_dir: str (optional)
              Directory for temporary files
       output_files_list: list (optional)
              Mutable list of output files
       description: str (optional)
              Band description
       flag_collapse_wtr_classes: bool
              Collapse interpreted layer water classes following standard
              DSWx-HLS product water classes
       **dswx_processed_bands: dict
              Remaining bands to be included into the DSWx-HLS product
    """
    _makedirs(output_file)
    shape = wtr.shape
    driver = gdal.GetDriverByName("GTiff")

    dswx_processed_bands['wtr'] = wtr

    # translate dswx_processed_bands_keys to band_description_dict keys
    # example: wtr_1 to WTR-1
    dswx_processed_bands_keys = list(dswx_processed_bands.keys())
    dswx_processed_band_names_list = []
    for dswx_processed_bands_key in dswx_processed_bands_keys:
        dswx_processed_band_names_list.append(
            dswx_processed_bands_key.upper().replace('_', '-'))

    # check input arrays different than None
    n_valid_bands = 0
    band_description_dict_keys = list(band_description_dict.keys())
    for i, band_name in enumerate(dswx_processed_band_names_list):
        if band_name not in band_description_dict_keys:
            continue
        if dswx_processed_bands[dswx_processed_bands_keys[i]] is None:
            continue
        n_valid_bands += 1

    if n_valid_bands == 1:
        # save interpreted layer (single band)
        nbands = 1
    else:
        # save DSWx product
        nbands = len(band_description_dict.keys())

    gdal_ds = driver.Create(output_file, shape[1], shape[0], nbands, gdal.GDT_Byte)
    gdal_ds.SetMetadata(dswx_metadata_dict)
    gdal_ds.SetGeoTransform(geotransform)
    gdal_ds.SetProjection(projection)

    for band_index, (band_name, description_from_dict) in enumerate(
            band_description_dict.items()):

        # check if band is in the list of processed bands
        if band_name in dswx_processed_band_names_list:

            # index using processed key from band name (e.g., WTR-1 to wtr_1)
            band_array = dswx_processed_bands[
                band_name.replace('-', '_').lower()]
        else:
            logger.warning(f'layer not found "{band_name}".')
            band_array = None

        # if band is not in the list of processed bands or it's None
        if band_array is None:
            band_array = np.zeros_like(wtr)

        gdal_band = gdal_ds.GetRasterBand(band_index + 1)

        if band_name in collapsable_layers_list and flag_collapse_wtr_classes:
            band_array = _collapse_wtr_classes(band_array)

        gdal_band.WriteArray(band_array)
        gdal_band.SetNoDataValue(UINT8_FILL_VALUE)
        if n_valid_bands == 1:
            # set color table and color interpretation
            dswx_ctable = _get_interpreted_dswx_ctable(flag_collapse_wtr_classes)
            gdal_band.SetRasterColorTable(dswx_ctable)
            gdal_band.SetRasterColorInterpretation(
                gdal.GCI_PaletteIndex)
        if description is not None:
            gdal_band.SetDescription(description)
        else:
            gdal_band.SetDescription(description_from_dict)

        gdal_band.FlushCache()
        gdal_band = None
        if n_valid_bands == 1:
            break

    gdal_ds.FlushCache()
    gdal_ds = None

    save_as_cog(output_file, scratch_dir, logger)

    if output_files_list is not None:
        output_files_list.append(output_file)
    logger.info(f'file saved: {output_file}')


def geotiff2png(src_geotiff_filename,
                dest_png_filename,
                output_height=-1,
                output_width=-1,
                scratch_dir='.',
                logger=None,
                ):
    """
    Convert a GeoTIFF file to a png file.

    Parameters
    ----------
    src_geotiff_filename : str
        Name (with path) of the source geotiff file to be 
        converted. This file must already exist.
    dest_png_filename : str
        Name (with path) for the output .png file
    output_height : int, optional.
        Height in Pixels for the output png. If not provided, 
        will default to the height of the source geotiff.
    output_width : int, optional.
        Width in Pixels for the output png. If not provided, 
        will default to the width of the source geotiff.
    scratch_dir: str, optional
        Temporary Directory
    logger : Logger, optional
        Logger for the project

    """
    if logger is None:
        logger = logging.getLogger('proteus')

    # logger.info('COG step 1: add overviews')

    # Load the source dataset
    gdal_ds = gdal.Open(src_geotiff_filename, 1)

    # select the resampling algorithm to use based on dtype
    gdal_dtype = gdal_ds.GetRasterBand(1).DataType
    dtype_name = gdal.GetDataTypeName(gdal_dtype).lower()
    is_integer = 'byte' in dtype_name  or 'int' in dtype_name

    if is_integer:
        resamp_algorithm = 'NEAREST'
    else:
        resamp_algorithm = 'CUBICSPLINE'

    del gdal_ds  # close the dataset (Python object and pointers)

    # Do not output the .aux.xml file alongside the PNG
    gdal.SetConfigOption('GDAL_PAM_ENABLED', 'NO')

    # Translate the existing geotiff to the .png format
    gdal.Translate(dest_png_filename, 
                        src_geotiff_filename, 
                        format='PNG',
                        height=output_height,
                        width=output_width,
                        resampleAlg=resamp_algorithm,
                        nogcp=True,  # do not print GCPs
                        )


def save_cloud_mask(mask, output_file, dswx_metadata_dict, geotransform, projection,
                    description = None, scratch_dir = '.', output_files_list = None):
    """Save DSWx-HLS cloud/cloud-mask layer

       Parameters
       ----------
       mask: numpy.ndarray
              Cloud/cloud-shadow layer
       output_file: str
              Output filename
       dswx_metadata_dict: dict
              Metadata dictionary to be written into the output file
       geotransform: numpy.ndarray
              Geotransform describing the output file geolocation
       projection: str
              Output file's projection
       description: str (optional)
              Band description
       scratch_dir: str (optional)
              Temporary directory
       output_files_list: list (optional)
              Mutable list of output files
    """
    _makedirs(output_file)
    shape = mask.shape
    driver = gdal.GetDriverByName("GTiff")
    gdal_ds = driver.Create(output_file, shape[1], shape[0], 1, gdal.GDT_Byte)
    gdal_ds.SetMetadata(dswx_metadata_dict)
    gdal_ds.SetGeoTransform(geotransform)
    gdal_ds.SetProjection(projection)
    mask_band = gdal_ds.GetRasterBand(1)
    mask_band.WriteArray(mask)
    mask_band.SetNoDataValue(UINT8_FILL_VALUE)

    # set color table and color interpretation
    mask_ctable = _get_cloud_mask_ctable()
    mask_band.SetRasterColorTable(mask_ctable)
    mask_band.SetRasterColorInterpretation(
        gdal.GCI_PaletteIndex)

    if description is not None:
        mask_band.SetDescription(description)

    gdal_ds.FlushCache()
    gdal_ds = None

    save_as_cog(output_file, scratch_dir, logger)

    if output_files_list is not None:
        output_files_list.append(output_file)
    logger.info(f'file saved: {output_file}')


def _save_binary_water(binary_water_layer, output_file, dswx_metadata_dict,
                       geotransform, projection, description = None,
                       scratch_dir = '.', output_files_list = None):
    """Save DSWx-HLS binary water layer

       Parameters
       ----------
       binary_water_layer: numpy.ndarray
              Binary water layer
       output_file: str
              Output filename
       dswx_metadata_dict: dict
              Metadata dictionary to be written into the output file
       geotransform: numpy.ndarray
              Geotransform describing the output file geolocation
       projection: str
              Output file's projection
       description: str (optional)
              Band description
       scratch_dir: str (optional)
              Temporary directory
       output_files_list: list (optional)
              Mutable list of output files
    """
    _makedirs(output_file)
    shape = binary_water_layer.shape
    driver = gdal.GetDriverByName("GTiff")
    gdal_ds = driver.Create(output_file, shape[1], shape[0], 1, gdal.GDT_Byte)
    gdal_ds.SetMetadata(dswx_metadata_dict)
    gdal_ds.SetGeoTransform(geotransform)
    gdal_ds.SetProjection(projection)
    binary_water_band = gdal_ds.GetRasterBand(1)
    binary_water_band.WriteArray(binary_water_layer)
    binary_water_band.SetNoDataValue(UINT8_FILL_VALUE)

    # set color table and color interpretation
    binary_water_ctable = _get_binary_water_ctable()
    binary_water_band.SetRasterColorTable(binary_water_ctable)
    binary_water_band.SetRasterColorInterpretation(
        gdal.GCI_PaletteIndex)

    if description is not None:
        binary_water_band.SetDescription(description)

    gdal_ds.FlushCache()
    gdal_ds = None

    save_as_cog(output_file, scratch_dir, logger)

    if output_files_list is not None:
        output_files_list.append(output_file)
    logger.info(f'file saved: {output_file}')


def _save_array(input_array, output_file, dswx_metadata_dict, geotransform,
                projection, description = None, scratch_dir = '.',
                output_files_list = None, output_dtype = gdal.GDT_Byte,
                ctable = None, no_data_value = None):
    """Save a generic DSWx-HLS layer (e.g., diagnostic layer, shadow layer, etc.)

       Parameters
       ----------
       input_array: numpy.ndarray
              DSWx-HLS layer to be saved
       output_file: str
              Output filename
       dswx_metadata_dict: dict
              Metadata dictionary to be written into the output file
       geotransform: numpy.ndarray
              Geotransform describing the output file geolocation
       projection: str
              Output file's projection
       description: str (optional)
              Band description
       scratch_dir: str (optional)
              Temporary directory
       output_files_list: list (optional)
              Mutable list of output files
       output_dtype: gdal.DataType
              GDAL data type
       ctable: GDAL ColorTable object
              GDAL ColorTable object
       no_data_value: numeric
              No data value
    """
    _makedirs(output_file)
    shape = input_array.shape
    driver = gdal.GetDriverByName("GTiff")
    gdal_ds = driver.Create(output_file, shape[1], shape[0], 1, output_dtype)
    if dswx_metadata_dict is not None:
        gdal_ds.SetMetadata(dswx_metadata_dict)
    gdal_ds.SetGeoTransform(geotransform)
    gdal_ds.SetProjection(projection)
    raster_band = gdal_ds.GetRasterBand(1)
    raster_band.WriteArray(input_array)
    if no_data_value is not None:
        raster_band.SetNoDataValue(no_data_value)

    if description is not None:
        raster_band.SetDescription(description)

    if ctable is not None:
        raster_band.SetRasterColorTable(ctable)
        raster_band.SetRasterColorInterpretation(
                gdal.GCI_PaletteIndex)

    gdal_ds.FlushCache()
    gdal_ds = None

    save_as_cog(output_file, scratch_dir, logger)

    if output_files_list is not None:
        output_files_list.append(output_file)
    logger.info(f'file saved: {output_file}')

def _makedirs(input_file):
    output_dir = os.path.dirname(input_file)
    if not output_dir:
        return
    os.makedirs(output_dir, exist_ok=True)


def _save_output_rgb_file(red, green, blue, output_file,
                          offset_dict, scale_dict,
                          flag_offset_and_scale_inputs,
                          dswx_metadata_dict,
                          geotransform, projection,
                          invalid_ind = None, scratch_dir='.',
                          output_files_list = None,
                          flag_infrared = False):
    """Save the a three-band reflectance-layer (RGB or infrared RGB) GeoTIFF

       Parameters
       ----------
       red: numpy.ndarray
              Red reflectance layer
       green: numpy.ndarray
              Green reflectance layer
       blue: numpy.ndarray
              Blue reflectance layer
       output_file: str
              Output filename
       offset_dict: dict
              Offset dictionary that stores band offsets
       scale_dict: dict
              Scale dictionary that stores bands scaling factor
       flag_offset_and_scale_inputs: bool
              Flag to indicate if the band has been already offseted and scaled
       dswx_metadata_dict: dict
              Metadata dictionary to be written into the output file
       geotransform: numpy.ndarray
              Geotransform describing the output file geolocation
       projection: str
              Output file's projection
       invalid_ind: list
              List of invalid indices to be set to NaN
       output_files_list: list (optional)
              Mutable list of output files
       scratch_dir: str (optional)
              Temporary directory
       flag_infrared: bool
              Flag to indicate if layer represents infrared reflectance,
              i.e., Red, NIR, and SWIR-1
    """
    _makedirs(output_file)
    shape = blue.shape
    driver = gdal.GetDriverByName("GTiff")
    gdal_dtype = GDT_Float32
    gdal_ds = driver.Create(output_file, shape[1], shape[0], 3, gdal_dtype)
    gdal_ds.SetMetadata(dswx_metadata_dict)
    gdal_ds.SetGeoTransform(geotransform)
    gdal_ds.SetProjection(projection)

    # HLS images were not yet corrected for offset and scale factor
    if not flag_offset_and_scale_inputs:

        if not flag_infrared:
            red_key = 'red'
            green_key = 'green'
            blue_key = 'blue'
        else:
            red_key = 'swir1'
            green_key = 'nir'
            blue_key = 'red'

        red = scale_dict[red_key] * (np.asarray(red, dtype=np.float32) -
                                   offset_dict[red_key])

        green = scale_dict[green_key] * (np.asarray(green, dtype=np.float32) -
                                       offset_dict[green_key])

        blue = scale_dict[blue_key] * (np.asarray(blue, dtype=np.float32) -
                                     offset_dict[blue_key])

    if invalid_ind is not None:
        red[invalid_ind] = np.nan
        green[invalid_ind] = np.nan
        blue[invalid_ind] = np.nan

    # Save red band
    gdal_ds.GetRasterBand(1).WriteArray(red)

    # Save green band
    gdal_ds.GetRasterBand(2).WriteArray(green)

    # Save blue band
    gdal_ds.GetRasterBand(3).WriteArray(blue)

    gdal_ds.FlushCache()
    gdal_ds = None

    save_as_cog(output_file, scratch_dir, logger)

    if output_files_list is not None:
        output_files_list.append(output_file)
    logger.info(f'file saved: {output_file}')


def get_projection_proj4(projection):
    """Return projection in proj4 format

       projection : str
              Projection

       Returns
       -------
       projection_proj4 : str
              Projection in proj4 format
    """
    srs = osr.SpatialReference()
    if projection.upper() == 'WGS84':
        srs.SetWellKnownGeogCS(projection)
    else:
        srs.ImportFromProj4(projection)
    projection_proj4 = srs.ExportToProj4()
    projection_proj4 = projection_proj4.strip()
    return projection_proj4


def _warp(input_file, geotransform, projection,
              length, width, scratch_dir = '.',
              resample_algorithm='nearest',
              relocated_file=None, margin_in_pixels=0,
              temp_files_list = None):
    """Relocate/reproject a file (e.g., landcover or DEM) based on geolocation
       defined by a geotransform, output dimensions (length and width)
       and projection

       Parameters
       ----------
       input_file: str
              Input filename
       geotransform: numpy.ndarray
              Geotransform describing the output file geolocation
       projection: str
              Output file's projection
       length: int
              Output length before adding the margin defined by
              `margin_in_pixels`
       width: int
              Output width before adding the margin defined by
              `margin_in_pixels`
       scratch_dir: str (optional)
              Temporary directory
       resample_algorithm: str
              Resample algorithm
       relocated_file: str
              Relocated file (output file)
       margin_in_pixels: int
              Margin in pixels (default: 0)
       temp_files_list: list (optional)
              Mutable list of temporary files

       Returns
       -------
       relocated_array : numpy.ndarray
              Relocated array
    """

    # Pixel spacing
    dy = geotransform[5]
    dx = geotransform[1]

    # Output Y-coordinate start (North) position with margin
    y0 = geotransform[3] - margin_in_pixels * dy

    # Output X-coordinate start (West) position with margin
    x0 = geotransform[0] - margin_in_pixels * dx

    # Output Y-coordinate end (South) position with margin
    yf = y0 + (length + 2 * margin_in_pixels) * dy

    # Output X-coordinate end (East) position with margin
    xf = x0 + (width + 2 * margin_in_pixels) * dx

    # Set output spatial reference system (SRS) from projection
    dstSRS = get_projection_proj4(projection)

    if relocated_file is None:
        relocated_file = tempfile.NamedTemporaryFile(
                    dir=scratch_dir, suffix='.tif').name
        logger.info(f'relocating file: {input_file} to'
                    f' temporary file: {relocated_file}')
        if temp_files_list is not None:
            temp_files_list.append(relocated_file)
    else:
        logger.info(f'relocating file: {input_file} to'
                    f' file: {relocated_file}')

    _makedirs(relocated_file)

    gdal.Warp(relocated_file, input_file, format='GTiff',
              dstSRS=dstSRS,
              outputBounds=[x0, yf, xf, y0], multithread=True,
              xRes=dx, yRes=abs(dy), resampleAlg=resample_algorithm,
              errorThreshold=0)

    gdal_ds = gdal.Open(relocated_file, gdal.GA_ReadOnly)
    relocated_array = gdal_ds.ReadAsArray()
    del gdal_ds

    return relocated_array

def _deep_update(main_dict, update_dict):
    """Update input dictionary with a second (update) dictionary
    https://stackoverflow.com/questions/3232943/update-value-of-a-nested-dictionary-of-varying-depth

       Parameters
       ----------
       main_dict: dict
              Input dictionary
       update_dict: dict
              Update dictionary

       Returns
       -------
       updated_dict : dict
              Updated dictionary
    """
    for key, val in update_dict.items():
        if isinstance(val, dict):
            main_dict[key] = _deep_update(main_dict.get(key, {}), val)
        else:
            main_dict[key] = val

    # return updated main_dict
    return main_dict


def parse_runconfig_file(user_runconfig_file = None, args = None):
    """
    Parse run configuration file updating an argument
    (argparse.Namespace) and an HlsThresholds object

       Parameters
       ----------
       user_runconfig_file: str (optional)
              Run configuration (runconfig) filename
       args: argparse.Namespace (optional)
              Argument object
    """
    bin_dirname = os.path.dirname(__file__)
    source_dirname = os.path.split(bin_dirname)[0]
    default_runconfig_file = f'{source_dirname}/proteus/defaults/dswx_hls.yaml'

    logger.info(f'Default runconfig file: {default_runconfig_file}')

    yaml_schema = f'{source_dirname}/proteus/schemas/dswx_hls.yaml'
    logger.info(f'YAML schema: {yaml_schema}')

    schema = yamale.make_schema(yaml_schema, parser='ruamel')

    # parse default config
    parser = ruamel_yaml(typ='safe')
    with open(default_runconfig_file, 'r') as f:
        default_runconfig = parser.load(f)

    if user_runconfig_file is not None:
        if not os.path.isfile(user_runconfig_file):
            error_msg = f'ERROR invalid file {user_runconfig_file}'
            logger.info(error_msg)
            raise Exception(error_msg)

        logger.info(f'Input runconfig file: {user_runconfig_file}')

        data = yamale.make_data(user_runconfig_file, parser='ruamel')

        logger.info(f'Validating runconfig file: {user_runconfig_file}')
        yamale.validate(schema, data)

        # parse user config
        with open(user_runconfig_file) as f_yaml:
            user_runconfig = parser.load(f_yaml)

        # copy user suppiled config into default config
        runconfig = _deep_update(default_runconfig, user_runconfig)

    else:
        runconfig = default_runconfig

    hls_thresholds = HlsThresholds()
    hls_thresholds_user = runconfig['runconfig']['groups']['hls_thresholds']

    # copy runconfig parameters from dictionary
    if hls_thresholds_user is not None:
        logger.info('HLS thresholds:')
        for key in hls_thresholds_user.keys():
            logger.info(f'     {key}: {hls_thresholds_user[key]}')
            hls_thresholds.__setattr__(key, hls_thresholds_user[key])

    if args is None:
        return hls_thresholds

    input_file_path = runconfig['runconfig']['groups']['input_file_group'][
        'input_file_path']

    ancillary_ds_group = runconfig['runconfig']['groups'][
        'dynamic_ancillary_file_group']

    product_path_group = runconfig['runconfig']['groups'][
        'product_path_group']

    browse_image_group = runconfig['runconfig']['groups'][
        'browse_image_group']

    processing_group = runconfig['runconfig']['groups']['processing']

    dem_file = ancillary_ds_group['dem_file']
    dem_description = ancillary_ds_group['dem_description']
    landcover_file = ancillary_ds_group['landcover_file']
    landcover_description = ancillary_ds_group['landcover_description']
    worldcover_file = ancillary_ds_group['worldcover_file']
    worldcover_description = ancillary_ds_group['worldcover_description']
    scratch_dir = product_path_group['scratch_path']
    output_directory = product_path_group['output_dir']
    product_id = product_path_group['product_id']
    browse_image_height = browse_image_group['browse_image_height']
    browse_image_width = browse_image_group['browse_image_width']

    if (input_file_path is not None and len(input_file_path) == 1 and
            os.path.isdir(input_file_path[0])):
        logger.info(f'input HLS files directory: {input_file_path[0]}')
        input_list = glob.glob(os.path.join(input_file_path[0], '*.tif'))
        args.input_list = input_list
    elif input_file_path is not None:
        input_list = input_file_path
        args.input_list = input_list

    # update args with runconfig parameters listed below
    variables_to_update_dict = {
        'dem_file': dem_file,
        'dem_description': dem_description,
        'landcover_file': landcover_file,
        'landcover_description': landcover_description,
        'worldcover_file': worldcover_file,
        'worldcover_description': worldcover_description,
        'scratch_dir': scratch_dir,
        'product_id': product_id,
        'browse_image_height': browse_image_height,
        'browse_image_width': browse_image_width
        }

    for var_name, runconfig_file in variables_to_update_dict.items():
        user_file = getattr(args, var_name)
        if user_file is not None and runconfig_file is not None:
            logger.warning(f'command line {var_name} "{user_file}"'
                f' has precedence over runconfig {var_name}'
                f' "{runconfig_file}".')
        elif user_file is None:
            setattr(args, var_name, runconfig_file)

    # If user runconfig was not provided, return
    if user_runconfig_file is None:
        return hls_thresholds

    # Save layers
    if product_id is None:
        product_id = 'dswx_hls'

    args.flag_use_otsu_terrain_masking = \
        processing_group['flag_use_otsu_terrain_masking']
    args.min_slope_angle = processing_group['min_slope_angle']
    args.max_sun_local_inc_angle = processing_group['max_sun_local_inc_angle']

    for i, (layer_name, args_name) in \
            enumerate(layer_names_to_args_dict.items()):
        layer_number = i + 1
        layer_var_name = layer_name.lower().replace('-', '_')
        runconfig_field = f'save_{layer_var_name}'

        flag_save_layer = processing_group[runconfig_field]
        arg_name = layer_names_to_args_dict[layer_name]

        # user (command-line interface) layer filename
        print("%%%%%%%%%%", args)
        print("arg_name: ", arg_name)

        user_layer_file = getattr(args, arg_name)

        # runconfig layer filename
        product_basename = (f'{product_id}_v{PRODUCT_VERSION}_B{layer_number:02}'
                            f'_{layer_name}.tif')
        runconfig_layer_file = os.path.join(output_directory,
                                            product_basename)

        if user_layer_file is not None and flag_save_layer:
            logger.warning(f'command line {arg_name} "{user_layer_file}" has'
                           f' precedence over runconfig {arg_name}'
                           f' "{runconfig_layer_file}".')
            continue

        if user_layer_file is not None or not flag_save_layer:
            continue

        setattr(args, args_name, runconfig_layer_file)

    # Browse Image Filename
    if browse_image_group['save_browse']:
        # Get user's CLI input for the browse image filename
        cli_arg_name = 'output_browse_image'
        cli_browse_fname = getattr(args, cli_arg_name)

        # Construct the default browse image filename per the runconfig
        product_basename = (f'{product_id}_v{PRODUCT_VERSION}.png')
        default_browse_fname = os.path.join(output_directory,
                                            product_basename)

        # If a browse image filename was provided via CLI, it takes
        # precendence over the default filename.
        if cli_browse_fname is not None:
                logger.warning(f'command line {cli_arg_name} "{cli_browse_fname}" has'
                                f' precedence over default {cli_arg_name}'
                                f' "{default_browse_fname}".')
                # `args` already contains the correct filename; no need to update.
        else:
            # use the default browse filename
            setattr(args, cli_arg_name, default_browse_fname)

    return hls_thresholds


def _get_dswx_metadata_dict(product_id):
    """Create and return metadata dictionary

       Parameters
       ----------
       output_file: str
              Output filename

       Returns
       -------
       dswx_metadata_dict : collections.OrderedDict
              Metadata dictionary
    """
    dswx_metadata_dict = OrderedDict()

    # identification


    dswx_metadata_dict['PRODUCT_ID'] = product_id
    dswx_metadata_dict['PRODUCT_VERSION'] = PRODUCT_VERSION
    dswx_metadata_dict['PROJECT'] = 'OPERA'
    dswx_metadata_dict['PRODUCT_LEVEL'] = '3'
    dswx_metadata_dict['PRODUCT_TYPE'] = 'DSWx-HLS'
    dswx_metadata_dict['PRODUCT_SOURCE'] = 'HLS'

    # save datetime 'YYYY-MM-DD HH:MM:SS'
    dswx_metadata_dict['PROCESSING_DATETIME'] = \
        datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    return dswx_metadata_dict

def _populate_dswx_metadata_datasets(dswx_metadata_dict,
                                     hls_dataset,
                                     dem_file=None,
                                     dem_description=None,
                                     landcover_file=None,
                                     landcover_description=None,
                                     worldcover_file=None,
                                     worldcover_description=None):
    """Populate metadata dictionary with input files

       Parameters
       ----------
       dswx_metadata_dict : collections.OrderedDict
              Metadata dictionary
       hls_dataset: str
              HLS dataset name
       dem_file: str
              DEM filename
       dem_description: str
              DEM description
       landcover_file: str
              Landcover filename
       landcover_description: str
              Landcover description
        worldcover_file: str
              Worldcover filename
        worldcover_description: str
              Worldcover description
    """

    # input datasets
    dswx_metadata_dict['HLS_DATASET'] = hls_dataset
    if dem_description:
        dswx_metadata_dict['DEM_SOURCE'] = dem_description
    elif dem_file:
        dswx_metadata_dict['DEM_SOURCE'] = \
            os.path.basename(dem_file)
    else:
        dswx_metadata_dict['DEM_SOURCE'] = '(not provided)'

    if landcover_description:
        dswx_metadata_dict['LANDCOVER_SOURCE'] = landcover_description
    elif landcover_file:
        dswx_metadata_dict['LANDCOVER_SOURCE'] = \
            os.path.basename(landcover_file)
    else:
        dswx_metadata_dict['LANDCOVER_SOURCE'] = '(not provided)'

    if worldcover_description:
        dswx_metadata_dict['WORLDCOVER_SOURCE'] = worldcover_description
    elif worldcover_file:
        dswx_metadata_dict['WORLDCOVER_SOURCE'] = \
            os.path.basename(worldcover_file)
    else:
        dswx_metadata_dict['WORLDCOVER_SOURCE'] = '(not provided)'


class Logger(object):
    """
    Class to redirect stdout and stderr to the logger
    """
    def __init__(self, logger, level, prefix=''):
       """
       Class constructor
       """
       self.logger = logger
       self.level = level
       self.prefix = prefix
       self.buffer = ''

    def write(self, message):

        # Add message to the buffer until "\n" is found
        if '\n' not in message:
            self.buffer += message
            return

        message = self.buffer + message

        # check if there is any character after the last \n
        # if so, move it to the buffer
        message_list = message.split('\n')
        if not message.endswith('\n'):
            self.buffer = message_list[-1]
            message_list = message_list[:-1]
        else:
            self.buffer = ''

        # print all characters before the last \n
        for line in message_list:
            if not line:
                continue
            self.logger.log(self.level, self.prefix + line)

    def flush(self):
        self.logger.log(self.level, self.buffer)
        self.buffer = ''


def create_logger(log_file, full_log_formatting=None):
    """Create logger object for a log file

       Parameters
       ----------
       log_file: str
              Log file
       full_log_formatting : bool
              Flag to enable full formatting of logged messages

       Returns
       -------
       logger : logging.Logger
              Logger object
    """
    # create logger
    logger.setLevel(logging.DEBUG)

    # create console handler and set level to debug
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)

    # create formatter
    # configure full log format, if enabled
    if full_log_formatting:
        msgfmt = ('%(asctime)s.%(msecs)03d, %(levelname)s, DSWx-HLS, '
                  '%(module)s, 999999, %(pathname)s:%(lineno)d, "%(message)s"')

        formatter = logging.Formatter(msgfmt, "%Y-%m-%d %H:%M:%S")
    else:
        formatter = logging.Formatter('%(message)s')

    # add formatter to ch
    ch.setFormatter(formatter)

    # add ch to logger
    logger.addHandler(ch)

    if log_file:
        file_handler = logging.FileHandler(log_file)

        file_handler.setFormatter(formatter)

        # add file handler to logger
        logger.addHandler(file_handler)

    sys.stdout = Logger(logger, logging.INFO)
    sys.stderr = Logger(logger, logging.ERROR, prefix='[StdErr] ')

    return logger

def _compute_hillshade(dem_file, scratch_dir, sun_azimuth_angle,
                      sun_elevation_angle, temp_files_list = None):
    """Compute hillshade using GDAL's DEMProcessing() function

       Parameters
       ----------
       dem_file: str
              DEM filename
       scratch_dir: str
              Scratch directory
       sun_azimuth_angle: float
              Sun azimuth angle
       sun_elevation_angle: float
              Sun elevation angle
       temp_files_list: list (optional)
              Mutable list of temporary files

       Returns
       -------
       hillshade : numpy.ndarray
              Hillshade
    """
    shadow_layer_file = tempfile.NamedTemporaryFile(
        dir=scratch_dir, suffix='.tif').name
    if temp_files_list is not None:
        temp_files_list.append(shadow_layer_file)

    gdal.DEMProcessing(shadow_layer_file, dem_file, "hillshade",
                      azimuth=sun_azimuth_angle,
                      altitude=sun_elevation_angle)
    gdal_ds = gdal.Open(shadow_layer_file, gdal.GA_ReadOnly)
    hillshade = gdal_ds.ReadAsArray()
    del gdal_ds
    return hillshade


def _compute_opera_shadow_layer(dem, sun_azimuth_angle, sun_elevation_angle,
                                min_slope_angle = MIN_SLOPE_ANGLE,
                                max_sun_local_inc_angle = MAX_SUN_LOCAL_INC_ANGLE,
                                pixel_spacing_x = 30, pixel_spacing_y = 30):
    """Compute hillshade using new OPERA shadow masking

       Parameters
       ----------
       dem_file: str
              DEM filename
       sun_azimuth_angle: float
              Sun azimuth angle
       sun_elevation_angle: float
              Sun elevation angle
       slope_angle_threshold: float
              Slope angle threshold
       MIN_SLOPE_ANGLE: float (optional)
              Maximum slope angle
       max_sun_local_inc_angle: float (optional)
              Maximum local-incidence angle
       pixel_spacing_x: float (optional)
              Pixel spacing in the X direction
       pixel_spacing_y: float (optional)
              Pixel spacing in the Y direction

       Returns
       -------
       hillshade : numpy.ndarray
              Hillshade
    """
    sun_azimuth = np.radians(sun_azimuth_angle)
    sun_zenith_degrees = 90 - sun_elevation_angle
    sun_zenith = np.radians(sun_zenith_degrees)

    target_to_sun_unit_vector = [np.sin(sun_azimuth) * np.sin(sun_zenith),
                                 np.cos(sun_azimuth) * np.sin(sun_zenith),
                                 np.cos(sun_zenith)]
    
    gradient_h = np.gradient(dem)
    terrain_normal_vector = [-gradient_h[1] / pixel_spacing_x,
                             -gradient_h[0] / - abs(pixel_spacing_y),
                             1]

    normalization_factor = np.sqrt(terrain_normal_vector[0] ** 2 +
                                   terrain_normal_vector[1] ** 2 + 1)

    sun_inc_angle = np.arccos(
        (terrain_normal_vector[0] * target_to_sun_unit_vector[0] +
         terrain_normal_vector[1] * target_to_sun_unit_vector[1] +
         terrain_normal_vector[2] * target_to_sun_unit_vector[2]) /
         normalization_factor)

    sun_inc_angle_degrees = np.degrees(sun_inc_angle)
        
    directional_slope_angle = np.degrees(np.arctan(
        terrain_normal_vector[0] * np.sin(sun_azimuth) +
        terrain_normal_vector[1] * np.cos(sun_azimuth)))

    backslope_mask = directional_slope_angle <= MIN_SLOPE_ANGLE
    low_sun_inc_angle_mask = sun_inc_angle_degrees <= max_sun_local_inc_angle
    shadow_mask = (low_sun_inc_angle_mask | (~ backslope_mask))

    return shadow_mask


def _get_binary_representation(diagnostic_layer_decimal, nbits=6):
    """
    Return the binary representation of the diagnostic layer in decimal
    representation.

       Parameters
       ----------
       diagnostic_layer_decimal: np.ndarray
              Diagnostic layer in decimal representation
       nbits: int
              Number of bits. Default: 6 (number of bits of
              interpreted_dswx_band_dict 5 plus 1)

       Returns
       -------
       diagnostic_layer_binary: np.ndarray
              Diagnostic layer in binary representation
    """

    diagnostic_layer_binary = np.zeros_like(diagnostic_layer_decimal,
                                            dtype=np.uint16)

    for i in range(nbits):
        diagnostic_layer_decimal, bit_array = \
            np.divmod(diagnostic_layer_decimal, 2)
        diagnostic_layer_binary += bit_array * (10 ** i)

    return diagnostic_layer_binary


def _crop_2d_array_all_sides(input_2d_array, margin):
    """
    Crops 2-D array by margin on top, bottom, left, and right.

       Parameters
       ----------
       input_2d_array: np.ndarray
              2-D array to be cropped
       margin: int
              The amount to crop arr from all four sides

       Returns
       -------
       cropped_2d_array: np.ndarray
              Cropped 2-D array
    """
    cropped_2d_array = input_2d_array[margin:-margin, margin:-margin]
    return cropped_2d_array


def generate_dswx_layers(input_list,
                         output_file = None,
                         hls_thresholds = None,
                         dem_file=None,
                         dem_description=None,
                         output_interpreted_band=None,
                         output_rgb_file=None,
                         output_infrared_rgb_file=None,
                         output_binary_water=None,
                         output_confidence_layer=None,
                         output_diagnostic_layer=None,
                         output_non_masked_dswx=None,
                         output_shadow_masked_dswx=None,
                         output_landcover=None,
                         output_shadow_layer=None,
                         output_cloud_mask=None,
                         output_dem_layer=None,
                         output_browse_image=None,
                         browse_image_height=3660,
                         browse_image_width=3660,
                         landcover_file=None,
                         landcover_description=None,
                         worldcover_file=None,
                         worldcover_description=None,
                         flag_offset_and_scale_inputs=False,
                         scratch_dir='.',
                         product_id=None,
                         flag_use_otsu_terrain_masking=True,
                         min_slope_angle=MIN_SLOPE_ANGLE,
                         max_sun_local_inc_angle=MAX_SUN_LOCAL_INC_ANGLE,
                         flag_debug=False):
    """Compute the DSWx-HLS product

       Parameters
       ----------
       input_list: list
              Input file list
       output_file: str
              Output filename
       hls_thresholds: HlsThresholds (optional)
              HLS reflectance thresholds for generating DSWx-HLS products
       dem_file: str (optional)
              DEM filename
       dem_description: str (optional)
              DEM description
       output_interpreted_band: str (optional)
              Output interpreted band filename
       output_rgb_file: str (optional)
              Output RGB filename
       output_infrared_rgb_file: str (optional)
              Output infrared RGB filename
       output_binary_water: str (optional)
              Output binary water filename
       output_confidence_layer: str (optional)
              Output confidence layer filename
       output_diagnostic_layer: str (optional)
              Output diagnostic layer filename
       output_non_masked_dswx: str (optional)
              Output (non-masked) interpreted layer filename
       output_shadow_masked_dswx: str (optional)
              Output shadow-masked filename
       output_landcover: str (optional)
              Output landcover classification file
       output_shadow_layer: str (optional)
              Output shadow layer filename
       output_cloud_mask: str (optional)
              Output cloud/cloud-shadow mask filename
       output_dem_layer: str (optional)
              Output elevation layer filename
       output_browse_image: str (optional)
              Output browse image PNG filename
       browse_image_height: int (optional)
              Height in pixels of the browse image PNG
       browse_image_width: int (optional)
              Width in pixels of the browse image PNG
       landcover_file: str (optional)
              Copernicus Global Land Service (CGLS) Land Cover Layer file
       landcover_description: str (optional)
              Copernicus Global Land Service (CGLS) Land Cover Layer description
       worldcover_file: str (optional)
              ESA WorldCover map filename
       worldcover_description: str (optional)
              ESA WorldCover map description
       flag_offset_and_scale_inputs: bool (optional)
              Flag indicating if DSWx-HLS should be offsetted and scaled
       scratch_dir: str (optional)
              Temporary directory
       product_id: str (optional)
              Product ID that will be saved in the output' product's
              metadata
       flag_use_otsu_terrain_masking: bool (optional)
              Flag to indicate whether the terrain masking should be computed
              with the Otsu threshold method
       MIN_SLOPE_ANGLE: float (optional)
              Maximum slope angle
       max_sun_local_inc_angle: float (optional)
              Maximum local-incidence angle
       flag_debug: bool (optional)
              Flag to indicate if execution is for debug purposes. If so,
              only a subset of the image will be loaded into memory

       Returns
       -------
       success : bool
              Flag success indicating if execution was successful
    """
    if hls_thresholds is None:
        hls_thresholds = parse_runconfig_file()

    if scratch_dir is None:
        scratch_dir = '.'

    logger.info('input parameters:')
    logger.info('    file(s):')
    for input_file in input_list:
        logger.info(f'        {input_file}')
    if output_file:
        logger.info(f'    output multi-band file: {output_file}')
    logger.info(f'    DEM file: {dem_file}')
    logger.info(f'    scratch directory: {scratch_dir}')

    os.makedirs(scratch_dir, exist_ok=True)

    image_dict = {}
    offset_dict = {}
    scale_dict = {}
    temp_files_list = []
    output_files_list = []
    build_vrt_list = []
    dem = None
    shadow_layer = None

    if product_id is None and output_file:
        product_id = os.path.splitext(os.path.basename(output_file))[0]
    elif product_id is None:
        product_id = 'dswx_hls'

    dswx_metadata_dict = _get_dswx_metadata_dict(product_id)

    version = None
    if not isinstance(input_list, list) or len(input_list) == 1:
        success = _load_hls_product_v1(input_list, image_dict, offset_dict,
                                       scale_dict, dswx_metadata_dict,
                                       flag_offset_and_scale_inputs,
                                       flag_debug = flag_debug)
        if success:
            version = '1.4'
    else:
        success = None

    # If success is None or False:
    if success is not True:
        success = _load_hls_product_v2(input_list, image_dict, offset_dict,
                                       scale_dict, dswx_metadata_dict,
                                       flag_offset_and_scale_inputs,
                                       flag_debug = flag_debug)
        if not success:
            logger.info(f'ERROR could not read file(s): {input_list}')
            return False
        version = '2.0'

    hls_dataset_name = image_dict['hls_dataset_name']
    _populate_dswx_metadata_datasets(
        dswx_metadata_dict,
        hls_dataset_name,
        dem_file=dem_file,
        dem_description=dem_description,
        landcover_file=landcover_file,
        landcover_description=landcover_description,
        worldcover_file=worldcover_file,
        worldcover_description=worldcover_description)

    spacecraft_name = dswx_metadata_dict['SPACECRAFT_NAME']
    logger.info(f'processing HLS {spacecraft_name[0]}30 dataset v.{version}')
    blue = image_dict['blue']
    green = image_dict['green']
    red = image_dict['red']
    nir = image_dict['nir']
    swir1 = image_dict['swir1']
    swir2 = image_dict['swir2']
    qa = image_dict['qa']

    geotransform = image_dict['geotransform']
    projection = image_dict['projection']
    length = image_dict['length']
    width = image_dict['width']

    sun_azimuth_angle_meta = dswx_metadata_dict['MEAN_SUN_AZIMUTH_ANGLE'].split(', ')
    sun_zenith_angle_meta = dswx_metadata_dict['MEAN_SUN_ZENITH_ANGLE'].split(', ')

    if len(sun_azimuth_angle_meta) == 2:
        sun_azimuth_angle = (float(sun_azimuth_angle_meta[0]) +
                            float(sun_azimuth_angle_meta[1])) / 2.0
    else:
        sun_azimuth_angle = float(sun_azimuth_angle_meta[0])
    if len(sun_zenith_angle_meta) == 2:
        sun_zenith_angle = (float(sun_zenith_angle_meta[0]) +
                            float(sun_zenith_angle_meta[1])) / 2.0
    else:
        sun_zenith_angle = float(sun_zenith_angle_meta[0])

    # Sun elevation and zenith angles are complementary
    sun_elevation_angle = 90 - float(sun_zenith_angle)

    logger.info(f'Mean Sun azimuth angle: {sun_azimuth_angle}')
    logger.info(f'Mean Sun elevation angle: {sun_elevation_angle}')

    if dem_file is not None:
        # DEM
        dem_cropped_file = tempfile.NamedTemporaryFile(
            dir=scratch_dir, suffix='.tif').name
        if temp_files_list is not None:
            temp_files_list.append(dem_cropped_file)
        dem_with_margin = _warp(dem_file, geotransform, projection,
                                    length, width, scratch_dir,
                                    resample_algorithm='cubic',
                                    relocated_file=dem_cropped_file,
                                    margin_in_pixels=DEM_MARGIN_IN_PIXELS,
                                    temp_files_list=temp_files_list)

        if flag_use_otsu_terrain_masking:
            # shadow masking with Otsu threshold method
            hillshade = _compute_hillshade(
                dem_cropped_file, scratch_dir, sun_azimuth_angle,
                sun_elevation_angle, temp_files_list = temp_files_list)
            shadow_layer_with_margin = _compute_otsu_threshold(
                hillshade, is_normalized = True)
        else:
            # new OPERA shadow masking
            shadow_layer_with_margin = _compute_opera_shadow_layer(
                dem_with_margin, sun_azimuth_angle, sun_elevation_angle,
                min_slope_angle = min_slope_angle,
                max_sun_local_inc_angle = max_sun_local_inc_angle)

        # remove extra margin from shadow_layer
        shadow_layer = _crop_2d_array_all_sides(shadow_layer_with_margin,
                                                DEM_MARGIN_IN_PIXELS)
        del shadow_layer_with_margin

        # remove extra margin from DEM
        dem = _crop_2d_array_all_sides(dem_with_margin, DEM_MARGIN_IN_PIXELS)
        del dem_with_margin
        if output_dem_layer is not None:
           _save_array(dem, output_dem_layer,
                       dswx_metadata_dict, geotransform, projection,
                       description=band_description_dict['DEM'],
                       output_dtype = gdal.GDT_Float32,
                       scratch_dir=scratch_dir,
                       output_files_list=build_vrt_list,
                       no_data_value=np.nan)
        if not output_file:
            del dem


        if output_shadow_layer:
            binary_mask_ctable = _get_binary_mask_ctable()
            _save_array(shadow_layer, output_shadow_layer,
                        dswx_metadata_dict, geotransform, projection,
                        description=band_description_dict['SHAD'],
                        scratch_dir=scratch_dir,
                        output_files_list=build_vrt_list,
                        ctable=binary_mask_ctable)

    landcover_mask = None
    if landcover_file is not None and worldcover_file is not None:
        # land cover
        landcover_mask = create_landcover_mask(
            landcover_file, worldcover_file, output_landcover,
            scratch_dir, landcover_mask_type, geotransform, projection,
            length, width, dswx_metadata_dict = dswx_metadata_dict,
            output_files_list=build_vrt_list, temp_files_list=temp_files_list)

    # Set array of invalid pixels
    if not flag_offset_and_scale_inputs:
        invalid_ind = np.where(blue < -5000)
    else:
        invalid_ind = np.where(blue < -0.5)

    if output_rgb_file:
        _save_output_rgb_file(red, green, blue, output_rgb_file,
                              offset_dict, scale_dict,
                              flag_offset_and_scale_inputs,
                              dswx_metadata_dict,
                              geotransform, projection,
                              invalid_ind=invalid_ind,
                              scratch_dir=scratch_dir,
                              output_files_list=output_files_list)

    if output_infrared_rgb_file:
        _save_output_rgb_file(swir1, nir, red, output_infrared_rgb_file,
                              offset_dict, scale_dict,
                              flag_offset_and_scale_inputs,
                              dswx_metadata_dict,
                              geotransform, projection,
                              invalid_ind=invalid_ind,
                              scratch_dir=scratch_dir,
                              output_files_list=output_files_list,
                              flag_infrared=True)

    diagnostic_layer_decimal = _compute_diagnostic_tests(
        blue, green, red, nir, swir1, swir2, hls_thresholds)
 
    diagnostic_layer_decimal[invalid_ind] = DIAGNOSTIC_LAYER_NO_DATA_DECIMAL

    interpreted_dswx_band = generate_interpreted_layer(
        diagnostic_layer_decimal)
    
    diagnostic_layer = _get_binary_representation(diagnostic_layer_decimal)
    del diagnostic_layer_decimal

    if output_diagnostic_layer:
        _save_array(diagnostic_layer, output_diagnostic_layer,
                    dswx_metadata_dict, geotransform, projection,
                    description=band_description_dict['DIAG'],
                    scratch_dir=scratch_dir,
                    output_files_list=build_vrt_list,
                    output_dtype=gdal.GDT_UInt16,
                    no_data_value=DIAGNOSTIC_LAYER_NO_DATA_BINARY_REPR)


    if invalid_ind is not None:
        interpreted_dswx_band[invalid_ind] = UINT8_FILL_VALUE

    if output_non_masked_dswx:
        save_dswx_product(interpreted_dswx_band,
                          output_non_masked_dswx,
                          dswx_metadata_dict,
                          geotransform,
                          projection,
                          description=band_description_dict['WTR-1'],
                          scratch_dir=scratch_dir,
                          output_files_list=build_vrt_list)

    landcover_shadow_masked_dswx = _apply_landcover_and_shadow_masks(
        interpreted_dswx_band, nir, landcover_mask, shadow_layer,
        hls_thresholds)

    if output_shadow_masked_dswx is not None:
        save_dswx_product(landcover_shadow_masked_dswx,
                          output_shadow_masked_dswx,
                          dswx_metadata_dict,
                          geotransform,
                          projection,
                          description=band_description_dict['WTR-2'],
                          scratch_dir=scratch_dir,
                          output_files_list=build_vrt_list)

    cloud, masked_dswx_band = _compute_mask_and_filter_interpreted_layer(
        landcover_shadow_masked_dswx, qa)

    if invalid_ind is not None:
        # Set invalid pixels to mask fill value
        cloud[invalid_ind] = UINT8_FILL_VALUE
        masked_dswx_band[invalid_ind] = UINT8_FILL_VALUE

    if output_interpreted_band:
        save_dswx_product(masked_dswx_band,
                          output_interpreted_band,
                          dswx_metadata_dict,
                          geotransform,
                          projection,
                          description=band_description_dict['WTR'],
                          scratch_dir=scratch_dir,
                          output_files_list=build_vrt_list)

    # Output the WTR layer as the browse image
    # Note: The browse image will be always be saved as a separate png file;
    # it will not included in the combined `output_file`.
    if output_browse_image:
        # If the `output_interpreted_band` was generated,
        # convert that to the browse image
        if output_interpreted_band:
            # create the browse image
            geotiff2png(src_geotiff_filename=output_interpreted_band,
                    dest_png_filename=output_browse_image,
                    output_height=browse_image_height,
                    output_width=browse_image_width,
                    scratch_dir=scratch_dir,
                    logger=logger
                    )
        else:
            # If the `output_interpreted_band` was not generated,
            # we'll need to make it temporarily to convert is to
            # the browse image

            # Create the source image as a geotiff
            # Reason: gdal.Create() cannot create .png files, so we
            # must start from a GeoTiff, etc.
            # Source: https://gis.stackexchange.com/questions/132298/gdal-c-api-how-to-create-png-or-jpeg-from-scratch
            tmp_geotiff_file = os.path.join(scratch_dir,"dswx_tmp.tif")
            save_dswx_product(masked_dswx_band,
                            tmp_geotiff_file,
                            dswx_metadata_dict,
                            geotransform,
                            projection,
                            scratch_dir=scratch_dir)
            
            # Convert to a png
            geotiff2png(src_geotiff_filename=tmp_geotiff_file,
                    dest_png_filename=output_browse_image,
                    output_height=browse_image_height,
                    output_width=browse_image_width,
                    scratch_dir=scratch_dir,
                    logger=logger
                    )
        
            # remove the temporarily-created file
            temp_files_list += [tmp_geotiff_file]

    if output_cloud_mask:
        save_cloud_mask(cloud, output_cloud_mask, dswx_metadata_dict, geotransform,
                        projection,
                        description=band_description_dict['CLOUD'],
                        scratch_dir=scratch_dir,
                        output_files_list=build_vrt_list)

    binary_water_layer = _get_binary_water_layer(masked_dswx_band)
    if output_binary_water:
        _save_binary_water(binary_water_layer, output_binary_water,
                           dswx_metadata_dict,
                           geotransform, projection,
                           scratch_dir=scratch_dir,
                           description=band_description_dict['BWTR'],
                           output_files_list=build_vrt_list)

    # TODO: fix CONF layer!!!
    if output_confidence_layer:
        confidence_layer = _get_confidence_layer(masked_dswx_band)
        confidence_layer_ctable = _get_confidence_layer_ctable()
        _save_array(confidence_layer,
                    output_confidence_layer,
                    dswx_metadata_dict,
                    geotransform, projection,
                    scratch_dir=scratch_dir,
                    description=band_description_dict['CONF'],
                    output_files_list=build_vrt_list,
                    ctable=confidence_layer_ctable,
                    no_data_value=UINT8_FILL_VALUE)

    # save output_file as GeoTIFF
    if output_file and not output_file.endswith('.vrt'):
        save_dswx_product(masked_dswx_band,
                          output_file,
                          dswx_metadata_dict,
                          geotransform,
                          projection,
                          bwtr=binary_water_layer,
                          diag=diagnostic_layer,
                          wtr_1=interpreted_dswx_band,
                          wtr_2=landcover_shadow_masked_dswx,
                          land=landcover_mask,
                          shad=shadow_layer,
                          cloud=cloud,
                          dem=dem,
                          scratch_dir=scratch_dir,
                          output_files_list=output_files_list)

    # save output_file as VRT
    elif output_file:
        vrt_options = gdal.BuildVRTOptions(resampleAlg='nearest')
        gdal.BuildVRT(output_file, build_vrt_list, options=vrt_options)
        build_vrt_list.append(output_file)
        logger.info(f'file saved: {output_file}')

    logger.info('removing temporary files:')
    for filename in temp_files_list:
        if not os.path.isfile(filename):
            continue
        os.remove(filename)
        logger.info(f'    {filename}')

    logger.info('output files:')
    for filename in build_vrt_list + output_files_list:
        logger.info(f'    {filename}')

    return True

