"""boom: a Python client for the BOOM API.

Talks to the main BOOM API, the one backing the alert pipeline: free-form
Mongo queries over the catalogs, cone searches, aggregation pipelines and
cutouts, plus an analysis layer that turns the raw catalog documents into
typed sources with cleaned photometry.

It needs a BOOM account, which is provisioned by an admin. If you only
have a Babamul account, this package cannot authenticate you: use the
``babamul`` package, which serves the public, curated endpoints. This
package builds on it and reuses its models and plotting.
"""

from .cache import Cache
from .client import BoomClient, arcsec_to_metres, get_user_agent
from .config import API_URLS, BoomConfig, get_base_url
from .exceptions import (
    APIAuthenticationError,
    APIError,
    APINotFoundError,
    BoomError,
    ConfigurationError,
    QueryError,
)
from .models import COLUMNS, Observation, Source
from .objects import (
    alert_collection,
    aux_collection,
    get_alerts,
    get_object,
    get_radec,
    get_source,
    photometry,
    plot_cutouts,
    plot_lightcurve,
)
from .photometry import CUTS, QualityCuts, build_photometry
from .projections import alert_projection, aux_projection

__all__ = [
    "API_URLS",
    "APIAuthenticationError",
    "APIError",
    "APINotFoundError",
    "COLUMNS",
    "CUTS",
    "BoomClient",
    "BoomConfig",
    "BoomError",
    "Cache",
    "ConfigurationError",
    "Observation",
    "QualityCuts",
    "QueryError",
    "Source",
    "alert_collection",
    "alert_projection",
    "arcsec_to_metres",
    "aux_collection",
    "aux_projection",
    "build_photometry",
    "get_alerts",
    "get_base_url",
    "get_object",
    "get_radec",
    "get_source",
    "get_user_agent",
    "photometry",
    "plot_cutouts",
    "plot_lightcurve",
]
