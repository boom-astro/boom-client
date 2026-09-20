"""HTTP client for the BOOM API."""

from __future__ import annotations

import base64
import logging
import platform
import time
from collections.abc import Iterator, Mapping, Sequence
from functools import lru_cache
from typing import Any, Literal, cast

import httpx
import numpy as np

from .config import (
    DEFAULT_BACKOFF_FACTOR,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    BoomConfig,
)
from .exceptions import (
    APIAuthenticationError,
    APIError,
    APINotFoundError,
    ConfigurationError,
    QueryError,
)

logger = logging.getLogger(__name__)

Survey = Literal["ZTF", "LSST", "DECAM", "WINTER"]
WhichCutouts = Literal["first", "last", "brightest", "faintest"]

#: Sent with every query unless overridden. The API applies no server-side
#: limit, so an unselective filter would otherwise run until Mongo gives up.
DEFAULT_MAX_TIME_MS = 60_000

#: Rows fetched per request by :meth:`BoomClient.iter_find`.
DEFAULT_PAGE_SIZE = 1000

#: Mean Earth radius in metres. Mongo measures ``$nearSphere`` distances on
#: a sphere of this size, so an angular radius has to be converted.
EARTH_RADIUS_M = 6371008.8

RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

_UNITS = {
    "deg": "Degrees",
    "degree": "Degrees",
    "degrees": "Degrees",
    "rad": "Radians",
    "radian": "Radians",
    "radians": "Radians",
    "arcsec": "Arcseconds",
    "arcsecond": "Arcseconds",
    "arcseconds": "Arcseconds",
    "arcmin": "Arcminutes",
    "arcminute": "Arcminutes",
    "arcminutes": "Arcminutes",
}


@lru_cache(maxsize=1)
def get_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("boom-client")
    except PackageNotFoundError:
        return "0.0.0+unknown"


@lru_cache(maxsize=1)
def get_user_agent() -> str:
    """Build the ``User-Agent`` header sent with every request.

    Like the babamul package, this carries nothing that identifies a machine
    or a person: only the package version, the Python version and the OS
    name, so the BOOM team can tell which client versions are still in use.
    """
    return (
        f"boom-client-python/{get_version()} "
        f"(Python/{platform.python_version()}; "
        f"{platform.system() or 'unknown'})"
    )


def normalize_unit(unit: str) -> str:
    """Map a friendly unit name onto the enum variant the API expects."""
    normalized = _UNITS.get(str(unit).strip().lower())
    if normalized is None:
        raise ValueError(
            f"Unknown unit {unit!r}. Use one of "
            "'deg', 'rad', 'arcsec', 'arcmin'."
        )
    return normalized


def normalize_radius(radius: Any, unit: str) -> tuple[float, str]:
    """Return ``(value, api_unit)``, unwrapping an astropy Quantity."""
    if hasattr(radius, "to") and hasattr(radius, "unit"):
        import astropy.units as u

        return float(radius.to(u.deg).value), "Degrees"
    return float(radius), normalize_unit(unit)


def normalize_coordinates(
    coordinates: Any,
) -> dict[str, list[float]]:
    """Normalize user coordinates into the API's ``{name: [ra, dec]}`` map.

    Accepts a ``SkyCoord`` (scalar or array), an ``(ra, dec)`` pair, a
    sequence of pairs, or a mapping of name to pair. Unnamed inputs are
    keyed by their position, which is also how the results come back.
    """
    if hasattr(coordinates, "ra") and hasattr(coordinates, "dec"):
        import astropy.units as u

        ras = coordinates.ra.to(u.deg).value
        decs = coordinates.dec.to(u.deg).value
        if coordinates.isscalar:
            return {"0": [float(ras), float(decs)]}
        return {
            str(i): [float(ra), float(dec)]
            for i, (ra, dec) in enumerate(zip(ras, decs, strict=True))
        }

    if isinstance(coordinates, Mapping):
        return {
            str(name): [float(pair[0]), float(pair[1])]
            for name, pair in coordinates.items()
        }

    if (
        isinstance(coordinates, Sequence)
        and len(coordinates) == 2
        and all(isinstance(v, (int, float)) for v in coordinates)
    ):
        return {"0": [float(coordinates[0]), float(coordinates[1])]}

    if isinstance(coordinates, Sequence):
        return {
            str(i): [float(pair[0]), float(pair[1])]
            for i, pair in enumerate(coordinates)
        }

    raise TypeError(
        "coordinates must be a SkyCoord, an (ra, dec) pair, a sequence of "
        f"pairs, or a {{name: (ra, dec)}} mapping, got {type(coordinates)}"
    )


