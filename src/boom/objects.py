"""Build objects and sources out of the BOOM catalogs.

An object's data is split in two: ``{SURVEY}_alerts`` holds one document
per alert, ``{SURVEY}_alerts_aux`` holds the whole photometric history and
the cross-matches. Two levels are offered on top of that join:

- :func:`get_object` returns the merged raw document, shaped the way the
  ``babamul`` tools read an alert, so plotting works on it directly.
- :func:`get_source` returns a typed :class:`~boom.models.Source` with
  cleaned photometry, a host guess and summary statistics.
"""

from __future__ import annotations

from typing import Any, cast

import pandas as pd
from babamul.cutouts import plot_cutouts as _plot_cutouts
from babamul.lightcurves import plot_lightcurve as _plot_lightcurve

from .cache import Cache
from .client import BoomClient, Survey
from .exceptions import APINotFoundError
from .models import (
    HOST_CATALOGS,
    Observation,
    Source,
    apply_photometry_dtypes,
    empty_photometry,
    observations_from_frame,
)
from .photometry import build_photometry
from .projections import alert_projection, aux_projection

#: Radius used to pair an object with its counterpart in another survey,
#: mirroring ZTF_LSST_XMATCH_RADIUS on the server.
XMATCH_RADIUS_ARCSEC = 2.0

#: Radius used to look for a host galaxy in the cross-match catalogs.
HOST_RADIUS_ARCSEC = 10.0

SURVEYS: tuple[Survey, ...] = ("ZTF", "LSST")

_AUX_FIELDS = (
    "prv_candidates",
    "prv_nondetections",
    "fp_hists",
    "cross_matches",
    "aliases",
    "coordinates",
)


def alert_collection(survey: Survey) -> str:
    """Name of the per-alert catalog for a survey."""
    return f"{survey.upper()}_alerts"


def aux_collection(survey: Survey) -> str:
    """Name of the per-object catalog for a survey."""
    return f"{survey.upper()}_alerts_aux"


def get_radec(document: dict[str, Any]) -> tuple[float, float] | None:
    """Read ``(ra, dec)`` in degrees out of a document's ``coordinates``.

    BOOM stores positions as GeoJSON, whose longitude runs over
    [-180, 180), so the right ascension is the stored value plus 180.
    """
    coordinates = document.get("coordinates") or {}
    geojson = coordinates.get("radec_geojson") or {}
    pair = geojson.get("coordinates")
    if not pair or len(pair) != 2:
        return None
    return float(pair[0]) + 180.0, float(pair[1])


