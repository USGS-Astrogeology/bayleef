import json
import os
import re
import subprocess
import sys

from datetime import datetime
from functools import partial, reduce
from glob import glob
from subprocess import PIPE, Popen
import yaml

import gdal
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import plio
import pvl
import wget
import osr

from plio.io.io_gdal import GeoDataset

import autocnet
from autocnet import CandidateGraph
from autocnet.graph.edge import Edge
from autocnet.matcher import suppression_funcs
from autocnet.matcher.subpixel import (clip_roi, subpixel_phase,
                                       subpixel_template)
from pysis.exceptions import ProcessError
from pysis.isis import (cam2map, campt, footprintinit, jigsaw, map2map,
                        pointreg, spiceinit)

sys.path.insert(0, os.path.abspath('..'))

import logging
logger = logging.getLogger('Bayleef')

from bayleef import config

def get_path(response, root, dataset):
    """
    """
    if isinstance(response, dict):
        response = [response]

    try:
        for data in response:
            scene_id = data['entityId']
            s = data['summary']
            date = datetime.strptime(data['acquisitionDate'], '%Y-%m-%d')
            pathstr, rowstr = re.findall(r'Path: \b\d+\b|Row: \b\d+\b', s)
            path = pathstr.split(' ')[1]
            row = rowstr.split(' ')[1]
            return os.path.join(root, dataset, str(date.year), row, path, scene_id)

    except Exception as e:
        logger.error('Failed to process request: {}'.format(e))


def keys_to_lower(dictionary):
    """
    """
    for key in dictionary.keys():
        if isinstance(dictionary[key], dict):
            keys_to_lower(dictionary[key])
        dictionary[key.lower()] = dictionary.pop(key)


def apply_dict(dictionary, func, *args, **kwargs):
    """
    """
    for key in dictionary.keys():
        if isinstance(dictionary[key], dict):
            apply_dict(dictionary[key], func)
        dictionary[key] = func(dictionary[key], *args, **kwargs)

def geolocate(infile, outfile, lats, lons, dstSRS="EPSG:4326", format="GTiff", woptions={}, toptions={}):
    """
    """
    image = gdal.Open(infile, gdal.GA_Update)
    geoloc= {
        'X_DATASET' : lons,
        'X_BAND' : '1',
        'Y_DATASET' : lats,
        'Y_BAND' : '1',
        'PIXEL_OFFSET' : '0',
        'LINE_OFFSET' : '0',
        'PIXEL_STEP' : '1',
        'LINE_STEP' : '1'
    }

    image.SetMetadata(geoloc, 'GEOLOCATION')
    # explicity close image
    del image
    gdal.Warp(outfile, infile, format=format, dstSRS=dstSRS)
    return GeoDataset(outfile)


def master_isvalid(file):

    if len(gdal.Open(file).GetSubDatasets()) != 17:
        return False

    calibrated_image = GeoDataset('HDF4_SDS:UNKNOWN:"{}":37'.format(file))
    lats = GeoDataset('HDF4_SDS:UNKNOWN:"{}":30'.format(file))
    lons = GeoDataset('HDF4_SDS:UNKNOWN:"{}":31'.format(file))

    res = []
    for ds in [calibrated_image, lats, lons]:
        arr = ds.read_array()
        test = np.empty(arr.shape)
        test[:] = ds.metadata['_FillValue']
        res.append(not (test == arr).all())
    return all(res)


def run_davinci(script, infile=None, outfile=None, bin_dir=config.davinci_bin, args=[]):
    '''
    '''
    command = ['davinci', '-f', os.path.join(bin_dir, script), 'from={}'.format(infile), 'to={}'.format(outfile)]

    # add additional positional args
    if args:
        command.extend(args)

    logger.info(' '.join(command))
    p = Popen(command, stdin=PIPE, stdout=PIPE, stderr=PIPE)
    output, err = p.communicate(b"input data that is passed to subprocess' stdin")
    rc = p.returncode

    if rc != 0:
        raise Exception('Davinci returned non-zero error code {} : \n{}\n{}'.format(rc, err.decode('utf-8'), output.decode('utf-8') ))
    return output.decode('utf-8'), err.decode('utf-8')