def _drop_none(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if v is not None}


class BoomClient:
    """Authenticated client for the BOOM API.

    The client logs in lazily on the first request and refreshes its token
    when the API rejects it, so a long-lived instance keeps working across
    a notebook session.

    Parameters
    ----------
    username, password : str | None
        BOOM credentials. Fall back to ``BOOM_API_USER`` and
        ``BOOM_API_PASSWORD``.
    url : str | None
        API base URL. Falls back to ``BOOM_API_URL`` then ``BOOM_ENV``.
    timeout : float
        Per-request timeout in seconds.
    max_retries : int
        Attempts on transport errors and 5xx responses.
    backoff_factor : float
        Base delay of the exponential backoff between retries.
    transport : httpx.BaseTransport | None
        Transport override, for tests.

    Examples
    --------
    >>> client = BoomClient()  # doctest: +SKIP
    >>> client.count("ZTF_alerts", {"objectId": "ZTF18abcdefg"})  # doctest: +SKIP
    42
    """

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        url: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = BoomConfig.from_env(
            username=username,
            password=password,
            url=url,
            timeout=timeout,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
        )
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._client = httpx.Client(
            base_url=self.config.url,
            timeout=self.config.timeout,
            headers={"User-Agent": get_user_agent()},
            transport=transport,
        )

    def __enter__(self) -> BoomClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def __repr__(self) -> str:
        return (
            f"BoomClient(url={self.config.url!r}, "
            f"username={self.config.username!r})"
        )

    # ---- authentication ----

    def authenticate(self) -> str:
        """Exchange the credentials for a JWT and return it."""
        try:
            response = self._client.post(
                "/auth",
                data={
                    "username": self.config.username,
                    "password": self.config.password,
                },
            )
        except httpx.RequestError as error:
            raise APIError(f"Request to /auth failed: {error}") from error

        if response.status_code == 401:
            raise APIAuthenticationError(
                "Invalid username or password.", status_code=401
            )
        if response.status_code >= 400:
            raise APIAuthenticationError(
                f"Authentication failed ({response.status_code}): "
                f"{_error_message(response)}",
                status_code=response.status_code,
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise APIAuthenticationError(
                "The API did not return an access token.",
                status_code=response.status_code,
            )
        expires_in = payload.get("expires_in")
        # Renew a minute early so a request never starts on a token that
        # expires while it is in flight.
        self._token = cast(str, token)
        self._token_expires_at = (
            time.monotonic() + float(expires_in) - 60.0
            if expires_in
            else float("inf")
        )
        return self._token

    def _auth_header(self) -> dict[str, str]:
        if self._token is None or time.monotonic() >= self._token_expires_at:
            self.authenticate()
        return {"Authorization": f"Bearer {self._token}"}

    # ---- transport ----

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Send an authenticated request and return the unwrapped ``data``."""
        last_error: Exception | None = None
        reauthenticated = False

        for attempt in range(self.config.max_retries):
            try:
                response = self._client.request(
                    method,
                    endpoint,
                    params=params,
                    json=json,
                    headers=self._auth_header(),
                )
            except httpx.RequestError as error:
                last_error = error
                self._sleep_before_retry(attempt)
                continue

            if response.status_code == 401 and not reauthenticated:
                # The token was rejected: drop it and try once with a fresh
                # one before giving up, so an expired session is invisible.
                reauthenticated = True
                self._token = None
                continue

            if response.status_code in RETRY_STATUS_CODES:
                last_error = APIError(
                    f"API error ({response.status_code}): "
                    f"{_error_message(response)}",
                    status_code=response.status_code,
                )
                self._sleep_before_retry(attempt)
                continue

            return _unwrap(response, endpoint)

        if last_error is not None:
            raise (
                last_error
                if isinstance(last_error, APIError)
                else APIError(f"Request failed: {last_error}")
            ) from last_error
        raise APIAuthenticationError(
            "Authentication kept being rejected.", status_code=401
        )

    def _sleep_before_retry(self, attempt: int) -> None:
        delay = self.config.backoff_factor * (2**attempt)
        logger.debug("retrying in %.1fs (attempt %d)", delay, attempt + 1)
        time.sleep(delay)

    # ---- queries ----

    def find(
        self,
        catalog: str,
        filter: dict[str, Any] | None = None,
        *,
        projection: dict[str, Any] | None = None,
        limit: int | None = None,
        skip: int | None = None,
        sort: dict[str, Any] | list[tuple[str, int]] | None = None,
        max_time_ms: int | None = DEFAULT_MAX_TIME_MS,
    ) -> list[dict[str, Any]]:
        """Run a Mongo ``find`` on a catalog.

        Parameters
        ----------
        catalog : str
            Catalog name, e.g. ``"ZTF_alerts"``.
        filter : dict | None
            Mongo query document. Defaults to ``{}``, which matches
            everything, so pass ``limit``.
        projection : dict | None
            Fields to include or exclude.
        limit, skip : int | None
            Standard Mongo paging. Prefer :meth:`iter_find` for large pulls.
        sort : dict | list[tuple[str, int]] | None
            Sort specification, e.g. ``{"candidate.jd": -1}``.
        max_time_ms : int | None
            Server-side time budget. ``None`` or ``0`` removes it.
        """
        return cast(
            list[dict[str, Any]],
            self._request(
                "POST",
                "/queries/find",
                json=_drop_none(
                    {
                        "catalog_name": catalog,
                        "filter": filter if filter is not None else {},
                        "projection": projection,
                        "limit": limit,
                        "skip": skip,
                        "sort": _normalize_sort(sort),
                        "max_time_ms": max_time_ms or None,
                    }
                ),
            ),
        )

    def find_one(
        self,
        catalog: str,
        filter: dict[str, Any] | None = None,
        *,
        projection: dict[str, Any] | None = None,
        sort: dict[str, Any] | list[tuple[str, int]] | None = None,
        max_time_ms: int | None = DEFAULT_MAX_TIME_MS,
    ) -> dict[str, Any] | None:
        """Return the first matching document, or ``None``."""
        documents = self.find(
            catalog,
            filter,
            projection=projection,
            limit=1,
            sort=sort,
            max_time_ms=max_time_ms,
        )
        return documents[0] if documents else None

    def iter_find(
        self,
        catalog: str,
        filter: dict[str, Any] | None = None,
        *,
        projection: dict[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        limit: int | None = None,
        sort: dict[str, Any] | list[tuple[str, int]] | None = None,
        max_time_ms: int | None = DEFAULT_MAX_TIME_MS,
    ) -> Iterator[dict[str, Any]]:
        """Page through a find query, yielding documents one by one.

        The API has no cursor, so paging is skip-based. Mongo only
        guarantees a stable order under an explicit sort, so this defaults
        to sorting on ``_id`` to keep pages from overlapping or skipping.
        """
        sort = sort if sort is not None else {"_id": 1}
        fetched = 0
        skip = 0
        while True:
            size = page_size
            if limit is not None:
                size = min(page_size, limit - fetched)
                if size <= 0:
                    return
            page = self.find(
                catalog,
                filter,
                projection=projection,
                limit=size,
                skip=skip,
                sort=sort,
                max_time_ms=max_time_ms,
            )
            yield from page
            fetched += len(page)
            skip += len(page)
            if len(page) < size:
                return

    def count(self, catalog: str, filter: dict[str, Any] | None = None) -> int:
        """Count documents matching a filter (exact, scans the index)."""
        return int(
            self._request(
                "POST",
                "/queries/count",
                json={
                    "catalog_name": catalog,
                    "filter": filter if filter is not None else {},
                },
            )
        )

    def estimated_count(self, catalog: str) -> int:
        """Approximate document count from collection metadata (cheap)."""
        return int(
            self._request(
                "POST",
                "/queries/estimated_count",
                json={"catalog_name": catalog},
            )
        )

    def cone_search(
        self,
        catalog: str,
        coordinates: Any,
        radius: Any,
        unit: str = "arcsec",
        *,
        filter: dict[str, Any] | None = None,
        projection: dict[str, Any] | None = None,
        limit: int | None = None,
        skip: int | None = None,
        sort: dict[str, Any] | list[tuple[str, int]] | None = None,
        max_time_ms: int | None = DEFAULT_MAX_TIME_MS,
    ) -> dict[str, list[dict[str, Any]]]:
        """Search a catalog around one or more sky positions.

        Parameters
        ----------
        catalog : str
            Catalog name. It must carry a ``coordinates.radec_geojson``
            field, which every alert and aux collection does.
        coordinates : SkyCoord | tuple | Sequence | Mapping
            See :func:`normalize_coordinates`.
        radius : float | astropy.units.Quantity
            Search radius. A Quantity carries its own unit and overrides
            *unit*.
        unit : str
            Unit of *radius* when it is a plain number.

        Returns
        -------
        dict
            One entry per input position, keyed by its name (or its index
            for unnamed input), holding the matching documents.
        """
        value, api_unit = normalize_radius(radius, unit)
        return cast(
            dict[str, list[dict[str, Any]]],
            self._request(
                "POST",
                "/queries/cone_search",
                json=_drop_none(
                    {
                        "catalog_name": catalog,
                        "object_coordinates": normalize_coordinates(
                            coordinates
                        ),
                        "radius": value,
                        "unit": api_unit,
                        "filter": filter,
                        "projection": projection,
                        "limit": limit,
                        "skip": skip,
                        "sort": _normalize_sort(sort),
                        "max_time_ms": max_time_ms or None,
                    }
                ),
            ),
        )

    def near_search(
        self,
        catalog: str,
        ra: float,
        dec: float,
        radius_arcsec: float = 1.0,
        *,
        filter: dict[str, Any] | None = None,
        projection: dict[str, Any] | None = None,
        limit: int | None = 1,
        max_time_ms: int | None = DEFAULT_MAX_TIME_MS,
    ) -> list[dict[str, Any]]:
        """Search around one position, nearest match first.

        :meth:`cone_search` answers "everything inside this radius" in no
        particular order. This answers "what is closest", which is what a
        cross-match wants, by running a ``$nearSphere`` query through
        :meth:`find`. Each result carries a ``distance_arcsec`` field.

        Parameters
        ----------
        catalog : str
            Catalog name. It needs a 2dsphere index on
            ``coordinates.radec_geojson``, which the alert, aux and
            cross-match catalogs have.
        ra, dec : float
            Centre of the search, in degrees.
        radius_arcsec : float
            Maximum separation to consider.
        limit : int | None
            How many neighbours to return, closest first. ``None`` returns
            every match inside the radius.
        """
        near = {
            "coordinates.radec_geojson": {
                "$nearSphere": {
                    "$geometry": {
                        "type": "Point",
                        "coordinates": [ra - 180.0, dec],
                    },
                    "$maxDistance": arcsec_to_metres(radius_arcsec),
                }
            }
        }
        if filter:
            near.update(filter)

        matches = self.find(
            catalog,
            near,
            projection=projection,
            limit=limit,
            max_time_ms=max_time_ms,
        )
        return [_with_separation(match, ra, dec) for match in matches]

    def pipeline(
        self,
        catalog: str,
        stages: list[dict[str, Any]],
        *,
        max_time_ms: int | None = DEFAULT_MAX_TIME_MS,
    ) -> list[dict[str, Any]]:
        """Run an aggregation pipeline on a catalog.

        The server passes the stages through to Mongo without an allowlist,
        so a pipeline ending in ``$out`` or ``$merge`` writes to the
        database. This client refuses those two stages; everything else is
        your responsibility.
        """
        for stage in stages:
            for name in ("$out", "$merge"):
                if name in stage:
                    raise QueryError(
                        f"Refusing to run a pipeline containing {name}: it "
                        "writes to the BOOM database."
                    )
        return cast(
            list[dict[str, Any]],
            self._request(
                "POST",
                "/queries/pipeline",
                json=_drop_none(
                    {
                        "catalog_name": catalog,
                        "pipeline": stages,
                        "max_time_ms": max_time_ms or None,
                    }
                ),
            ),
        )

    # ---- catalogs ----

    def catalogs(self, *, details: bool = False) -> list[dict[str, Any]]:
        """List the catalogs the account can read.

        With *details*, each entry also carries Mongo ``$collStats``
        storage statistics, which costs a stats call per catalog.
        """
        return cast(
            list[dict[str, Any]],
            self._request("GET", "/catalogs", params={"get_details": details}),
        )

    def catalog_names(self) -> list[str]:
        """Names only, sorted, of the catalogs the account can read."""
        return [catalog["name"] for catalog in self.catalogs()]

    def catalog_indexes(self, catalog: str) -> list[dict[str, Any]]:
        """List a catalog's indexes.

        Worth checking before a large :meth:`find`: an unindexed filter
        turns into a collection scan.
        """
        return cast(
            list[dict[str, Any]],
            self._request("GET", f"/catalogs/{catalog}/indexes"),
        )

    def catalog_sample(
        self, catalog: str, size: int = 1
    ) -> list[dict[str, Any]]:
        """Return random documents from a catalog, to inspect its schema."""
        if not 1 <= size <= 1000:
            raise ValueError("size must be between 1 and 1000")
        return cast(
            list[dict[str, Any]],
            self._request(
                "GET", f"/catalogs/{catalog}/sample", params={"size": size}
            ),
        )

    # ---- cutouts ----

    def cutouts(
        self,
        survey: Survey,
        *,
        candid: int | None = None,
        object_id: str | None = None,
        which: WhichCutouts | None = None,
        band: str | None = None,
    ) -> dict[str, Any]:
        """Fetch science, template and difference cutouts for an alert.

        Exactly one of *candid* or *object_id* is required. With
        *object_id*, *which* picks the alert ("brightest" by default).

        The base64 payloads are decoded, so ``cutoutScience`` and friends
        come back as raw (gzipped FITS) bytes, ready for
        ``babamul.cutouts.plot_cutouts``.
        """
        if (candid is None) == (object_id is None):
            raise ValueError("Pass exactly one of candid or object_id.")

        payload = cast(
            dict[str, Any],
            self._request(
                "GET",
                f"/surveys/{survey.lower()}/cutouts",
                params=_drop_none(
                    {
                        "candid": candid,
                        "objectId": object_id,
                        "which": which,
                        "band": band,
                    }
                ),
            ),
        )
        for key in ("cutoutScience", "cutoutTemplate", "cutoutDifference"):
            if isinstance(payload.get(key), str):
                payload[key] = base64.b64decode(payload[key])
        return payload

    # ---- misc ----

    def health(self) -> Any:
        """Hit the API root, which answers without authentication."""
        try:
            response = self._client.get("/")
        except httpx.RequestError as error:
            raise APIError(f"Request failed: {error}") from error
        return _unwrap(response, "/")


def arcsec_to_metres(radius_arcsec: float) -> float:
    """Convert an angular radius into the metres ``$nearSphere`` expects."""
    return float(np.deg2rad(radius_arcsec / 3600.0) * EARTH_RADIUS_M)


def _with_separation(
    match: dict[str, Any], ra: float, dec: float
) -> dict[str, Any]:
    """Annotate a match with its angular separation from the search centre."""
    coordinates = (match.get("coordinates") or {}).get("radec_geojson") or {}
    pair = coordinates.get("coordinates")
    if not pair or len(pair) != 2:
        return match

    from astropy.coordinates import SkyCoord

    centre = SkyCoord(ra, dec, unit="deg")
    other = SkyCoord(pair[0] + 180.0, pair[1], unit="deg")
    return {
        **match,
        "distance_arcsec": float(centre.separation(other).arcsec),
    }


def _normalize_sort(
    sort: dict[str, Any] | list[tuple[str, int]] | None,
) -> dict[str, Any] | None:
    if sort is None or isinstance(sort, dict):
        return sort
    return dict(sort)


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return response.text
    if isinstance(payload, dict):
        return str(payload.get("message", response.text))
    return response.text


def _unwrap(response: httpx.Response, endpoint: str) -> Any:
    """Turn an API response into its payload, or raise."""
    if response.status_code == 401:
        raise APIAuthenticationError(
            "Authentication failed. Check your credentials.", status_code=401
        )
    if response.status_code == 403:
        raise APIAuthenticationError(
            f"Forbidden: {_error_message(response)}", status_code=403
        )
    if response.status_code == 404:
        raise APINotFoundError(
            f"{_error_message(response)} (the catalog may exist but be "
            "outside your access)",
            status_code=404,
        )
    if response.status_code == 400:
        raise QueryError(
            f"The API rejected the query: {_error_message(response)}",
            status_code=400,
        )
    if response.status_code >= 400:
        raise APIError(
            f"API error ({response.status_code}): {_error_message(response)}",
            status_code=response.status_code,
        )

    try:
        payload = response.json()
    except Exception as error:
        raise APIError(
            f"{endpoint} returned a non-JSON body: {response.text[:200]}"
        ) from error

    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


__all__ = [
    "BoomClient",
    "ConfigurationError",
    "DEFAULT_MAX_TIME_MS",
    "DEFAULT_PAGE_SIZE",
    "EARTH_RADIUS_M",
    "Survey",
    "WhichCutouts",
    "arcsec_to_metres",
    "get_user_agent",
    "normalize_coordinates",
    "normalize_radius",
    "normalize_unit",
]
