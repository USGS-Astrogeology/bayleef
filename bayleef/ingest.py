import errno
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from datetime import date, datetime
from functools import partial, reduce
from glob import glob
from os import path
from shutil import copyfile
from subprocess import PIPE, Popen

import bayleef
import gdal
import geopandas as gpd
import matplotlib
import matplotlib.animation as animation
import matplotlib.pyplot as plt
import numpy as np
import osr
import pandas as pd
import plio
import pvl
import seaborn as sns
import shapely
import wget
import yaml
from bayleef import config
from geoalchemy2 import Geometry, WKTElement
from geoalchemy2.shape import from_shape
from matplotlib import rcParams
from mpl_toolkits.axes_grid1 import make_axes_locatable
from plio.io.io_gdal import GeoDataset
from pylab import rcParams
from shapely.geometry import Polygon
from sqlalchemy import *

import autocnet
import pysis
from autocnet import CandidateGraph
from autocnet.graph.edge import Edge
from autocnet.matcher import suppression_funcs
from autocnet.matcher.subpixel import (clip_roi, subpixel_phase,
                                       subpixel_template)
from pysis.exceptions import ProcessError
from pysis.isis import (cam2map, campt, footprintinit, jigsaw, map2map,
                        pointreg, spiceinit)

from . import utils
import logging
logger = logging.getLogger('Bayleef')

plt.switch_backend('agg')

def master(root, masterhdf):
    """
    Ingestion function for master. Master is unique in that it cannot be pulled
    by an API, therefore original MASTER files have to exist locally.

    Parameters
    ----------
    root : str
           path to the bayleef data directory.

    masterhdf : str
                path to a MASTER .HDF file
    """
    fd = GeoDataset(masterhdf)
    meta = fd.metadata

    meta['bayleef_name'] = 'MASTER'

    for key in meta.keys():
        val = meta.pop(key)
        meta[key.lower()] = val

    date = datetime.strptime(meta['completiondate'] , "%d-%b-%Y %H:%M:%S")
    line = meta['flightlinenumber']
    daytime_flag = meta['day_night_flag']
    ID = meta['producer_granule_id'].split('.')[0]

    basepath = path.join(root, 'MASTER',str(date.year), str(line), daytime_flag,ID)
    ogdatapath = path.join(basepath, 'original')
    imagedatapath = path.join(basepath, 'imagedata')

    newhdf = path.join(ogdatapath, path.basename(masterhdf))

    # Try making the directory
    try:
        os.makedirs(basepath)
        os.makedirs(ogdatapath)
        os.makedirs(imagedatapath)
    except OSError as exc:
        if exc.errno == errno.EEXIST and path.isdir(basepath):
            pass
        else:
            raise

    # copy original hdf
    try:
        copyfile(masterhdf, newhdf)
    except shutil.SameFileError:
        pass

    # explicitly close file descriptor
    del fd

    fd = GeoDataset(newhdf)
    subdatasets = fd.dataset.GetSubDatasets()

    for dataset in subdatasets:
        ofilename = '{}.tif'.format(dataset[1].split()[1])
        ofilename_abspath = path.join(imagedatapath, ofilename)
        gdal.Translate(ofilename_abspath, dataset[0], format="GTiff")

    # create geo corrected calibrated image
    lats = path.join(imagedatapath, 'PixelLatitude.tif')
    lons = path.join(imagedatapath, 'PixelLongitude.tif')
    image = path.join(imagedatapath, 'CalibratedData.tif')

    geocorrected_image = path.join(imagedatapath, 'CalibratedData_Geo.tif')
    utils.geolocate(image, geocorrected_image, lats, lons)


    # Master has 50 bands, extract them as seperate files
    for i in range(1,51):
        gdal.Translate(path.join(imagedatapath, 'b{}.tif'.format(i)), path.join(imagedatapath, 'CalibratedData_Geo.tif'), bandList=[i])

    index_meta = {}

    ll = float(meta['lon_ll']), float(meta['lat_ll'])
    lr = float(meta['lon_lr']), float(meta['lon_lr'])
    ul = float(meta['lon_ul']), float(meta['lon_ul'])
    ur = float(meta['lon_ur']), float (meta['lon_ur'])

    index_meta['geom'] = Polygon([ll, ul, ur, lr]).wkt
    index_meta['id'] = ID

    index_meta['time'] = {}
    index_meta['time']['year'] = date.year
    index_meta['time']['month'] = date.month
    index_meta['time']['day'] = date.day
    index_meta['time']['hour'] = date.hour

    index_json_file = path.join(basepath, 'index.json')
    meta_json_file = path.join(basepath,'meta.json')
    json.dump(index_meta, open(index_json_file, 'w+'))
    json.dump(meta, open(meta_json_file, 'w+'))


