__version__ = "0.6.1"

# Sign-in and Garmin access through MyAuth come from the MyCommons building
# block `access` (garmin_mcp/access/ is a copy, see MyCommons/sync.sh).
from .access import myauth as _myauth

_myauth.configure(app="mcp-garmin", platform="Garmin")