def get_alerts(
    client: BoomClient,
    survey: Survey,
    object_id: str,
    *,
    projection: dict[str, Any] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Every alert emitted for an object, newest first."""
    return client.find(
        alert_collection(survey),
        {"objectId": object_id},
        projection=projection,
        sort={"candidate.jd": -1},
        limit=limit,
    )


def get_object(
    client: BoomClient,
    survey: Survey,
    object_id: str,
    *,
    trim: bool = True,
    include_survey_matches: bool = True,
    cache: Cache | None = None,
) -> dict[str, Any]:
    """Join an object's latest alert with its full history.

    Parameters
    ----------
    client : BoomClient
        An authenticated client.
    survey : str
        ``"ZTF"`` or ``"LSST"``.
    object_id : str
        The object identifier, as stored in ``objectId``.
    trim : bool
        Request only the fields this package reads. An untrimmed aux
        document for an old object runs to megabytes, so leave this on
        unless you need a field the projections omit.
    include_survey_matches : bool
        Also fetch the same source in the other survey and attach it under
        ``survey_matches``, keyed by lowercase survey name, the way
        ``babamul`` reads it. Uses the recorded alias when there is one,
        and falls back to the nearest object within
        :data:`XMATCH_RADIUS_ARCSEC`.
    cache : Cache | None
        Where to read and store the result. Defaults to
        :meth:`Cache.from_env`, disabled unless ``BOOM_CACHE_DIR`` is set.

    Returns
    -------
    dict
        A document carrying ``objectId``, ``candidate``,
        ``prv_candidates``, ``prv_nondetections``, ``fp_hists``,
        ``cross_matches``, ``aliases`` and optionally ``survey_matches``.
        It can be passed straight to :func:`plot_lightcurve`.

    Raises
    ------
    APINotFoundError
        If the object has no alert in that survey.
    """
    survey = cast(Survey, survey.upper())
    cache = cache if cache is not None else Cache.from_env()
    key = f"{survey}-{object_id}"
    if not include_survey_matches:
        key += "-nomatch"
    if not trim:
        key += "-full"

    cached = cache.get_json("objects", key)
    if cached is not None:
        return cast(dict[str, Any], cached)

    obj = _fetch_object(client, survey, object_id, trim=trim)
    if include_survey_matches:
        obj["survey_matches"] = _survey_matches(client, survey, obj, trim=trim)

    cache.set_json("objects", key, obj)
    return obj


def _fetch_object(
    client: BoomClient, survey: Survey, object_id: str, *, trim: bool
) -> dict[str, Any]:
    """Fetch and merge the alert and aux documents for one object."""
    alert = client.find_one(
        alert_collection(survey),
        {"objectId": object_id},
        projection=alert_projection() if trim else None,
        sort={"candidate.jd": -1},
    )
    if alert is None:
        raise APINotFoundError(
            f"No {survey} alert found for object {object_id}.",
            status_code=404,
        )

    aux = (
        client.find_one(
            aux_collection(survey),
            {"_id": object_id},
            projection=aux_projection(survey) if trim else None,
        )
        or {}
    )

    obj = dict(alert)
    for field in _AUX_FIELDS:
        if field in aux:
            obj[field] = aux[field]
    obj.setdefault("prv_candidates", [])
    obj.setdefault("prv_nondetections", [])
    obj.setdefault("fp_hists", [])
    obj.setdefault("cross_matches", {})
    return obj


def _survey_matches(
    client: BoomClient, survey: Survey, obj: dict[str, Any], *, trim: bool
) -> dict[str, Any]:
    """Find this object in the other surveys, by alias then by position."""
    matches: dict[str, Any] = {}
    aliases = obj.get("aliases") or {}
    radec = get_radec(obj)

    for other in SURVEYS:
        if other == survey:
            continue

        match_id = next(iter(aliases.get(other, [])), None)
        if match_id is None and radec is not None:
            nearest = client.near_search(
                aux_collection(other),
                *radec,
                XMATCH_RADIUS_ARCSEC,
                projection={"_id": 1},
            )
            match_id = nearest[0]["_id"] if nearest else None
        if match_id is None:
            continue

        try:
            match = _fetch_object(client, other, str(match_id), trim=trim)
        except APINotFoundError:
            match = (
                client.find_one(
                    aux_collection(other),
                    {"_id": match_id},
                    projection=aux_projection(other) if trim else None,
                )
                or {}
            )
            if not match:
                continue
            match["objectId"] = match.pop("_id", match_id)
        matches[other.lower()] = match

    return matches


# ---- sources ----


def get_source(
    client: BoomClient,
    survey: Survey,
    object_id: str,
    *,
    quality_cuts: bool = True,
    enrich_crossmatches: bool = True,
    cache: Cache | None = None,
) -> Source:
    """Build a typed :class:`~boom.models.Source` for an object.

    On top of :func:`get_object` this cleans the photometry, merges in the
    other survey's measurements, looks for a host and computes the summary
    statistics.

    Parameters
    ----------
    quality_cuts : bool
        Drop the measurements the surveys flag as unusable.
    enrich_crossmatches : bool
        Search the host catalogs for a counterpart and record its angular
        separation. Costs one query per catalog in
        :data:`~boom.models.HOST_CATALOGS`.
    cache : Cache | None
        Reads and writes the finished source, not just the raw documents.
    """
    survey = cast(Survey, survey.upper())
    cache = cache if cache is not None else Cache.from_env()

    stored = Source.load(cache, object_id)
    if stored is not None:
        return stored

    obj = get_object(client, survey, object_id, cache=cache)
    table = photometry(obj, survey, quality_cuts=quality_cuts)

    crossmatches = dict(obj.get("cross_matches") or {})
    crossmatches.pop("", None)
    radec = _position(obj)
    if enrich_crossmatches and radec is not None:
        crossmatches.update(_host_matches(client, *radec))

    source = _build_source(obj, survey, table, crossmatches, radec)
    source.save(cache)
    return source


def _build_source(
    obj: dict[str, Any],
    survey: Survey,
    table: pd.DataFrame,
    crossmatches: dict[str, list[dict[str, Any]]],
    radec: tuple[float, float] | None,
) -> Source:
    candidate = obj.get("candidate") or {}
    aliases = obj.get("aliases") or {}
    object_id = str(obj.get("objectId", ""))
    ra, dec = radec if radec is not None else (float("nan"), float("nan"))

    host = _pick_host(crossmatches)
    offset, host_origin = host.get("offset"), host.get("origin")
    if offset is None and candidate.get("distpsnr1") is not None:
        offset, host_origin = candidate["distpsnr1"], "PS1"

    identifiers: dict[str, str | None] = {survey.lower() + "id": object_id}
    for other in SURVEYS:
        if other != survey:
            identifiers[other.lower() + "id"] = next(
                iter(aliases.get(other, [])), None
            )

    return Source(
        objectid=object_id,
        survey=survey,
        jd=float(candidate.get("jd") or float("nan")),
        ra=ra,
        dec=dec,
        photometry=observations_from_frame(table),
        crossmatches=crossmatches,
        offset=offset,
        host_origin=host_origin,
        redshift=host.get("redshift"),
        redshift_error=host.get("redshift_error"),
        redshift_origin=host.get("redshift_origin"),
        **identifiers,
    )


def _pick_host(
    crossmatches: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Take the host from the first catalog that has one.

    :data:`~boom.models.HOST_CATALOGS` is ordered by how much the redshift
    is trusted, so the first hit wins rather than the nearest one. Keys
    the match does not carry are left out, so the caller can fall back.
    """
    for catalog in HOST_CATALOGS:
        matches = crossmatches.get(catalog) or []
        if not matches:
            continue
        match = matches[0]
        host: dict[str, Any] = {}
        if match.get("distance_arcsec") is not None:
            host["offset"] = match["distance_arcsec"]
            host["origin"] = catalog
        if match.get("z") is not None:
            technique = match.get("z_tech")
            host["redshift"] = match["z"]
            host["redshift_error"] = match.get("z_unc")
            host["redshift_origin"] = (
                f"{catalog}_{technique}" if technique else catalog
            )
        if host:
            return host
    return {}


def _host_matches(
    client: BoomClient, ra: float, dec: float
) -> dict[str, list[dict[str, Any]]]:
    """Nearest entry in each host catalog, with its separation."""
    matches = {}
    for catalog in HOST_CATALOGS:
        try:
            matches[catalog] = client.near_search(
                catalog, ra, dec, HOST_RADIUS_ARCSEC
            )
        except APINotFoundError:
            continue
    return matches


def _position(obj: dict[str, Any]) -> tuple[float, float] | None:
    candidate = obj.get("candidate") or {}
    if candidate.get("ra") is not None and candidate.get("dec") is not None:
        return float(candidate["ra"]), float(candidate["dec"])
    return get_radec(obj)


# ---- photometry ----


def photometry(
    obj: dict[str, Any],
    survey: Survey | None = None,
    *,
    quality_cuts: bool = True,
) -> pd.DataFrame:
    """Flatten an object's photometry, and its matches', into one table.

    Parameters
    ----------
    obj : dict
        An object as returned by :func:`get_object`.
    survey : str | None
        Which survey's column conventions to apply. Inferred from the
        object id when omitted.
    quality_cuts : bool
        Drop the measurements the surveys flag as unusable.

    Returns
    -------
    pandas.DataFrame
        One row per measurement, sorted by epoch, with the columns in
        :data:`boom.models.COLUMNS`.
    """
    survey = cast(Survey, (survey or _infer_survey(obj)).upper())
    frames = [build_photometry(obj, survey, quality_cuts=quality_cuts)]

    for name, match in (obj.get("survey_matches") or {}).items():
        frames.append(
            build_photometry(
                match, cast(Survey, name.upper()), quality_cuts=quality_cuts
            )
        )

    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return empty_photometry()

    merged = pd.concat(frames, ignore_index=True).sort_values(
        "jd", ignore_index=True
    )
    return apply_photometry_dtypes(merged)


def _infer_survey(obj: dict[str, Any]) -> str:
    return "ZTF" if str(obj.get("objectId", "")).startswith("ZTF") else "LSST"


# ---- plotting ----


def plot_lightcurve(obj: dict[str, Any] | Source, **kwargs: Any) -> None:
    """Plot a light curve, via babamul's plotting code.

    Takes either the dict from :func:`get_object` or a
    :class:`~boom.models.Source`.
    """
    document = obj.to_alert_style() if isinstance(obj, Source) else obj
    _plot_lightcurve(document, **kwargs)


def plot_cutouts(
    client: BoomClient,
    survey: Survey,
    *,
    object_id: str | None = None,
    candid: int | None = None,
    which: str | None = None,
    band: str | None = None,
    **kwargs: Any,
) -> Any:
    """Fetch an alert's cutouts and plot them, via babamul's plotting code."""
    payload = client.cutouts(
        survey,
        object_id=object_id,
        candid=candid,
        which=cast(Any, which),
        band=band,
    )
    if object_id is not None:
        payload.setdefault("objectId", object_id)
    return _plot_cutouts(payload, survey.lower(), **kwargs)


__all__ = [
    "HOST_RADIUS_ARCSEC",
    "SURVEYS",
    "XMATCH_RADIUS_ARCSEC",
    "Observation",
    "Source",
    "alert_collection",
    "aux_collection",
    "get_alerts",
    "get_object",
    "get_radec",
    "get_source",
    "photometry",
    "plot_cutouts",
    "plot_lightcurve",
]
