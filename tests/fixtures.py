"""Catalog documents shaped like the real ones, for the offline tests."""

from __future__ import annotations

from typing import Any


def geojson(ra: float, dec: float) -> dict[str, Any]:
    """Coordinates the way BOOM stores them, longitude shifted by 180."""
    return {
        "radec_geojson": {"type": "Point", "coordinates": [ra - 180.0, dec]}
    }


def ztf_point(jd: float, **overrides: Any) -> dict[str, Any]:
    point = {
        "jd": jd,
        "band": "g",
        "ra": 12.0,
        "dec": -30.0,
        "magpsf": 18.5,
        "sigmapsf": 0.1,
        "diffmaglim": 20.4,
        "isdiffpos": "t",
        "psfFlux": 120.0,
        "psfFluxErr": 12.0,
        "snr": 10.0,
        "drb": 0.98,
        "nbad": 0,
    }
    point.update(overrides)
    return point


def lsst_point(jd: float, **overrides: Any) -> dict[str, Any]:
    point = {
        "jd": jd,
        "band": "i",
        "ra": 12.0,
        "dec": -30.0,
        "magpsf": 19.0,
        "sigmapsf": 0.12,
        "diffmaglim": 23.5,
        "isdiffpos": True,
        "psfFlux": 90.0,
        "psfFluxErr": 9.0,
        "snr": 8.0,
        "reliability": 0.9,
        "isDipole": False,
        "isNegative": False,
        "centroid_flag": False,
        "psfFlux_flag": False,
        "pixelFlags": False,
        "glint_trail": False,
    }
    point.update(overrides)
    return point


ZTF_ID = "ZTF18abcdefg"
LSST_ID = "42"

ZTF_ALERT = {
    "_id": 2695378462115015007,
    "objectId": ZTF_ID,
    "candidate": {
        "jd": 2460000.5,
        "ra": 12.0,
        "dec": -30.0,
        "band": "g",
        "magpsf": 18.2,
        "sigmapsf": 0.08,
        "distpsnr1": 1.4,
    },
    "coordinates": geojson(12.0, -30.0),
}

ZTF_AUX = {
    "_id": ZTF_ID,
    "prv_candidates": [
        ztf_point(2459998.5),
        ztf_point(2459999.5, magpsf=18.4),
        ztf_point(2460000.5, magpsf=18.2, snr=25.0),
    ],
    "prv_nondetections": [{"jd": 2459997.5, "band": "r", "diffmaglim": 20.6}],
    "fp_hists": [
        ztf_point(2460000.5, snr=30.0),  # same epoch as an alert
        ztf_point(2460001.5, magpsf=18.9, snr=6.0),
    ],
    "cross_matches": {"": [{"ignored": True}], "NED": [{"z": 0.03}]},
    "aliases": {"LSST": [LSST_ID], "DECAM": []},
    "coordinates": geojson(12.0, -30.0),
}

LSST_ALERT = {
    "_id": 900001,
    "objectId": LSST_ID,
    "candidate": {
        "jd": 2460000.2,
        "ra": 12.0,
        "dec": -30.0,
        "band": "i",
        "magpsf": 19.0,
        "sigmapsf": 0.12,
    },
    "coordinates": geojson(12.0, -30.0),
}

LSST_AUX = {
    "_id": LSST_ID,
    "prv_candidates": [lsst_point(2459999.0), lsst_point(2460000.2)],
    "fp_hists": [lsst_point(2460002.0, magpsf=19.4)],
    "cross_matches": {},
    "aliases": {"ZTF": [ZTF_ID], "DECAM": []},
    "coordinates": geojson(12.0, -30.0),
}

HOST_MATCHES = {
    "NED": [
        {
            "_id": "NGC1234",
            "z": 0.031,
            "z_unc": 0.001,
            "z_tech": "SPEC",
            "coordinates": geojson(12.0005, -30.0005),
        }
    ],
    "DESI_DR1": [],
    "LSPSC": [],
    "PS1_DR2": [],
}

CATALOGS = {
    "ZTF_alerts": [ZTF_ALERT],
    "ZTF_alerts_aux": [ZTF_AUX],
    "LSST_alerts": [LSST_ALERT],
    "LSST_alerts_aux": [LSST_AUX],
    **HOST_MATCHES,
}
