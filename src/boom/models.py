"""Typed models for a source and its individual measurements."""

from __future__ import annotations

import json
from typing import Any, Literal, cast

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, computed_field

from .cache import Cache

#: Where a measurement came from: an alert packet, forced photometry, or
#: an upper limit on a visit with no detection.
ObsClass = Literal["alert", "fp", "ul"]

#: Default signal-to-noise floor for calling a measurement a detection.
DEFAULT_MIN_SNR = 3.0

#: A measurement fainter than the visit's limiting magnitude by more than
#: this is inconsistent with itself, so it is not trusted as a detection.
MAG_LIMIT_SLACK = 1.0

#: Canonical photometry schema. Every table this package produces has
#: exactly these columns, in this order, with these dtypes, so a Parquet
#: round-trip and a concat across surveys both stay lossless.
PHOTOMETRY_DTYPES: dict[str, str] = {
    "jd": "float64",
    "mjd": "float64",
    "band": "object",
    "magpsf": "float64",
    "sigmapsf": "float64",
    "diffmaglim": "float64",
    "snr": "float64",
    "ra": "float64",
    "dec": "float64",
    "psf_flux": "float64",
    "psf_flux_err": "float64",
    "isdiffpos": "boolean",
    "det_type": "object",
    "survey": "object",
}

COLUMNS = list(PHOTOMETRY_DTYPES)

#: Cross-match catalogs searched for a host, in the order they are trusted.
HOST_CATALOGS = ("NED", "DESI_DR1", "LSPSC", "PS1_DR2")


class Observation(BaseModel):
    """One photometric measurement of a source."""

    model_config = ConfigDict(extra="ignore")

    jd: float
    band: str
    survey: str
    det_type: ObsClass = "alert"
    magpsf: float | None = None
    sigmapsf: float | None = None
    diffmaglim: float | None = None
    snr: float | None = None
    ra: float | None = None
    dec: float | None = None
    psf_flux: float | None = None
    psf_flux_err: float | None = None
    isdiffpos: bool | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mjd(self) -> float:
        """Modified Julian Date of the measurement."""
        return self.jd - 2400000.5

    @computed_field  # type: ignore[prop-decorator]
    @property
    def estdiffmaglim(self) -> float | None:
        """Limiting magnitude implied by this measurement's own noise.

        Useful where the survey reports no ``diffmaglim``: a detection at
        signal-to-noise *s* implies a 5-sigma limit *2.5 log10(5/s)*
        magnitudes fainter.
        """
        if self.magpsf is None or not self.snr or self.snr <= 0:
            return None
        return float(self.magpsf - 2.5 * np.log10(5.0 / self.snr))


