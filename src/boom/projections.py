"""Minimal field sets for the alert and aux catalogs.

Aux documents carry the whole photometric history, which for an old ZTF
object runs to megabytes. Passing one of these as a projection keeps the
transfer to the fields the photometry and source builders actually read.
"""

from __future__ import annotations

from typing import Any

from .client import Survey

ALERT_FIELDS = (
    "objectId",
    "candidate.jd",
    "candidate.ra",
    "candidate.dec",
    "candidate.band",
    "candidate.magpsf",
    "candidate.sigmapsf",
    "candidate.distpsnr1",
    "coordinates",
)

_COMMON_POINT_FIELDS = (
    "jd",
    "band",
    "ra",
    "dec",
    "magpsf",
    "sigmapsf",
    "diffmaglim",
    "isdiffpos",
    "psfFlux",
    "psfFluxErr",
)

_ZTF_POINT_FIELDS = (
    *_COMMON_POINT_FIELDS,
    "snr",
    "snr_psf",
    "drb",
    "nbad",
)

_LSST_POINT_FIELDS = (
    *_COMMON_POINT_FIELDS,
    "snr",
    "snr_psf",
    "reliability",
    "isDipole",
    "isNegative",
    "centroid_flag",
    "psfFlux_flag",
    "pixelFlags",
    "glint_trail",
)

_HISTORY_FIELDS: dict[str, tuple[str, ...]] = {
    "ZTF": ("prv_candidates", "prv_nondetections", "fp_hists"),
    "LSST": ("prv_candidates", "fp_hists"),
}

_POINT_FIELDS: dict[str, tuple[str, ...]] = {
    "ZTF": _ZTF_POINT_FIELDS,
    "LSST": _LSST_POINT_FIELDS,
}


def alert_projection() -> dict[str, Any]:
    """Fields to pull from ``{SURVEY}_alerts``."""
    return dict.fromkeys(ALERT_FIELDS, 1)


def aux_projection(survey: Survey) -> dict[str, Any]:
    """Fields to pull from ``{SURVEY}_alerts_aux``."""
    key = survey.upper()
    projection: dict[str, Any] = {
        f"{history}.{field}": 1
        for history in _HISTORY_FIELDS[key]
        for field in _POINT_FIELDS[key]
    }
    projection["aliases"] = 1
    projection["cross_matches"] = 1
    projection["coordinates"] = 1
    return projection


__all__ = ["ALERT_FIELDS", "alert_projection", "aux_projection"]
