# highload_config.py
from auth import app as orig_app  # or wherever your current app config is

# This module just exposes a dict you can import and plug in later
SQLALCHEMY_ENGINE_OPTIONS = {
    "pool_size": 15,        # number of persistent connections
    "max_overflow": 10,     # extra connections during spikes
    "pool_pre_ping": True,  # recycle dead connections
    "pool_recycle": 1800,   # recycle every 30 minutes
}