def init(outfile, additional_kernels={}):
    try:
        logger.info("Running Spiceinit on {}".format(outfile))
        spiceinit(from_=outfile, spksmithed=True, **additional_kernels)
    except ProcessError as e:
        logger.error('file: {}'.format(outfile))
        logger.error("STDOUT: {}".format(e.stdout.decode('utf-8')))
        logger.error("STDERR: {}".format(e.stderr.decode('utf-8')))
        raise Exception('Spice Init Error')

    try:
        logger.info("Running Footprintinit on {}".format(outfile))
        footprintinit(from_=outfile)
    except ProcessError as e:
        logger.error('file: {}'.format(outfile))
        logger.error("STDOUT: {}".format(e.stdout.decode('utf-8')))
        logger.error("STDERR: {}".format(e.stderr.decode('utf-8')))
        raise Exception('Footprint Init Error')



def thm_crop(infile, outfile, minlat, maxlat):
    run_davinci('thm_crop_lat.dv', infile, outfile, args=['minlat={}'.format(str(minlat)), 'maxlat={}'.format(maxlat)])


def match_pair(img1_path, img2_path, figpath=None):
    src_points = point_grid(GeoDataset(img1_path), step=50)
    f = open('temp.txt', 'w+')
    f.write('\n'.join('{}, {}'.format(int(x),int(y)) for x,y in src_points))
    del f

    label = pvl.loads(campt(from_=img1_path, coordlist='temp.txt', coordtype='image'))
    points = []
    for group in label:
        try:
            lat = group[1]['PlanetocentricLatitude'].value
            lon = group[1]['PositiveEast360Longitude'].value
            points.append([lat, lon])
        except Exception as e:
            continue

    logger.info("{} points from image1 successfully reprojected to image2, rejected {}".format(str(len(points)), str(len(src_points)-len(points))))

    if len(points) == 0:
        raise Exception("No valid points were found for pair {} {}".format(img1_path, img2_path))

    f = open('temp.txt', 'w+')
    f.write('\n'.join('{}, {}'.format(x,y) for x,y in points))
    del f

    img2label = pvl.loads(campt(from_=img2_path, coordlist='temp.txt', coordtype='ground', allowoutside=False))
    dst_lookup = {}
    for i,group in enumerate(img2label):
        if not group[1]['Error']:
            line = group[1]['Line']
            sample = group[1]['Sample']
            dst_lookup[i] = [sample, line]

    filelist = [img1_path, img2_path]
    cg = CandidateGraph.from_filelist(filelist)

    edge = cg[0][1]['data']
    img1 = GeoDataset(img1_path)
    img2 = GeoDataset(img2_path)

    src_keypoints = pd.DataFrame(data=src_points, columns=['x', 'y'])
    src_keypoints['response'] = 0
    src_keypoints['angle'] = 0
    src_keypoints['octave'] = 0
    src_keypoints['layer'] = 0
    src_keypoints
    edge.source._keypoints = src_keypoints

    results = []
    dst_keypoints = []
    dst_index = 0
    distances = []

    arr1 = img1.read_array()
    arr2 = img2.read_array()
    del img1
    del img2

    for keypoint in edge.source.keypoints.iterrows():
        index, row = keypoint

        sx, sy = row['x'], row['y']

        try:
            dx, dy =  dst_lookup[index]
        except KeyError:
            continue

        try:
            ret = refine_subpixel(sx, sy, dx, dy, arr1, arr2, size=50, reduction=10, convergence_threshold=1)
        except Exception as ex:
            continue

        if ret is not None:
            x,y,metrics = ret
        else:
            continue

        dist = np.linalg.norm([x-dx, y-dy])
        results.append([0, index, 1, dst_index, dist])
        dst_keypoints.append([x,y, 0,0,0,0,0])
        dst_index += 1


    matches = pd.DataFrame(data=results, columns=['source_image', 'source_idx',
                                                  'destination_image', 'destination_idx',
                                                  'distance'])

    if matches.empty:
        logger.error("After matching points, matches dataframe returned empty.")

    dst_keypoints = pd.DataFrame(data=dst_keypoints, columns=['x', 'y', 'response', 'size', 'angle', 'octave', 'layer'])
    edge.destination._keypoints = dst_keypoints



    edge._matches = matches
    edge.compute_fundamental_matrix()
    distance_check(edge, clean_keys=['fundamental'])

    if figpath:
        plt.figure(figsize=(10,25))
        cg[0][1]['data'].plot(clean_keys=['fundamental', 'distance'], nodata=-32768.0)
        plt.savefig(figpath)
        plt.close()
    return cg



