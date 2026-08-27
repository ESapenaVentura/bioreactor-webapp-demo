import os
import json

def get_units_from_assets():
    """Get the units from the assets file."""
    cwd = os.path.dirname(os.path.abspath(__file__))
    assets_file = os.path.join(cwd, "assets/units.json")
    units = {}
    with open(assets_file, "r") as f:
        units = json.load(f)
    return units

## All the configs go here
### Server settings
SERVER_URL = os.getenv("SERVER_URL") or "opc.tcp://localhost:4840/ganymede/server/"
NAMESPACE_URI = os.getenv("NAMESPACE_URI") or "http://examples.ganymede.github.io"

## All the configs go here
### Server settings
SERVER_URL = os.getenv("SERVER_URL") or "opc.tcp://localhost:4840/ganymede/server/"
NAMESPACE_URI = os.getenv("NAMESPACE_URI") or "http://examples.ganymede.github.io"

### Runtime settings
PUBLISHING_INTERVAL_MS = int(os.getenv("PUBLISHING_INTERVAL_MS") or 500)
REACTOR = os.getenv("REACTOR") or "Cytiva Wave"
RECONNECT_MAX_SECONDS = int(os.getenv("RECONNECT_MAX_SECONDS") or 30)
UNITS = get_units_from_assets()