class Source(BaseModel):
    """A source, its photometry and its cross-matches.

    Built by :func:`boom.objects.get_source`. Holds the object's identity
    across surveys, a host guess taken from the cross-matches, and one
    :class:`Observation` per measurement.
    """

    model_config = ConfigDict(extra="ignore")

    objectid: str
    survey: str
    jd: float
    ra: float
    dec: float
    ztfid: str | None = None
    lsstid: str | None = None
    tns_name: str | None = None
    offset: float | None = None
    host_origin: str | None = None
    redshift: float | None = None
    redshift_error: float | None = None
    redshift_origin: str | None = None
    photometry: list[Observation] = []
    crossmatches: dict[str, list[dict[str, Any]]] = {}

    # ---- derived summaries ----

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ndethist(self) -> int:
        """Number of detections passing the default cuts."""
        return len(self.get_detections())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def filters(self) -> list[str]:
        """Bands the source was detected in."""
        detections = self.get_detections()
        if detections.empty:
            return []
        return sorted(set(detections["band"].dropna()))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ndetfilters(self) -> int:
        """How many distinct bands the source was detected in."""
        return len(self.filters)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def jdstarthist(self) -> float:
        """Epoch of the first detection."""
        return self._detection_stat("jd", "min")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def jdendhist(self) -> float:
        """Epoch of the last detection."""
        return self._detection_stat("jd", "max")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def age(self) -> float:
        """Days between the first and the last detection."""
        return self.jdendhist - self.jdstarthist

    @computed_field  # type: ignore[prop-decorator]
    @property
    def peak_mag(self) -> float:
        """Brightest detected magnitude, across all bands."""
        return self._detection_stat("magpsf", "min")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def median_ra(self) -> float:
        """Median right ascension of the detections, in degrees."""
        return self._detection_stat("ra", "median")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def median_dec(self) -> float:
        """Median declination of the detections, in degrees."""
        return self._detection_stat("dec", "median")

    def _detection_stat(self, column: str, how: str) -> float:
        detections = self.get_detections()
        if detections.empty or column not in detections:
            return float("nan")
        values = pd.to_numeric(detections[column], errors="coerce").dropna()
        return float(getattr(values, how)()) if len(values) else float("nan")

    # ---- photometry ----

    def get_photometry(self) -> pd.DataFrame:
        """Every measurement, including forced photometry and limits."""
        if not self.photometry:
            return empty_photometry()
        frame = pd.DataFrame(
            [obs.model_dump() for obs in self.photometry]
        ).sort_values("jd", ignore_index=True)
        return apply_photometry_dtypes(frame)

    def get_detections(
        self,
        min_snr: float = DEFAULT_MIN_SNR,
        *,
        include_lsst_fp: bool = False,
    ) -> pd.DataFrame:
        """Measurements that are real, positive detections.

        Parameters
        ----------
        min_snr : float
            Signal-to-noise floor.
        include_lsst_fp : bool
            Keep LSST forced photometry. It is excluded by default: it is
            reported on every visit, so it swamps the alert history.
        """
        frame = self.get_photometry()
        if frame.empty:
            return frame

        snr = pd.to_numeric(frame["snr"], errors="coerce")
        magpsf = pd.to_numeric(frame["magpsf"], errors="coerce")
        diffmaglim = pd.to_numeric(frame["diffmaglim"], errors="coerce")

        keep = (
            frame["isdiffpos"].fillna(False).astype(bool)
            & magpsf.notna()
            & (snr > min_snr)
            & ~(magpsf > diffmaglim + MAG_LIMIT_SLACK)
        )
        if not include_lsst_fp:
            keep &= ~(
                (frame["det_type"] == "fp") & (frame["survey"] == "LSST")
            )
        return frame[keep].reset_index(drop=True)

    def plot_lightcurve(self, **kwargs: Any) -> None:
        """Plot the light curve, via babamul's plotting code."""
        from babamul.lightcurves import plot_lightcurve

        plot_lightcurve(self.to_alert_style(), **kwargs)

    # ---- interchange ----

    def to_alert_style(self) -> dict[str, Any]:
        """Render as an alert-shaped dict, the way babamul reads one.

        Lets a :class:`Source` reach any tool that expects a raw alert,
        including ``babamul.lightcurves.plot_lightcurve``.
        """
        detections = self.get_detections()
        history = [
            {
                "jd": row["jd"],
                "magpsf": row["magpsf"],
                "sigmapsf": row["sigmapsf"],
                "diffmaglim": row["diffmaglim"],
                "snr": row["snr"],
                "band": row["band"],
            }
            for row in detections.to_dict(orient="records")
        ]
        candidate = history[-1] if history else {"jd": self.jd}
        return {
            "objectId": self.objectid,
            "candidate": {**candidate, "ra": self.ra, "dec": self.dec},
            "prv_candidates": history[:-1],
            "prv_nondetections": [],
            "fp_hists": [],
            "cross_matches": self.crossmatches,
        }

    def to_json(self, *, detections_only: bool = True) -> str:
        """Serialize to JSON, photometry included as columns."""
        data = self.model_dump(mode="json", exclude={"photometry"})
        frame = (
            self.get_detections() if detections_only else self.get_photometry()
        )
        data["photometry"] = frame.replace({np.nan: None}).to_dict(
            orient="list"
        )
        return json.dumps(data)

    @classmethod
    def from_json(cls, data: str) -> Source:
        """Rebuild a source written by :meth:`to_json`."""
        payload: dict[str, Any] = json.loads(data)
        frame = pd.DataFrame(payload.pop("photometry", {}))
        return cls(
            **payload,
            photometry=observations_from_frame(apply_photometry_dtypes(frame)),
        )

    # ---- persistence ----

    def save(self, cache: Cache) -> None:
        """Write to a cache: metadata and photometry as Parquet, matches as JSON."""
        meta = self.model_dump(exclude={"photometry", "crossmatches"})
        cache.set_frame("sources", self.objectid, pd.DataFrame([meta]))
        cache.set_frame("photometry", self.objectid, self.get_photometry())
        cache.set_json("crossmatches", self.objectid, self.crossmatches)

    @classmethod
    def load(cls, cache: Cache, object_id: str) -> Source | None:
        """Read back a source written by :meth:`save`, or ``None`` if absent."""
        meta = cache.get_frame("sources", object_id)
        if meta is None or meta.empty:
            return None
        frame = cache.get_frame("photometry", object_id)
        fields = cast(
            "dict[str, Any]", meta.replace({np.nan: None}).iloc[0].to_dict()
        )
        return cls(
            **fields,
            photometry=(
                observations_from_frame(frame) if frame is not None else []
            ),
            crossmatches=cache.get_json("crossmatches", object_id) or {},
        )


def empty_photometry() -> pd.DataFrame:
    """An empty photometry table carrying the right columns and dtypes."""
    return pd.DataFrame(
        {
            column: pd.Series(dtype=dtype)
            for column, dtype in PHOTOMETRY_DTYPES.items()
        }
    )


def observations_from_frame(frame: pd.DataFrame) -> list[Observation]:
    """Build :class:`Observation` objects from a photometry table."""
    if frame.empty:
        return []
    rows = cast(
        "list[dict[str, Any]]",
        frame.replace({np.nan: None}).to_dict(orient="records"),
    )
    return [Observation(**row) for row in rows]


def apply_photometry_dtypes(frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce a table onto :data:`PHOTOMETRY_DTYPES`, adding missing columns."""
    for column, dtype in PHOTOMETRY_DTYPES.items():
        if column not in frame.columns:
            frame[column] = pd.Series(dtype=dtype)
        elif dtype != "object":
            frame[column] = pd.Series(frame[column]).astype(cast(Any, dtype))
    return frame.loc[:, list(PHOTOMETRY_DTYPES)]


__all__ = [
    "COLUMNS",
    "DEFAULT_MIN_SNR",
    "HOST_CATALOGS",
    "PHOTOMETRY_DTYPES",
    "ObsClass",
    "Observation",
    "Source",
    "apply_photometry_dtypes",
    "empty_photometry",
    "observations_from_frame",
]
