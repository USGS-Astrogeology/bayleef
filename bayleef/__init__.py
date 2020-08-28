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

def check_file_or_path(path, file=False):
    """checks if file or path exists."""
    if not path:
        return False, f"is missing or not set"
    if not os.path.exists(path):
        return False, f"path '{path}' does not exist"
    if file and not os.path.isfile(path):
        return False, f"file '{path}' does not exist"
    return True, ''

def loop_params(config, params, file=False):
    """loops over keys in 'params' in the config dict."""
    errors = []
    for param in params:
        ok, msg = check_file_or_path(config.get(param, ''), file)
        if not ok:
            errors.append(f"\t'{param}' {msg}.")
    return errors    

def config_check(config, config_path):
    """checks if the paths in the config.yaml file exist"""
    errors = [f"Your configuration file {config_path} is not configured properly:"]
    warnings = [f"WARNING: Your configuration file {config_path} is missing some settings.",
        "This may result in unexpected behavior or errors later."]
    # I am assuming the config file location does not get checked here.
    # The copyfile command below should throw an exception below so it would be redundant.
    errors += loop_params(config, ['data', 'davinci_bin'])
    errors += loop_params(config['themis'], ['map_file'], file=True)
    warnings += loop_params(config['themis'], ['controlled_kernels'])
    if len(errors)>1:
        if len(warnings)>2:
            errors += warnings
        raise USGSError('\n'.join(errors))
    if len(warnings)>2:
        print('\n'.join(warnings))

config_file = os.path.join(get_path('config'), 'config.yaml')
config = None

os.makedirs(os.path.join(str(Path.home()), ".bayleef"), exist_ok=True)
config_path = os.path.join(str(Path.home()), ".bayleef", "config.yaml")
if not os.path.isfile(config_path):
    copyfile(config_file, config_path)
config = dotdict(yaml.load(open(config_path), Loader=yaml.BaseLoader))
config_check(config, config_path)
config_file = config_path
