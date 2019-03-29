import errno
import hashlib
import json
import math
import os
import re
import traceback
from datetime import date, datetime
from glob import glob
from os import path
from shutil import copyfile

import matplotlib.animation as animation
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pandas.io import sql
from matplotlib import rcParams
from mpl_toolkits.axes_grid1 import make_axes_locatable
from pylab import rcParams
from sqlalchemy import *
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm.session import sessionmaker


import logging
logger = logging.getLogger('Bayleef')

import bayleef
import gdal
import geoalchemy2
import geopandas as gpd
import osr
import plio
import pvl
import shapely
from bayleef.utils import apply_dict, get_path, keys_to_lower
from geoalchemy2 import Geometry, WKTElement
from geoalchemy2.shape import from_shape
from plio.io.io_gdal import GeoDataset
from shapely import wkt
from shapely.geometry import Polygon
from io import StringIO


def landsat_8_c1_to_sql(folder, engine):
    """
    For tagging day night, the following info is used from USGS EROS:

    Field Definition:  Nearest WRS Row to the Line-Of-Sight scene center.
    This is used primarily for scenes with off-nadir look angles.
    The center of scene for off-nadir imaging may be several paths
    left or right of the orbital path and the center may even be
    off the WRS-2 grid when near the poles. This is an estimated value, for reference.

    Values:
    001-059 = Northern Hemisphere (Descending)
    060 = Equator (Descending)
    061-119 = Southern Hemisphere (Descending)
    120-122 = Southern Polar Zone (Descending)
    123-183 = Southern Hemisphere (Ascending)
    184 = Equator (Ascending)
    185-246 = Northern Hemisphere (Ascending)
    247-248 = Northern Polar Zone (Descending)

    For acquisitions near the poles, it is possible to
    look off-nadir toward the pole, into an area not defined by the WRS-2 grid
    (above 82.61 degrees). To allow unique Target Row assignments, the North Pole
    area is assigned a row of 88n, and the South Pole area is assigned a row of 99n,
    where n is a sequential number. Up to seven scenes can be covered in these areas;
    therefore, the scenes are assigned row numbers 880 to 886, or 990 to 996.

    """
    try:
        metafile = glob(folder+'/*MTL.txt')[0]
        ang = glob(folder+'/*ANG.txt')[0]
        bqa = glob(folder+'/*BQA.TIF')[0]
        bands = [
             glob(folder+'/*B1.TIF')[0],
             glob(folder+'/*B2.TIF')[0],
             glob(folder+'/*B3.TIF')[0],
             glob(folder+'/*B4.TIF')[0],
             glob(folder+'/*B5.TIF')[0],
             glob(folder+'/*B6.TIF')[0],
             glob(folder+'/*B7.TIF')[0],
             glob(folder+'/*B8.TIF')[0],
             glob(folder+'/*B9.TIF')[0],
             glob(folder+'/*B10.TIF')[0],
             glob(folder+'/*B11.TIF')[0]
        ]
    except IndexError as e:
        print(traceback.format_exc())
        raise ValueError("{} is not a valid landsat folder: {}".format(folder, e))

    metadata = pvl.load(metafile)[0][1]
    # Change all keys to lowercase since postgres makes uppercase names a pain
    keys_to_lower(metadata)

    # Get the Primary Key
    pk = metadata['metadata_file_info']['landsat_scene_id']
    is_day = not int(metadata['product_metadata']['wrs_row']) in range(123,245)
    metadata['image_attributes']['is_daytime'] = is_day

    # Strip Timestamp info from all dates, causes to_sql errors
    apply_dict(metadata, lambda x: str(x).split('+')[0] if isinstance(x, datetime) else x)

    # Process Metadata into the database
    for key in metadata.keys():
        # change the ID name
        submeta = metadata[key]
        submeta['landsat_scene_id'] = pk
        df = gpd.GeoDataFrame.from_dict(submeta, orient='index').T
        df.to_sql(key, engine, index=False, schema='landsat_8_c1', if_exists='append')

    # Get spatiotemporal data
    pm = metadata['product_metadata']

    ll = pm['corner_ll_lon_product'], pm['corner_ll_lat_product']
    lr = pm['corner_lr_lon_product'], pm['corner_lr_lat_product']
    ul = pm['corner_ul_lon_product'], pm['corner_ul_lat_product']
    ur = pm['corner_ur_lon_product'], pm['corner_ur_lat_product']

    footprint = shapely.geometry.Polygon([ll, ul, ur, lr])
    time = metadata['product_metadata']['date_acquired']
    image_record = {
        'geom' : WKTElement(Polygon([ll, ul, ur, lr]), srid=4326),
        'time' : metadata['product_metadata']['date_acquired'],
        'landsat_scene_id' : pk,
        'b1' : bands[0],
        'b2' : bands[1],
        'b3' : bands[2],
        'b4' : bands[3],
        'b5' : bands[4],
        'b6' : bands[5],
        'b7' : bands[6],
        'b8' : bands[7],
        'b9' : bands[8],
        'b10' : bands[9],
        'b11' : bands[10],
        'bqa' : bqa,
        'metafile' : metafile,
        'ang' : ang
    }
    gpd.GeoDataFrame(image_record, index=[0]).to_sql('images', engine, schema='landsat_8_c1', if_exists='append', index=False,
                                                        dtype={'geom': Geometry('POLYGON', srid=4326)})



