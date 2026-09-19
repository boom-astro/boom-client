"""Assemble BOOM catalog documents into objects the babamul tools accept.

An object's data lives in two collections: ``{SURVEY}_alerts`` holds one
document per alert, ``{SURVEY}_alerts_aux`` holds the object's full
photometric history and cross-matches. This module joins them into the
shape ``babamul.lightcurves`` and ``babamul.cutouts`` already know how to
read, so the plotting and photometry code is not written twice.
"""

from __future__ import annotations

from typing import Any, cast

import pandas as pd
from babamul.cutouts import plot_cutouts as _plot_cutouts
from babamul.lightcurves import (
    get_fp_hists,
    get_prv_candidates,
    get_prv_nondetections,
)
from babamul.lightcurves import plot_lightcurve as _plot_lightcurve

from .cache import Cache
from .client import BoomClient, Survey
from .exceptions import APINotFoundError

#: Positional uncertainty used to pair a ZTF object with an LSST one,
#: mirroring ZTF_LSST_XMATCH_RADIUS on the server.
XMATCH_RADIUS_ARCSEC = 2.0

#: Aux collections carry no prv_nondetections outside ZTF.
_NONDETECTION_SURVEYS = frozenset({"ZTF"})

_OTHER_SURVEY = {"ZTF": "LSST", "LSST": "ZTF"}

_AUX_PHOTOMETRY_FIELDS = (
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
    include_survey_matches: bool = True,
    cache: Cache | None = None,
) -> dict[str, Any]:
    """Build a full object: latest alert, history, cross-matches.

    Parameters
    ----------
    client : BoomClient
        An authenticated client.
    survey : str
        ``"ZTF"`` or ``"LSST"``.
    object_id : str
        The object identifier, as stored in ``objectId``.
    include_survey_matches : bool
        Also look for the same source in the other survey, within
        :data:`XMATCH_RADIUS_ARCSEC`, and attach it under
        ``survey_matches``. This costs one extra cone search.
    cache : Cache | None
        Where to read and store the result. Defaults to
        :meth:`Cache.from_env`, which is disabled unless
        ``BOOM_CACHE_DIR`` is set.

    Returns
    -------
    dict
        A document carrying ``objectId``, ``candidate``,
        ``prv_candidates``, ``prv_nondetections``, ``fp_hists``,
        ``cross_matches`` and, optionally, ``survey_matches``. It can be
        passed straight to :func:`plot_lightcurve`.

    Raises
    ------
    APINotFoundError
        If the object has no alert in that survey.
    """
    survey = cast(Survey, survey.upper())
    cache = cache if cache is not None else Cache.from_env()
    suffix = "" if include_survey_matches else "-nomatch"
    cache_key = f"{survey}-{object_id}{suffix}"

    cached = cache.get_json("objects", cache_key)
    if cached is not None:
        return cast(dict[str, Any], cached)

    alert = client.find_one(
        alert_collection(survey),
        {"objectId": object_id},
        sort={"candidate.jd": -1},
    )
    if alert is None:
        raise APINotFoundError(
            f"No {survey} alert found for object {object_id}.",
            status_code=404,
        )

    aux = client.find_one(aux_collection(survey), {"_id": object_id}) or {}

    obj = dict(alert)
    for field in _AUX_PHOTOMETRY_FIELDS:
        if field in aux:
            obj[field] = aux[field]
    obj.setdefault("prv_candidates", [])
    obj.setdefault("prv_nondetections", [])
    obj.setdefault("fp_hists", [])

    if include_survey_matches:
        obj["survey_matches"] = _find_survey_matches(client, survey, obj)

    cache.set_json("objects", cache_key, obj)
    return obj


def _find_survey_matches(
    client: BoomClient, survey: Survey, obj: dict[str, Any]
) -> dict[str, Any]:
    """Look for the same source in the other survey's aux collection."""
    other = _OTHER_SURVEY.get(survey)
    radec = get_radec(obj)
    if other is None or radec is None:
        return {}

    results = client.cone_search(
        aux_collection(cast(Survey, other)),
        {"target": radec},
        XMATCH_RADIUS_ARCSEC,
        "arcsec",
        limit=1,
    )
    matches = results.get("target") or []
    if not matches:
        return {}

    match = dict(matches[0])
    match["objectId"] = match.pop("_id", None)
    return {other.lower(): match}


def photometry(obj: dict[str, Any]) -> pd.DataFrame:
    """Flatten an object's photometry into a single table.

    Merges the alert history, the forced photometry and, where a survey
    provides them, the non-detection upper limits, for the object's own
    survey and for any survey matched by :func:`get_object`.

    Returns
    -------
    pandas.DataFrame
        Columns ``mjd``, ``mag``, ``magerr``, ``band``, ``lim``,
        ``survey`` and ``origin``, sorted by ``mjd``. ``lim`` marks a row
        as an upper limit rather than a detection.
    """
    survey = _survey_of(obj)
    rows = _photometry_rows(obj, survey)

    for name, match in (obj.get("survey_matches") or {}).items():
        rows.extend(_photometry_rows(match, name.upper()))

    frame = pd.DataFrame(
        rows,
        columns=[
            "mjd",
            "mag",
            "magerr",
            "band",
            "lim",
            "survey",
            "origin",
        ],
    )
    if frame.empty:
        return frame
    return frame.sort_values("mjd", ignore_index=True)


def _photometry_rows(
    document: dict[str, Any], survey: str
) -> list[dict[str, Any]]:
    sources = [
        ("prv_candidates", get_prv_candidates(document)),
        ("fp_hists", get_fp_hists(document)),
    ]
    if survey in _NONDETECTION_SURVEYS:
        sources.append(("prv_nondetections", get_prv_nondetections(document)))
    return [
        {**row, "survey": survey, "origin": origin}
        for origin, rows in sources
        for row in rows
    ]


def _survey_of(obj: dict[str, Any]) -> str:
    object_id = str(obj.get("objectId", ""))
    return "ZTF" if object_id.startswith("ZTF") else "LSST"


def plot_lightcurve(obj: dict[str, Any], **kwargs: Any) -> None:
    """Plot an object's light curve, via babamul's plotting code.

    Takes the dict returned by :func:`get_object`. Keyword arguments go
    through to ``babamul.lightcurves.plot_lightcurve``.
    """
    _plot_lightcurve(obj, **kwargs)


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
    """Fetch an alert's cutouts and plot them, via babamul's plotting code.

    Keyword arguments go through to ``babamul.cutouts.plot_cutouts``.
    """
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
    "XMATCH_RADIUS_ARCSEC",
    "alert_collection",
    "aux_collection",
    "get_alerts",
    "get_object",
    "get_radec",
    "photometry",
    "plot_cutouts",
    "plot_lightcurve",
]
