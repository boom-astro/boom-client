"""Tests for merging, deduplicating and cleaning photometry."""

from __future__ import annotations

import pandas as pd
import pytest

from boom.models import COLUMNS
from boom.photometry import build_photometry

from .fixtures import LSST_AUX, ZTF_AUX, lsst_point, ztf_point


def test_empty_object_yields_the_canonical_columns():
    frame = build_photometry({}, "ZTF")
    assert frame.empty
    assert list(frame.columns) == COLUMNS


def test_every_history_is_merged():
    frame = build_photometry(ZTF_AUX, "ZTF")
    assert set(frame["det_type"]) == {"alert", "fp", "ul"}


def test_forced_photometry_does_not_duplicate_an_alert_epoch():
    frame = build_photometry(ZTF_AUX, "ZTF")
    duplicated = frame[frame["jd"] == 2460000.5]
    assert len(duplicated) == 1
    assert duplicated.iloc[0]["det_type"] == "alert"


def test_rows_are_sorted_by_epoch():
    frame = build_photometry(ZTF_AUX, "ZTF")
    assert frame["jd"].is_monotonic_increasing


def test_mjd_is_derived_from_jd():
    frame = build_photometry(ZTF_AUX, "ZTF")
    assert frame["mjd"].iloc[0] == pytest.approx(
        frame["jd"].iloc[0] - 2400000.5
    )


def test_survey_column_is_stamped():
    assert set(build_photometry(LSST_AUX, "LSST")["survey"]) == {"LSST"}


def test_ztf_string_isdiffpos_is_understood_without_flux():
    document = {
        "prv_candidates": [
            {k: v for k, v in ztf_point(2460000.5).items() if k != "psfFlux"},
            {
                **{
                    k: v
                    for k, v in ztf_point(2460001.5).items()
                    if k != "psfFlux"
                },
                "isdiffpos": "f",
            },
        ]
    }
    frame = build_photometry(document, "ZTF")
    assert list(frame["isdiffpos"]) == [True, False]


# ---- quality cuts ----


@pytest.mark.parametrize(
    "bad",
    [
        {"drb": 0.1},
        {"nbad": 5},
        {"diffmaglim": 17.0},
    ],
)
def test_ztf_drops_flagged_rows(bad):
    document = {"prv_candidates": [ztf_point(2460000.5, **bad)]}
    assert build_photometry(document, "ZTF").empty


@pytest.mark.parametrize(
    "bad",
    [
        {"reliability": 0.1},
        {"isDipole": True},
        {"centroid_flag": True},
        {"psfFlux_flag": True},
        {"pixelFlags": True},
        {"glint_trail": True},
    ],
)
def test_lsst_drops_flagged_rows(bad):
    document = {"prv_candidates": [lsst_point(2460000.5, **bad)]}
    assert build_photometry(document, "LSST").empty


def test_cuts_can_be_turned_off():
    document = {"prv_candidates": [ztf_point(2460000.5, drb=0.1)]}
    assert len(build_photometry(document, "LSST", quality_cuts=False)) == 1


def test_missing_flag_columns_do_not_drop_rows():
    document = {"prv_candidates": [{"jd": 2460000.5, "band": "g"}]}
    assert len(build_photometry(document, "ZTF")) == 1


def test_missing_flag_columns_do_not_crash_lsst():
    document = {"prv_candidates": [{"jd": 2460000.5, "band": "i"}]}
    assert len(build_photometry(document, "LSST")) == 1


def test_dtypes_survive_a_parquet_round_trip(tmp_path):
    frame = build_photometry(ZTF_AUX, "ZTF")
    path = tmp_path / "photometry.parquet"
    frame.to_parquet(path, index=False)
    pd.testing.assert_frame_equal(pd.read_parquet(path), frame)


def test_stored_isdiffpos_wins_over_the_flux_sign():
    document = {
        "prv_candidates": [
            ztf_point(2460000.5, isdiffpos="t", psfFlux=-5.0),
            ztf_point(2460001.5, isdiffpos="f", psfFlux=5.0),
        ]
    }
    frame = build_photometry(document, "ZTF")
    assert list(frame["isdiffpos"]) == [True, False]


def test_flux_sign_fills_in_a_missing_isdiffpos():
    rows = [
        {k: v for k, v in ztf_point(2460000.5).items() if k != "isdiffpos"},
        {
            **{
                k: v
                for k, v in ztf_point(2460001.5).items()
                if k != "isdiffpos"
            },
            "psfFlux": -5.0,
        },
    ]
    frame = build_photometry({"prv_candidates": rows}, "ZTF")
    assert list(frame["isdiffpos"]) == [True, False]


def test_upper_limits_are_not_positive_differences():
    frame = build_photometry(ZTF_AUX, "ZTF")
    limits = frame[frame["det_type"] == "ul"]
    assert not limits.empty
    assert not limits["isdiffpos"].any()


def test_negative_differences_are_kept_not_cut():
    document = {
        "prv_candidates": [
            lsst_point(
                2460000.5, isNegative=True, isdiffpos=False, psfFlux=-90.0
            )
        ]
    }
    frame = build_photometry(document, "LSST")
    assert len(frame) == 1
    assert not frame["isdiffpos"].iloc[0]
