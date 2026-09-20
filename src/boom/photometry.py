"""Turn an object's raw histories into one clean photometry table.

Each survey scatters its measurements over three lists (``prv_candidates``,
``fp_hists``, ``prv_nondetections``) that overlap in time, and flags bad
measurements with survey-specific columns. This module merges them,
deduplicates on epoch and drops what the survey marks as unusable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .client import Survey
from .models import COLUMNS, apply_photometry_dtypes, empty_photometry

#: Epoch precision used to decide that two measurements are the same visit.
JD_TOLERANCE = 1e-6

_RENAMES = {
    "psfFlux": "psf_flux",
    "psfFluxErr": "psf_flux_err",
    "snr_psf": "snr",
}

_HISTORIES = (
    ("prv_candidates", "alert"),
    ("fp_hists", "fp"),
    ("prv_nondetections", "ul"),
)


@dataclass(frozen=True)
class QualityCuts:
    """Rows a survey marks as unusable, dropped when *quality_cuts* is on.

    Attributes
    ----------
    flags : tuple[str, ...]
        Boolean columns whose truth means "discard this measurement".
    min_values : dict[str, float]
        Columns dropped when they fall below the given value.
    max_values : dict[str, float]
        Columns dropped when they rise above the given value.
    """

    flags: tuple[str, ...] = ()
    min_values: dict[str, float] = field(default_factory=dict)
    max_values: dict[str, float] = field(default_factory=dict)


#: ZTF drops bad-pixel-heavy and low-real-bogus rows, and visits whose
#: limiting magnitude is too shallow to constrain anything.
ZTF_CUTS = QualityCuts(
    min_values={"drb": 0.65, "diffmaglim": 19.0},
    max_values={"nbad": 1.0},
)

#: LSST already flags its own problems, so the cut is mostly a flag sweep.
#: ``isNegative`` is deliberately absent: it records the sign of the
#: difference, not a defect, and it is already carried by ``isdiffpos``.
#: Cutting on it would delete every fading epoch, which on a variable
#: source is most of the light curve.
LSST_CUTS = QualityCuts(
    flags=(
        "centroid_flag",
        "psfFlux_flag",
        "isDipole",
        "pixelFlags",
        "glint_trail",
    ),
    min_values={"reliability": 0.4},
)

CUTS: dict[str, QualityCuts] = {"ZTF": ZTF_CUTS, "LSST": LSST_CUTS}


def build_photometry(
    document: dict[str, Any],
    survey: Survey,
    *,
    quality_cuts: bool = True,
) -> pd.DataFrame:
    """Merge one object's histories into a single table.

    Parameters
    ----------
    document : dict
        An object as returned by :func:`boom.objects.get_object`, or any
        document carrying the history lists.
    survey : str
        ``"ZTF"`` or ``"LSST"``: picks the column names and the cuts.
    quality_cuts : bool
        Drop the rows the survey flags as unusable. Turn it off to see
        everything the pipeline stored.

    Returns
    -------
    pandas.DataFrame
        One row per measurement, sorted by epoch, with the columns listed
        in :data:`COLUMNS`. ``det_type`` is ``"alert"``, ``"fp"`` or
        ``"ul"``; ``isdiffpos`` marks a positive (source-brightening)
        difference.
    """
    key = survey.upper()
    frames = []
    for history, det_type in _HISTORIES:
        rows = document.get(history) or []
        if not rows:
            continue
        frame = pd.DataFrame(rows)
        frame["det_type"] = det_type
        frames.append(frame)

    if not frames:
        return empty_photometry()

    merged = _deduplicate(frames)
    merged = merged.rename(columns=_RENAMES)
    merged = merged.loc[:, ~merged.columns.duplicated()]

    if quality_cuts:
        merged = merged[~_bad_rows(merged, CUTS[key])]

    return _finalize(merged, key)


def _deduplicate(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate histories, keeping the first source to report an epoch.

    Forced photometry and non-detections re-report epochs that already
    came through as alerts. The lists are merged in descending order of
    trust, so the alert wins and the duplicate is dropped.
    """
    merged = frames[0]
    for frame in frames[1:]:
        if "jd" not in frame.columns:
            continue
        seen: Any = merged["jd"].round(6) if "jd" in merged.columns else []
        keep = ~frame["jd"].round(6).isin(seen)
        if keep.any():
            merged = pd.concat([merged, frame[keep]], ignore_index=True)
    return merged


def _bad_rows(frame: pd.DataFrame, cuts: QualityCuts) -> pd.Series[bool]:
    """Boolean mask of the rows a survey considers unusable."""
    bad = pd.Series(False, index=frame.index)
    for column in cuts.flags:
        if column in frame.columns:
            bad |= frame[column].fillna(False).astype(bool)
    for column, threshold in cuts.min_values.items():
        if column in frame.columns:
            bad |= pd.to_numeric(frame[column], errors="coerce") < threshold
    for column, threshold in cuts.max_values.items():
        if column in frame.columns:
            bad |= pd.to_numeric(frame[column], errors="coerce") > threshold
    return bad


def _finalize(frame: pd.DataFrame, survey: str) -> pd.DataFrame:
    """Add the derived columns and return the canonical column order."""
    frame = frame.copy()
    frame["survey"] = survey

    if "snr" not in frame.columns:
        frame["snr"] = np.nan
    frame["isdiffpos"] = _difference_sign(frame)

    frame["mjd"] = pd.to_numeric(frame["jd"], errors="coerce") - 2400000.5

    for column in COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA

    frame = frame.sort_values("jd", ignore_index=True).replace({None: np.nan})
    return apply_photometry_dtypes(frame)


def _difference_sign(frame: pd.DataFrame) -> pd.Series[Any]:
    """Whether each measurement is a positive difference.

    The surveys record this directly, so the stored value wins. The flux
    sign only fills the gaps: upper limits carry neither field, and older
    records may predate one of them.
    """
    stored: pd.Series[Any] = (
        frame["isdiffpos"].map(_as_bool)
        if "isdiffpos" in frame.columns
        else pd.Series(pd.NA, index=frame.index, dtype="object")
    )
    if "psf_flux" not in frame.columns:
        return stored
    derived = pd.to_numeric(frame["psf_flux"], errors="coerce") > 0.0
    return stored.where(stored.notna(), derived)


def _as_bool(value: Any) -> Any:
    """ZTF writes isdiffpos as ``"t"``/``"f"``, LSST as a real boolean.

    A missing value has to come back as ``pd.NA`` rather than go through
    ``bool()``: a float NaN is truthy, so a row that merely lacks the
    field would otherwise read as a positive difference.
    """
    if isinstance(value, str):
        return value.lower() in {"t", "true", "1"}
    if value is None or pd.isna(value):
        return pd.NA
    return bool(value)


def _empty() -> pd.DataFrame:
    return pd.DataFrame(
        {column: pd.Series(dtype="object") for column in COLUMNS}
    )


__all__ = [
    "CUTS",
    "LSST_CUTS",
    "ZTF_CUTS",
    "QualityCuts",
    "build_photometry",
]