def master_to_sql(directories, engine):
    """
    """
    metarecs = []
    imagerecs = []

    if isinstance(directories, str):
        directories = [directories]

    for directory in directories:
        ID = os.path.basename(directory)
        hdfs = os.path.join(directory, '{}.tif'.format(ID))
        files = glob(os.path.join(directory,'*.tif'))
        files = sorted(files)
        filecolumns = ['id', 'time', 'geom', 'original'] + [os.path.basename(os.path.splitext(file)[0]).lower() for file in files]

        try:
            # open any file to get metadata
            ds = GeoDataset(directory+'/CalibratedData_Geo.tif')
            meta = ds.metadata
            meta['id'] = ID

            # array formatting for postgres
            meta['scale_factor'] = '{'+meta['scale_factor']+'}'

            for key in meta.keys():
                val = meta.pop(key)
                meta[key.lower()] = val

            del ds

            date = datetime.strptime(meta['completiondate'] , "%d-%b-%Y %H:%M:%S")

            ll = float(meta['lon_ll']), float(meta['lat_ll'])
            lr = float(meta['lon_lr']),float (meta['lon_lr'])
            ul = float(meta['lon_ul']), float(meta['lon_ul'])
            ur = float(meta['lon_ur']), float (meta['lon_ur'])

            footprint = WKTElement(Polygon([ll, ul, ur, lr]), srid=4326)

            images_data = [ID, date, footprint, hdfs] + files
        except Exception as e:
            print(e)
            continue
        metarecs.append(meta)
        imagerecs.append(images_data)

    metadf = pd.DataFrame(metarecs)
    imagedf = gpd.GeoDataFrame(imagerecs, columns=filecolumns)

    imagedf.to_sql('images', engine, schema='master', if_exists='append', index=False, dtype={'geom': Geometry('POLYGON', srid=4326)})
    metadf.to_sql('image_attributes', engine, schema='master', if_exists='append', index=False)


def get_meta(directory):
    """
    Grabs metadata and returns a record dictionary, only really used for creating
    tables being uploaded to Postgresself.

    Parameters
    ----------

    directory : str
                Folder with the bayleef package

    Returns
    -------
    : dict
      Dictionary with row data

    """
    if os.path.exists(path.join(directory, 'meta.json')):
        metafile = path.join(directory, 'meta.json')
    else:
        metafile = path.join(directory, 'metadata.json')

    index_file = path.join(directory, 'index.json')

    meta = json.dumps(json.load(open(metafile, 'r')))
    indices = json.load(open(index_file, 'r'))

    columns = ['id', 'meta']
    data = [indices['id'], meta]
    return dict(zip(columns, data))