def refine_subpixel(sx, sy, dx, dy, s_img, d_img, size=100, reduction=25, convergence_threshold=.5):
    """
    Iteratively apply a subpixel phase matcher to source (s_img) amd destination (d_img)
    images. The size parameter is used to set the initial search space. The algorithm
    is recursively applied to reduce the total search space by reduction until the convergence criteria
    are met. Convergence is defined as the point at which the computed shifts (x_shift,y_shift) are
    less than the convergence_threshold. In instances where the size is reducted to 1 pixel the
    algorithm terminates and returns None.

    Parameters
    ----------
    sx : numeric
         The x position of the center of the template to be matched to
    sy : numeric
         The y position of the center of the template to be matched to
    dx : numeric
         The x position of the center of the search to be matched from
    dy : numeric
         The y position of the center of the search to be matched to

    s_img : object
            A plio geodata object from which the template is extracted
    d_img : object
            A plio geodata object from which the search is extracted
    size : int
           One half of the total size of the template, so a 251 default results in a 502 pixel search space
    reduction : int
                With each recursive call to this func, the size is reduced by this amount
    convergence_threshold : float
                            The value under which the result can shift in the x and y directions to force a break

    Returns
    -------
    dx : float
         The new x value for the match in the destination (d) image
    dy : float
         The new y value for the match in the destination (d) image
    metrics : tuple
              A tuple of metrics. In the case of the phase matcher this are difference
              and RMSE in the phase dimension.
    """
    s_template, _, _ = clip_roi(s_img, sx, sy,
                             size_x=size, size_y=size)
    d_search, dxr, dyr = clip_roi(d_img, dx, dy,
                           size_x=size, size_y=size)
    if s_template.shape != d_search.shape:
        s_size = s_template.shape
        d_size = d_search.shape
        updated_size = int(min(s_size + d_size) / 2)
        s_template, _, _ = clip_roi(s_img, sx, sy,
                             size_x=updated_size, size_y=updated_size)
        d_search, dxr, dyr = clip_roi(d_img, dx, dy,
                            size_x=updated_size, size_y=updated_size)

    # Apply the phase matcher
    shift_x, shift_y, metrics = subpixel_phase(s_template, d_search, upsample_factor=100)
    # Apply the shift to d_search and compute the new correspondence location
    dx += (shift_x + dxr)
    dy += (shift_y + dyr)
    # Break if the solution has converged
    if abs(shift_x) < convergence_threshold and abs(shift_y) < convergence_threshold:
        return dx, dy, metrics
    else:
        size -= reduction
        if size < 1:
            return
        return refine_subpixel(sx, sy,  dx, dy, s_img, d_img, size)

def normalize_image_res(image1, image2, image2out, image1out, out_type='ISIS3', nodata=-32768.0):
    width = max(image1.pixel_width, image2.pixel_width)

    f1 = gdal.Warp('/vsimem/temp1.out', image1.file_name, targetAlignedPixels=True, xRes = width, yRes = width, format = out_type)
    f2 = gdal.Warp('/vsimem/temp2.out', image2.file_name, targetAlignedPixels=True, xRes = width, yRes = width, format = out_type)
    del(f1, f2)

    temp1 = GeoDataset('/vsimem/temp1.out')
    temp2 = GeoDataset('/vsimem/temp2.out')
    minx = 0
    miny = 0
    maxx = max(temp1.read_array().shape[1], temp2.read_array().shape[1])
    maxy = max(temp1.read_array().shape[0], temp2.read_array().shape[0])

    fp1 = gdal.Translate(image1out, '/vsimem/temp1.out', srcWin = [minx, miny, maxx - minx, maxy - miny], noData=nodata)
    fp2 = gdal.Translate(image2out, '/vsimem/temp2.out', srcWin = [minx, miny, maxx - minx, maxy - miny], noData=nodata)
    del(fp1, fp2)


