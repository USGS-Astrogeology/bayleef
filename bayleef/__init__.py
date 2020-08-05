__version__ = "0.2.2"

# The USGS API endpoint
USGS_API = "https://earthexplorer.usgs.gov/inventory/json"

#
# Four catalogs are available for querying
#

CATALOG_NODES = ["CWIC", "EE", "HDDS", "LPVS"]

# http://lsiexplorer.cr.usgs.gov/
CWIC_LSI_EXPLORER_CATALOG_NODE = "CWIC"

# http://earthexplorer.usgs.gov/
EARTH_EXPLORER_CATALOG_NODE = "EE"

# http://hddsexplorer.usgs.gov/
HDDS_EXPLORER_CATALOG_NODE = "HDDS"

# http://lpvsexplorer.cr.usgs.gov/
LPCS_EXPLORER_CATALOG_NODE = "LPCS"

class USGSError(Exception):
    pass

class USGSApiKeyRequiredError(Exception):
    pass

class USGSAmbiguousNode(Exception):
    pass

class USGSDependencyRequired(ImportError):
    pass

from bayleef.examples import get_path
import os
import yaml
from pathlib import Path
from shutil import copyfile

class dotdict(dict):
     """dot.notation access to dictionary attributes"""
     def __getattr__(self, name):
         o = dict.get(self, name)
         if isinstance(o, dict):
             return dotdict(o)
         else:
             return o

     __setattr__ = dict.__setitem__
     __delattr__ = dict.__delitem__

config_file = os.path.join(get_path('config'), 'config.yaml')
config = None

os.makedirs(os.path.join(str(Path.home()), ".bayleef"), exist_ok=True)
if not os.path.isfile(os.path.join(str(Path.home()), ".bayleef", "config.yaml")):
    copyfile(config_file, os.path.join(str(Path.home()), ".bayleef", "config.yaml"))
    config = dotdict(yaml.load(open(config_file), Loader=yaml.BaseLoader))
else:
    config = dotdict(yaml.load(open(os.path.join(str(Path.home()), ".bayleef", "config.yaml")), Loader=yaml.BaseLoader))

config_file = os.path.join(str(Path.home()), ".bayleef", "config.yaml")