def get_indices(directory):
    """
    Grabs indices from bayleef package and returns a record dictionary, only really used for creating
    tables being uploaded to Postgresself.

    Parameters
    ----------

    directory : str
                Folder with the bayleef package

    Returns
    -------
    : dict
      Dictionary with row data
    """
    index_file = path.join(directory, 'index.json')
    indices = json.load(open(index_file, 'r'))

    data = {}

    for key in indices.keys():
        if "time" in key:
            data[key] = datetime(**indices[key])
        if "geom" in key:
            data[key] = WKTElement(indices[key])
        else:
            data[key] = indices[key]
    return data


def get_imagedata(directory):
    """
    Grabs proccessed images and files from bayleef package and returns a record dictionary, only really used for creating
    tables being uploaded to Postgresself.

    Parameters
    ----------

    directory : str
                Folder with the bayleef package

    Returns
    -------
    : dict
      Dictionary with row data
    """
    index_file = path.join(directory, 'index.json')
    indices = json.load(open(index_file, 'r'))

    images_path = path.join(directory, 'imagedata')
    files = glob(path.join(images_path, '*'))
    print(files)
    columns = ['id'] + [path.splitext(path.basename(f))[0] for f in files]
    data = [indices['id']] + files

    return dict(sorted(zip(columns, data)))


def get_originaldata(directory):
    """
    Grabs original images and files from bayleef package and returns a record dictionary, only really used for creating
    tables being uploaded to Postgresself.

    Parameters
    ----------

    directory : str
                Folder with the bayleef package

    Returns
    -------
    : dict
      Dictionary with row data
    """
    index_file = path.join(directory, 'index.json')
    indices = json.load(open(index_file, 'r'))

    ogdata_path = path.join(directory, 'original')
    files = glob(path.join(ogdata_path, '*'))

    columns = ['id'] + [path.splitext(path.basename(f))[0] for f in files]
    data = [indices['id']] + files
    return dict(sorted(zip(columns, data)))