def preprocess(thm_id, outdir, day=True, validate=False, projected_images=True, map_file=config.themis.map_file, originals=True, gtiffs=False, meta=True, index=True):
    '''
    Downloads Themis file by ID and runs it through spice init and
    footprint init.
    '''
    original = os.path.join(outdir, 'original')
    images = os.path.join(outdir, 'images')

    ogcube = os.path.join(original, 'l1.cub')
    projcube = os.path.join(original, 'l2.cub')
    metafile = os.path.join(outdir, 'meta.json')
    indexfile = os.path.join(outdir, 'index.json')


    os.makedirs(original, exist_ok=True)
    os.makedirs(images, exist_ok=True)

    kerns = get_controlled_kernels(thm_id)

    if os.path.exists(outdir) and os.path.exists(original) and os.path.exists(metafile) and os.path.exists(indexfile) :
        logger.info("File {} Exists, skipping redownload.".format(outdir))
        return bool(kerns)

    if originals:
        if day:
            out, err = run_davinci('thm_pre_process.dv', infile=thm_id, outfile=ogcube)
        else:
            out, err = run_davinci('thm_pre_process_night.dv', infile=thm_id, outfile=ogcube)


        if validate:
            try:
                init(ogcube, additional_kernels=kerns)
                label = pvl.loads(campt(from_=ogcube))
            except ProcessError as e:
                logger.info('campt Error')
                logger.info('file: {}'.format(outfile))
                logger.error("STDOUT: {}".format(e.stdout.decode('utf-8')))
                logger.error("STDERR: {}".format(e.stderr.decode('utf-8')))

            incidence_angle = label['GroundPoint']['Incidence'].value

            if day and incidence_angle > 90:
                logger.info("incidence angle suggests night, but {} was proccessed for day, reprocessing".format(thm_id))
                out, err = run_davinci('thm_pre_process_night.dv', infile=thm_id, outfile=ogcube)
                init(ogcube, additional_kernels=kerns)
            elif not day and incidence_angle <= 90:
                logger.info("incidence angle suggests day, but {} was proccessed for night, reprocessing".format(thm_id))
                out, err = run_davinci('thm_pre_process.dv', infile=thm_id, outfile=ogcube)
                init(ogcube, additional_kernels=kerns)

        else:
            init(ogcube, additional_kernels=kerns)

        if projected_images:
            project(ogcube, projcube, map_file)

    img = GeoDataset(ogcube)

    if meta:
        meta = json.loads(json.dumps(img.metadata, default = lambda o:str(o) if isinstance(o, datetime) else o))
        try:
            meta['map_file'] = str(pvl.load(map_file))
        except Exception as e:
            logger.error("Failed to load map file {}:\n{}".format(map_file, e))
            raise Exception("Invalid map file.")

        json.dump(meta, open(metafile, 'w+'))
        if kerns:
            logger.info('Used Controlled Kernels')
            meta['used_control_kernels'] = True

    if index:
        date = img.metadata['IsisCube']['Instrument']['StartTime']
        index_meta = {}
        index_meta['geom'] = img.footprint.ExportToWkt()
        index_meta['id'] = thm_id
        index_meta['time'] = {}
        index_meta['time']['year'] = date.year
        index_meta['time']['month'] = date.month
        index_meta['time']['day'] = date.day
        index_meta['time']['hour'] = date.hour
        nbands = img.nbands
        json.dump(index_meta, open(indexfile, 'w+'))

    del img

    if gtiffs:
        for band in range(1,nbands+1):
            tiffpath = os.path.join(images, 'b{}.tiff'.format(band))
            logger.info('Writing: {}'.format(tiffpath))
            gdal.Translate(tiffpath, ogcube, bandList=[band], format='GTiff')

    return bool(kerns)


def date_converter(o):
    if isinstance(o, np.ndarray):
            return o.tolist()
    if isinstance(o, datetime):
        return o.isoformat()

def print_dict(d):
    print(str(yaml.dump(json.loads(json.dumps(d, default=date_converter)), default_flow_style=False )))

