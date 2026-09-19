"""boom: a Python client for the BOOM API.

Talks to the main BOOM API, the one backing the alert pipeline: free-form
Mongo queries over the catalogs, cone searches, aggregation pipelines and
cutouts. It needs a BOOM account, which is provisioned by an admin.

If you only have a Babamul account, this package cannot authenticate you:
use the ``babamul`` package, which serves the public, curated endpoints.
This package builds on it, and reuses its models and plotting.
"""

from .cache import Cache
from .client import BoomClient, get_user_agent
from .config import API_URLS, BoomConfig, get_base_url
from .exceptions import (
    APIAuthenticationError,
    APIError,
    APINotFoundError,
    BoomError,
    ConfigurationError,
    QueryError,
)
from .objects import (
    alert_collection,
    aux_collection,
    get_alerts,
    get_object,
    photometry,
    plot_cutouts,
    plot_lightcurve,
)

__all__ = [
    "API_URLS",
    "APIAuthenticationError",
    "APIError",
    "APINotFoundError",
    "BoomClient",
    "BoomConfig",
    "BoomError",
    "Cache",
    "ConfigurationError",
    "QueryError",
    "alert_collection",
    "aux_collection",
    "get_alerts",
    "get_base_url",
    "get_object",
    "get_user_agent",
    "photometry",
    "plot_cutouts",
    "plot_lightcurve",
]