def serial_upload(dataset, files, engine, chunksize=100, unique_only=True, srid=4326):
    """
    Uploads files from given dataset. All files must be from the same dataset.

    TODO: The boilerplate is real

    parameters
    ----------

    dataset : str
             case insensative dataset name (e.g. MASTER, LANDSAT_8_C1)

    files : list
            List of directories to upload

    engine : sqlalchemy engine

    chunksize : chunks files into n bunches

    unique_only : If True, pulls all indices from DB and uses them to remove
                  duplicates before upload. If False, this step is skipped, which
                  means if any file is a duplicate, the entire transaction fails.

    srid : geometry reference system for geometries

    """
    nfiles = len(files)
    chunks = [files[i:i + chunksize] for i in range(0, nfiles, chunksize)]
    connection = engine.raw_connection()
    cursor = connection.cursor()
    dataset = dataset.lower()
    current_ids_in_db = None
    # Probably some sqlalchemy magic I can do instead of straight SQL
    cursor.execute('CREATE SCHEMA IF NOT EXISTS {};'.format(dataset))
    connection.commit()
    if unique_only:
        try:
            current_ids_in_db = set(pd.read_sql('select id from {}.indices'.format(dataset), engine)['id'])
        except:
            current_ids_in_db = {}

    try:

        pd_engine = sql.SQLDatabase(engine)

        # too much boilerplate...
        for chunk in chunks:
            logger.info("Uploading:\n{}".format(chunk))
            indices = gpd.GeoDataFrame.from_dict([get_indices(file) for file in chunk])
            meta = pd.DataFrame.from_dict([get_meta(file) for file in chunk])
            imagedata = pd.DataFrame.from_dict([get_imagedata(file) for file in chunk])
            ogdata = pd.DataFrame.from_dict([get_originaldata(file) for file in chunk])
            index_columns = indices.columns
            geo_columns = [c for c in index_columns if "geom" in c]
            index_dtypes = dict(zip(geo_columns, [Geometry('POLYGON')]*len(geo_columns)))

            # create the table using pandas to generate the schema, this avoids
            # some convoluded sqlalchemy code, although is this any better?
            indices_table = pd.io.sql.SQLTable('indices', pd_engine, frame=indices, schema=dataset, index=False, if_exists='append',dtype=index_dtypes)
            meta_table = pd.io.sql.SQLTable('meta', pd_engine, frame=meta, schema=dataset, index=False, if_exists='append',dtype={'meta':JSONB})
            imagedata_table = pd.io.sql.SQLTable('imagedata', pd_engine, frame=imagedata, schema=dataset, index=False, if_exists='append')
            ogdata_table = pd.io.sql.SQLTable('original', pd_engine, frame=ogdata, schema=dataset, index=False, if_exists='append')

            if unique_only:
                indices_dups = indices.isin({'id' : current_ids_in_db})['id']
                meta_dups = meta.isin({'id' : current_ids_in_db})['id']
                imagedata_dups = imagedata.isin({'id' : current_ids_in_db})['id']
                ogdata_dups = ogdata.isin({'id' : current_ids_in_db})['id']

                # remove dups before upload
                indices = indices[~indices_dups]
                meta = meta[~meta_dups]
                imagedata = imagedata[~imagedata_dups]
                ogdata = ogdata[~ogdata_dups]

            if all([indices.empty, meta.empty, imagedata.empty, ogdata.empty]):
                raise Exception('All Tables are empty')

            if not indices_table.exists():
                cursor.execute(indices_table.sql_schema())
            if not meta_table.exists():
                cursor.execute(meta_table.sql_schema())
            if not imagedata_table.exists():
                cursor.execute(imagedata_table.sql_schema())
            if not ogdata_table.exists():
                cursor.execute(ogdata_table.sql_schema())

            indices = indices.to_csv(None, sep='\t', quotechar="'", header=False, index=False)
            meta = meta.to_csv(None, sep='\t', quotechar="'", header=False, index=False)
            imagedata = imagedata.to_csv(None, sep='\t', quotechar="'", header=False, index=False)
            ogdata = ogdata.to_csv(None, sep='\t', quotechar="'", header=False, index=False)

            cursor.copy_from(StringIO(indices), '{}.indices'.format(dataset), null="")
            cursor.copy_from(StringIO(meta), '{}.meta'.format(dataset), null="")
            cursor.copy_from(StringIO(imagedata), '{}.imagedata'.format(dataset), null="")
            cursor.copy_from(StringIO(ogdata), '{}.original'.format(dataset), null="")

        indices_pk_create = 'ALTER TABLE {}.indices ADD PRIMARY KEY (id);'.format(dataset)
        meta_pk_create = 'ALTER TABLE {}.meta ADD PRIMARY KEY (id);'.format(dataset)
        imagedata_pk_create = 'ALTER TABLE {}.imagedata ADD PRIMARY KEY (id);'.format(dataset)
        original_pk_create = 'ALTER TABLE {}.original ADD PRIMARY KEY (id);'.format(dataset)
        cursor.execute(indices_pk_create)
        cursor.execute(meta_pk_create)
        cursor.execute(imagedata_pk_create)
        cursor.execute(original_pk_create)

        for column in index_columns:
            if "geom" in column:
                geo_index_create = 'CREATE INDEX IF NOT EXISTS idx_{}_indices_geom ON {}.indices USING gist({});'.format(dataset, dataset, column)
                cursor.execute(geo_index_create)
            if "time" in column:
                time_index_create = 'CREATE INDEX IF NOT EXISTS ids_{}_indices_time ON {}.indices USING BTREE(time);'.format(dataset, dataset, column)
                cursor.execute(time_index_create)

        connection.commit()
    except Exception as e:
        connection.rollback()
        raise Exception('*** ERROR - ROLLIING BACK CHANGES ***\n{}'.format(e))
    finally:
        cursor.close()
        connection.close()



func_map = {
    'LANDSAT_8_C1' : landsat_8_c1_to_sql,
    'MASTER' : serial_upload
}