def point_grid(img, nodata=-32768.0, step=50):
    arr = img.read_array()
    xs = np.linspace(0, arr.shape[1]-1, num=arr.shape[1]/step)
    ys = np.linspace(0, arr.shape[0]-1, num=arr.shape[0]/step)

    # array of x,y pairs
    points = np.transpose([np.tile(xs, len(ys)), np.repeat(ys, len(xs))])
    points = [p for p in points if arr[int(p[1])][int(p[0])] != nodata]

    return points

def distance_check(e, clean_keys=[]):
    matches, mask = e.clean(clean_keys)
    thresh = np.percentile(matches['distance'], 90)
    mask = np.ones(mask.shape[0], dtype=bool)
    mask[e.matches['distance'] >= thresh] = False

    e.masks['distance'] = mask


def project(img, to, mapfile, matchmap=False):
    params = {
        'from_' : img,
        'map' : mapfile,
        'to' : to,
        'matchmap': matchmap
    }

    if GeoDataset(img).metadata['IsisCube'].get('Mapping', False):
        try:
            params['interp'] = 'NEARESTNEIGHBOR'
            logger.info('Running map2map on {} with params {}'.format(img, params))
            map2map(**params)
        except ProcessError as e:
            logger.info('map2map Error')
            logger.error("STDOUT: {}".format(e.stdout.decode('utf-8')))
            logger.error("STDERR: {}".format(e.stderr.decode('utf-8')))
    else:
        try:
            logger.info('Running cam2map on {}'.format(img))
            cam2map(**params)
        except ProcessError as e:
            logger.info('cam2map Error')
            logger.error("STDOUT: {}".format(e.stdout.decode('utf-8')))
            logger.error("STDERR: {}".format(e.stderr.decode('utf-8')))


def get_controlled_kernels(thmid, kernel_dir=config.themis.controlled_kernels, day=True):
    if not kernel_dir:
        return {}

    found = False
    if day:
        kernels = os.path.join(kernel_dir, 'DIR')
    else:
        kernels = os.path.join(kernel_dir, 'NIR')

    files = glob(os.path.join(kernels, '*.txt'))
    for f in files:
        contents = open(f, 'r').read()
        if thmid in contents:
            found = True
            break

    return {
        'ck' : glob(os.path.join(kernels, '*_ck.bc'))[0],
        'spk' : glob(os.path.join(kernels, '*_spk.bsp'))[0]
    } if found else {}


def array2raster(rasterfn, array, newRasterfn):
    """
    Writes an array to a GeoDataset using another dataset as reference. Borrowed
    from: https://pcjericks.github.io/py-gdalogr-cookbook/raster_layers.html

    Parameters
    ----------
    rasterfn : str, GeoDataset
               Dataset or path to the dataset to use as a reference. Geotransform
               and spatial reference information is copied into the new image.

    array : np.array
            Array to write

    newRasterfn : str
                  Filename for new raster image

    Returns
    -------
    : GeoDataset
      File handle for the new raster file

    """
    naxis = len(array.shape)
    assert naxis == 2 or naxis == 3

    if naxis == 2:
        # exapnd the third dimension
        array = array[:,:,None]

    nbands = array.shape[2]

    if isinstance(rasterfn, GeoDataset):
        rasterfn = rasterfn.file_name

    raster = gdal.Open(rasterfn)
    geotransform = raster.GetGeoTransform()
    originX = geotransform[0]
    originY = geotransform[3]
    pixelWidth = geotransform[1]
    pixelHeight = geotransform[5]
    cols = array.shape[1]
    rows = array.shape[0]

    driver = gdal.GetDriverByName('GTiff')
    outRaster = driver.Create(newRasterfn, cols, rows, nbands, gdal.GDT_Float32)
    outRaster.SetGeoTransform((originX, pixelWidth, 0, originY, 0, pixelHeight))

    for band in range(1,nbands+1):
        outband = outRaster.GetRasterBand(band)
        # Bands use indexing starting at 1
        outband.WriteArray(array[:,:,band-1])
        outband.FlushCache()

    outRasterSRS = osr.SpatialReference()
    outRasterSRS.ImportFromWkt(raster.GetProjectionRef())
    outRaster.SetProjection(outRasterSRS.ExportToWkt())
    outRaster = None
    return GeoDataset(newRasterfn)