def themis_pairs(root, id1, id2):
    def stats(arr, additional_funcs=[]):
        return {
            'mean': float(np.mean(arr)),
            'min': float(np.min(arr)),
            'max': float(np.max(arr)),
            'stddev': float(np.std(arr))
        }

    # enforce ID1 < ID2
    id1, id2 = sorted([id1, id2])

    data_dir = config.data
    themis_dir1 = os.path.join(data_dir, "THEMIS", id1[0], id1[1], id1)
    themis_dir2 = os.path.join(data_dir, "THEMIS", id2[0], id2[1], id2)
    pair_dir = os.path.join(data_dir, "THEMIS_PAIRS", id1, id2)

    map_file = config.themis.map_file
    if not os.path.isfile(map_file):
        raise Exception("{} does not exist.".format(map_file))


    pair_original_path = os.path.join(pair_dir, 'original')
    pair_images_path = os.path.join(pair_dir, 'imagedata')
    bundle_result_path = os.path.join(pair_dir, 'bundle')
    plot_path = os.path.join(pair_dir, 'plots')

    img1_path = os.path.join(themis_dir1, 'original', 'l1.cub')
    img2_path = os.path.join(themis_dir2, 'original', 'l1.cub')

    img1_cropped_path = os.path.join(pair_original_path, 'source.l1.cub')
    img2_cropped_path = os.path.join(pair_original_path, 'destination.l1.cub')

    img1_projected_path = os.path.join(pair_original_path, 'source.l2.cub')
    img2_projected_path = os.path.join(pair_original_path, 'destination.l2.cub')

    img1_projected_bt_path = os.path.join(pair_original_path, 'source.l2.bt.cub')
    img2_projected_bt_path = os.path.join(pair_original_path, 'destination.l2.bt.cub')

    img2_matchmapped_path =  os.path.join(pair_original_path, 'destination.l2.mm.cub')
    img2_matchmapped_bt_path = os.path.join(pair_original_path, 'destination.l2.bt.mm.cub')

    cubelis = os.path.join(pair_dir, 'filelist.txt')

    cnet_path = os.path.join(bundle_result_path, 'cnet.net')

    autocnet_plot_path = os.path.join(plot_path, 'autocnet.tif')
    histogram_plot_path = os.path.join(plot_path, 'hist.tif')
    overlap_plot_path = os.path.join(plot_path, 'overlap.tif')

    img1_b9_path = os.path.join(pair_images_path, 'source.b9.tif')
    img2_b9_path = os.path.join(pair_images_path, 'destination.b9.tif')
    img1_b9_bt_path = os.path.join(pair_images_path, 'source.b9.bt.tif')
    img2_b9_bt_path = os.path.join(pair_images_path, 'destination.b9.bt.tif')

    rad_diff_image = os.path.join(pair_images_path, 'rad_diff.tif')
    bt_diff_image = os.path.join(pair_images_path, 'bt_diff.tif')

    logger.info('Making directories {} and {}'.format(pair_original_path, pair_images_path))
    os.makedirs(pair_original_path, exist_ok=True)
    os.makedirs(pair_images_path, exist_ok=True)
    os.makedirs(bundle_result_path, exist_ok=True)
    os.makedirs(plot_path, exist_ok=True)

    # write out cubelist
    with open(cubelis, 'w') as f:
        f.write(img1_cropped_path + '\n')
        f.write(img2_cropped_path + '\n')

    logger.info('IDs: {} {}'.format(id1, id2))
    logger.info('DATA DIR: {}'.format(data_dir))
    logger.info('IMAGE 1 PATH: {}'.format(img1_path))
    logger.info('IMAGE 2 PATH: {}'.format(img2_path))
    logger.info('PAIR OG DIR: {}'.format(pair_original_path))
    logger.info('PAIR IMAGE PATH: {}'.format(pair_images_path))
    logger.info('PAIR DIR: {}'.format(pair_dir))

    img1_smithed = False
    img2_smithed = False

    img1_smithed = utils.preprocess(id1, themis_dir1, day=True, validate=True, gtiffs=False, projected_images=False)
    img2_smithed = utils.preprocess(id2, themis_dir2, day=True, validate=True, gtiffs=False, projected_images=False)

    img1_fh = GeoDataset(img1_path)
    img2_fh = GeoDataset(img2_path)

    # minLat maxLat minLon maxLon
    minLat, maxLat, _, _ = img1_fh.footprint.Intersection(img2_fh.footprint).GetEnvelope()
    utils.thm_crop(img1_path, img1_cropped_path, minLat, maxLat)
    utils.thm_crop(img2_path, img2_cropped_path, minLat, maxLat)

    del (img1_fh, img2_fh)

    used_smithed = True
    if not (img1_smithed and img2_smithed):
        logger.info("No smithed kernels found, matching with Autocnet.")
        used_smithed = False
        cg = utils.match_pair(img1_cropped_path, img2_cropped_path, figpath=autocnet_plot_path)
        cg.generate_control_network()
        cg.to_isis(os.path.splitext(cnet_path)[0])

        bundle_parameters = config.themis.bundle_parameters
        bundle_parameters['from_'] = cubelis
        bundle_parameters['cnet'] = cnet_path
        bundle_parameters['onet'] = cnet_path
        bundle_parameters['file_prefix'] = bundle_result_path+'/'
        logger.info("Running Jigsaw, parameters:\n")
        utils.print_dict(bundle_parameters)
        try:
            jigsaw(**bundle_parameters)
        except ProcessError as e:
            logger.error("STDOUT: {}".format(e.stdout.decode('utf-8')))
            logger.error("STDERR: {}".format(e.stderr.decode('utf-8')))
            raise Exception("Jigsaw Error")

    try:
        map_pvl = pvl.load(map_file)
    except Exception as e:
        logger.error("Error loading mapfile {}:\n{}".format(map_file, e))

    logger.info('Projecting {} to {} with map file:\n {}'.format(img1_cropped_path, img1_projected_path, map_pvl))
    utils.project(img1_cropped_path, img1_projected_path, map_file)

    logger.info('Projecting {} to {} with map file:\n {}'.format(img2_cropped_path, img2_projected_path, map_pvl))
    utils.project(img2_cropped_path, img2_projected_path, map_file)

    img1_footprint = GeoDataset(img1_projected_path).footprint
    img2_footprint = GeoDataset(img2_projected_path).footprint
    overlap_geom = img2_footprint.Intersection(img1_footprint)

    try:
        out1, err1 = utils.run_davinci('thm_tb.dv', img1_projected_path, img1_projected_bt_path)
        out2, err2 = utils.run_davinci('thm_tb.dv', img2_projected_path, img2_projected_bt_path)
    except Exception as e:
        logger.error(e)

    try:
        out1, err1 = utils.run_davinci('thm_post_process.dv', img1_projected_bt_path, img1_projected_bt_path)
        out2, err2 = utils.run_davinci('thm_post_process.dv', img2_projected_bt_path, img2_projected_bt_path)

        out1, err1 = utils.run_davinci('thm_bandselect.dv', img1_projected_bt_path, img1_projected_bt_path, args=['band=9'])
        out2, err2 = utils.run_davinci('thm_bandselect.dv', img2_projected_bt_path, img2_projected_bt_path, args=['band=9'])
    except Exception as e:
        logger.error(e)

    try:
        out1, err1 = utils.run_davinci('thm_post_process.dv', img1_projected_path, img1_projected_path)
        out2, err2 = utils.run_davinci('thm_post_process.dv', img2_projected_path, img2_projected_path)

        out1, err1 = utils.run_davinci('thm_bandselect.dv', img1_projected_path, img1_projected_path, args=['band=9'])
        out2, err2 = utils.run_davinci('thm_bandselect.dv', img2_projected_path, img2_projected_path, args=['band=9'])
    except Exception as e:
        logger.error(e)


    footprintinit(from_=img2_projected_bt_path)
    footprintinit(from_=img2_projected_path)

    logger.info('Creating matchmapped cubes')
    utils.project(img2_projected_path, img2_matchmapped_path, img1_projected_path, matchmap=True)
    utils.project(img2_projected_bt_path, img2_matchmapped_bt_path, img1_projected_bt_path, matchmap=True)

    img1_projected = GeoDataset(img1_projected_path)
    img2_projected = GeoDataset(img2_matchmapped_path)

    arr1 = img1_projected.read_array()
    arr2 = img2_projected.read_array()

    arr1[arr1 == pysis.specialpixels.SPECIAL_PIXELS['Real']['Null']] = 0
    arr2[arr2 == pysis.specialpixels.SPECIAL_PIXELS['Real']['Null']] = 0
    arr1[arr1 == -32768.] = 0
    arr2[arr2 == -32768.] = 0

    arr1 = np.ma.MaskedArray(arr1, arr1 == 0)
    arr2 = np.ma.MaskedArray(arr2, arr2 == 0)

    img1_b9_overlap = np.ma.MaskedArray(arr1.data, arr1.mask | arr2.mask)
    img2_b9_overlap = np.ma.MaskedArray(arr2.data, arr1.mask | arr2.mask)
    rad_diff = np.ma.MaskedArray(img1_b9_overlap.data-img2_b9_overlap.data, arr1.mask | arr2.mask)

    img1rads = img1_b9_overlap[~img1_b9_overlap.mask]
    img2rads = img2_b9_overlap[~img2_b9_overlap.mask]

    img1_b9_overlap.data[img1_b9_overlap.mask] = 0
    img2_b9_overlap.data[img2_b9_overlap.mask] = 0
    rad_diff.data[rad_diff.mask] = 0

    # logger.info('Writing {}'.format(img1_b9_path))
    # ds = utils.array2raster(img1_projected_path, img1_b9_overlap, img1_b9_path)
    # del ds
    #
    # logger.info('Writing {}'.format(img2_b9_path))
    # ds = utils.array2raster(img2_projected_path, img2_b9_overlap, img2_b9_path)
    # del ds

    logger.info('Writing {}'.format(rad_diff_image))
    ds = utils.array2raster(img1_projected_path, rad_diff, rad_diff_image)
    del ds

    img1_bt_projected = GeoDataset(img1_projected_bt_path)
    img2_bt_projected = GeoDataset(img2_matchmapped_bt_path)

    arr1 = img1_bt_projected.read_array()
    arr2 = img2_bt_projected.read_array()

    arr1[arr1==0] = np.nan
    arr1[arr1==pysis.specialpixels.SPECIAL_PIXELS['Real']['Null']] = np.nan
    arr2[arr2==0] = np.nan

    arr1 = np.ma.MaskedArray(arr1, arr1 == 0)
    arr2 = np.ma.MaskedArray(arr2, arr2 == 0)

    img1_b9_bt_overlap = np.ma.MaskedArray(arr1.data, arr1.mask | arr2.mask)
    img2_b9_bt_overlap = np.ma.MaskedArray(arr2.data, arr1.mask | arr2.mask)
    bt_diff = np.ma.MaskedArray(img1_b9_bt_overlap.data-img2_b9_bt_overlap.data, arr1.mask | arr2.mask)

    img1bt = img1_b9_bt_overlap[~img1_b9_bt_overlap.mask]
    img2bt = img2_b9_bt_overlap[~img2_b9_bt_overlap.mask]

    img1_b9_bt_overlap.data[img1_b9_bt_overlap.mask] = 0
    img2_b9_bt_overlap.data[img2_b9_bt_overlap.mask] = 0
    bt_diff.data[bt_diff.mask] = 0
    
    image1 = arr1[~np.isnan(arr1)]
    image2 = arr2[~np.isnan(arr2)]

    img1_stats = stats.describe(image1)
    img2_stats = stats.describe(image2)

    bt_diff = arr1 - arr2
    bt_diff[bt_diff==0] = np.nan
    bt_diff2 = bt_diff[~np.isnan(bt_diff)]
    bt_diff_stats = stats.describe(bt_diff2)
    
    bt_diff2 = arr1-arr2
    bt_diff2_avg = np.nanmean(arr1) - np.nanmean(arr2)

    img1_offset =  arr1 - bt_diff2_avg;

    img1_normalized = (img1_offset[~np.isnan(img1_offset)] - np.mean(img1_offset[~np.isnan(img1_offset)]))/np.std(img1_offset[~np.isnan(img1_offset)])
    arr2_normalized = (arr2[~np.isnan(arr2)] - np.mean(arr2[~np.isnan(arr2)]))/np.std(arr2[~np.isnan(arr2)])

    img1_normalized_stats = stats.describe(img1_normalized)
    arr2_normalized_stats = stats.describe(arr2_normalized)
    
    img1_test2 = (img1_offset - np.mean(img1_offset[~np.isnan(img1_offset)]))/np.std(img1_offset[~np.isnan(img1_offset)])
    arr2_test2 = (arr2 - np.mean(arr2[~np.isnan(arr2)]))/np.std(arr2[~np.isnan(arr2)])

    img1_test = (img1_offset[~np.isnan(img1_offset)] - np.mean(img1_offset[~np.isnan(img1_offset)]))/np.std(img1_offset[~np.isnan(img1_offset)])
    arr2_test = (arr2[~np.isnan(arr2)] - np.mean(arr2[~np.isnan(arr2)]))/np.std(arr2[~np.isnan(arr2)])

    bins1, t_bins1 = np.histogram(img1_test, bins=300)
    bins2, t_bins2 = np.histogram(arr2_test, bins=300)

    array1 = t_bins1-t_bins2
    array2 = bins1-bins2

    RMS_array2 = array2
    RMS_array1 = array1[:-1]

    RMS_TEST = img1_test2 - arr2_test2

    # logger.info('Writing {}'.format(img1_b9_bt_path))
    # ds = utils.array2raster(img1_projected_bt_path, img1_b9_bt_overlap, img1_b9_bt_path)
    # del ds
    #
    # logger.info('Writing {}'.format(img2_b9_bt_path))
    # ds = utils.array2raster(img2_projected_bt_path, img2_b9_bt_overlap, img2_b9_bt_path)
    # del ds

    logger.info('Writing {}'.format(bt_diff_image))
    ds = utils.array2raster(img1_projected_bt_path, bt_diff, bt_diff_image)
    del ds

    img1_campt = pvl.loads(campt(from_=img1_path))['GroundPoint']
    img2_campt = pvl.loads(campt(from_=img1_path))['GroundPoint']

    img1_date = GeoDataset(img1_path).metadata['IsisCube']['Instrument']['StartTime']
    img2_date = GeoDataset(img2_path).metadata['IsisCube']['Instrument']['StartTime']

    metadata = {}
    metadata['img1'] = {}
    metadata['img1']['rad'] = stats(img1rads)
    metadata['img1']['tb'] = stats(img1bt)
    metadata['img1']['emission_angle'] = img1_campt['Emission'].value
    metadata['img1']['incidence_angle'] = img1_campt['Incidence'].value
    metadata['img1']['solar_lon'] = img1_campt['SolarLongitude'].value
    metadata['img1']['date'] = {
        'year' : img1_date.year,
        'month' : img1_date.month,
        'day': img1_date.day
    }

    metadata['img2'] = {}
    metadata['img2']['rad'] = stats(img2rads)
    metadata['img2']['tb'] = stats(img2bt)
    metadata['img2']['emission_angle'] = img2_campt['Emission'].value
    metadata['img2']['incidence_angle'] = img2_campt['Incidence'].value
    metadata['img2']['solar_lon'] = img2_campt['SolarLongitude'].value
    metadata['img2']['date'] = {
        'year' : img2_date.year,
        'month' : img2_date.month,
        'day': img2_date.day
    }

    metadata['diff'] = {}
    metadata['diff']['rad'] = stats(rad_diff)
    metadata['diff']['tb'] = stats(bt_diff)
    metadata['diff']['date(days)'] = (img1_date - img2_date).days
    metadata['id1'] = id1
    metadata['id2'] = id2

    metadata['plots'] = {}
    metadata['plots']['rad_hist'] = os.path.join(plot_path, 'rad_hist.png')
    metadata['plots']['tb_hist'] = os.path.join(plot_path, 'tb_hist.png')
    metadata['plots']['diff_hist'] = os.path.join(plot_path, 'diff_hist.png')
    metadata['plots']['themis_pair'] = os.path.join(plot_path, 'themis_pair.png')
    metadata['plots']['themis_pair_difference'] = os.path.join(plot_path, 'themis_pair_difference.png')
    metadata['plots']['difference_histogram'] = os.path.join(plot_path, 'difference_histogram.png')
    metadata['plots']['themis_pair_histogram'] = os.path.join(plot_path, 'themis_pair_histogram.png')
    metadata['plots']['themis_pair_normalized_histogram'] = os.path.join(plot_path, 'themis_pair_normalized_histogram.png')
    metadata['plots']['normalized_difference_histogram'] = os.path.join(plot_path, 'normalized_difference_histogram.png')
    metadata['plots']['normalized_difference_image'] = os.path.join(plot_path, 'normalized_difference_image.png')
    metadata['plots']['match_plot'] = autocnet_plot_path

    if not used_smithed:
        metadata['plots']['matching_plot'] = autocnet_plot_path
        metadata['bundle'] = {}
        for f in glob(os.path.join(bundle_result_path, '*')):
            metadata['bundle'][os.path.basename(os.path.splitext(f)[0])] = f


        try:
            df = pd.read_csv(metadata['bundle']['residuals'], header=1)
        except:
            df = pd.read_csv(metadata['bundle']['_residuals'], header=1)

        metadata['bundle']['residual_stats'] = stats(np.asarray(df['residual.1'][1:], dtype=float))

    utils.print_dict(metadata)

    plt.figure(figsize=(25,10))
    bins = sns.distplot(img1rads[~img1rads.mask], kde=False, norm_hist=False, label='{} {}'.format(id1, os.path.basename(img1_b9_path)))
    bins = sns.distplot(img2rads[~img2rads.mask], kde=False, norm_hist=False, label='{} {}'.format(id2,os.path.basename(img2_b9_path)))
    bins.set(xlabel='radiance', ylabel='counts')
    plt.legend()
    plt.savefig(metadata['plots']['rad_hist'])
    plt.close()

    plt.figure(figsize=(25,10))
    bins = sns.distplot(img1bt[~img1bt.mask], kde=False, norm_hist=False, label='{} {}'.format(id1, os.path.basename(img1_b9_bt_path)))
    bins = sns.distplot(img2bt[~img2bt.mask], kde=False, norm_hist=False, label='{} {}'.format(id2, os.path.basename(img2_b9_bt_path)))
    bins.set(xlabel='Brightness Temp', ylabel='counts')
    plt.legend()
    plt.savefig(metadata['plots']['tb_hist'])
    plt.close()

    plt.figure(figsize=(25,10))
    diffplot = sns.distplot(rad_diff[~rad_diff.mask],  kde=False)
    diffplot.set(xlabel='Delta Radiance', ylabel='counts')
    plt.savefig(metadata['plots']['diff_hist'])
    plt.close()

    plt.figure(figsize=(25,10))
    plt.imshow(arr1, alpha=.75, cmap='RdBu_r')
    plt.imshow(arr2, alpha=.75, cmap='RdBu_r')
    plt.colorbar()
    plt.savefig(metadata['plots']['themis_pair'])
    plt.close()

    plt.figure(figsize=(25,10))
    plt.imshow(bt_diff, alpha=.75, cmap='RdBu_r')
    plt.colorbar()
    plt.savefig(metadata['plots']['themis_pair_difference'])
    plt.close()
    
    plt.figure(figsize=(25,10))
    plt.hist(bt_diff2, bins=300, alpha=.75, color='purple', label='offset')
    plt.xlabel('Brightness Temperature Difference (K)')
    plt.ylabel('Counts')
    plt.legend()
    plt.savefig(metadata['plots']['difference_histogram'])
    plt.close()
    
    plt.figure(figsize=(25,10))
    plt.hist(image1, bins=300, alpha=.75, color='blue', label='Image 1')
    plt.hist(image2, bins=300, alpha=.75, color='green', label='Image 2')
    plt.xlabel('Brightness Temperature (K)')
    plt.ylabel('Counts')
    plt.legend()
    plt.savefig(metadata['plots']['themis_pair_histogram'])
    plt.close()
 
    plt.figure(figsize=(25,10))
    _ = plt.hist(img1_normalized, bins=300, alpha=.75, color='purple', label='Image 1 - $\Delta$ avg. BT')
    _ = plt.hist(arr2_normalized, bins=300, alpha=.75, color='green', label='Image 2')
    plt.xlabel('Normalized Brightness Temperature')
    plt.ylabel('Counts')
    plt.legend()
    plt.savefig(metadata['plots']['themis_pair_normalized_histogram'])
    plt.close()
    
    plt.figure(figsize=(25,10))
    width = RMS_array1[1] - RMS_array1[0]
    plt.bar(RMS_array1, RMS_array2, width=width, color='k', align='center')
    plt.xlim(np.amin(RMS_array1),np.amax(RMS_array1))
    plt.xlabel('Normalized Brightness Temperature Difference ([Image 1 - $\Delta$ avg. BT] - [Image 2])')
    plt.ylabel('Counts (Difference)')
    plt.savefig(metadata['plots']['normalized_difference_histogram'])
    plt.close()
    
    plt.figure(figsize=(25,10))
    width = RMS_array1[1] - RMS_array1[0]
    plt.bar(RMS_array1, RMS_array2, width=width, color='k', align='center')
    plt.xlim(np.amin(RMS_array1),np.amax(RMS_array1))
    plt.xlabel('Normalized Brightness Temperature Difference ([Image 1 - $\Delta$ avg. BT] - [Image 2])')
    plt.ylabel('Counts (Difference)')
    plt.savefig(metadata['plots']['normalized_difference_histogram'])
    plt.close()
    
    if np.nanmax(bt_diff2) > np.abs(np.nanmin(bt_diff2)):
        plt.figure(figsize=(30,15))
        plt.imshow(arr1, alpha=.95, cmap=cm.gray)
        plt.imshow(arr2, alpha=.95, cmap=cm.gray)
        plt.imshow(bt_diff2, alpha=.90, cmap='RdBu_r', vmin=-np.nanmax(bt_diff2), vmax=np.nanmax(bt_diff2))
        plt.colorbar()
        plt.savefig(metadata['plots']['normalized_difference_image'])
        plt.close()
    else:
        plt.figure(figsize=(30,15))
        plt.imshow(arr1, alpha=.95, cmap=cm.gray)
        plt.imshow(arr2, alpha=.95, cmap=cm.gray)
        plt.imshow(bt_diff2, alpha=.90, cmap='RdBu_r', vmin=np.nanmin(bt_diff2), vmax=np.abs(np.nanmin(bt_diff2)))
        plt.colorbar()
        plt.savefig(metadata['plots']['normalized_difference_image'])
        plt.close()
    
    metadata_path = os.path.join(pair_dir, 'metadata.json')
    json.dump(metadata,open(metadata_path, 'w+'), default=utils.date_converter)

    index_path = os.path.join(pair_dir, 'index.json')

    index = {}
    print(GeoDataset(img1_cropped_path).footprint.ExportToWkt())
    print(GeoDataset(img2_cropped_path).footprint.ExportToWkt())

    index['overlap_geom'] = overlap_geom.ExportToWkt()
    index['img1_geom'] =  img1_footprint.ExportToWkt()
    index['img2_geom'] =  img2_footprint.ExportToWkt()
    index['id'] = '{}_{}'.format(id1, id2)
    json.dump(index, open(index_path, 'w+'))

    utils.print_dict(index)
    logger.info("Complete")
