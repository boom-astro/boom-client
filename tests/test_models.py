"""Tests for the Observation and Source models."""

from __future__ import annotations

import math

import pytest

from boom.cache import Cache
from boom.models import (
    COLUMNS,
    Observation,
    Source,
    empty_photometry,
    observations_from_frame,
)


def observation(jd: float, **overrides) -> Observation:
    fields = {
        "jd": jd,
        "band": "g",
        "survey": "ZTF",
        "magpsf": 18.5,
        "sigmapsf": 0.1,
        "diffmaglim": 20.4,
        "snr": 10.0,
        "ra": 12.0,
        "dec": -30.0,
        "isdiffpos": True,
    }
    fields.update(overrides)
    return Observation(**fields)


def source(**overrides) -> Source:
    fields = {
        "objectid": "ZTF18abcdefg",
        "survey": "ZTF",
        "jd": 2460000.5,
        "ra": 12.0,
        "dec": -30.0,
        "photometry": [
            observation(2459999.5, magpsf=18.8),
            observation(2460000.5, magpsf=18.2, band="r"),
        ],
    }
    fields.update(overrides)
    return Source(**fields)


# ---- Observation ----


def test_mjd_is_derived():
    assert observation(2460000.5).mjd == pytest.approx(60000.0)


def test_estimated_limit_follows_the_noise():
    obs = observation(2460000.5, magpsf=18.0, snr=5.0)
    assert obs.estdiffmaglim == pytest.approx(18.0)


def test_estimated_limit_is_none_without_a_measurement():
    assert observation(2460000.5, magpsf=None).estdiffmaglim is None
    assert observation(2460000.5, snr=0.0).estdiffmaglim is None


def test_unknown_fields_are_dropped():
    obs = Observation(jd=1.0, band="g", survey="ZTF", nonsense=3)
    assert not hasattr(obs, "nonsense")


def test_observations_round_trip_through_a_frame():
    frame = empty_photometry()
    assert observations_from_frame(frame) == []


# ---- Source summaries ----


def test_detection_count():
    assert source().ndethist == 2


def test_filters_are_unique_and_sorted():
    assert source().filters == ["g", "r"]
    assert source().ndetfilters == 2


def test_epoch_range_and_age():
    src = source()
    assert src.jdstarthist == 2459999.5
    assert src.jdendhist == 2460000.5
    assert src.age == pytest.approx(1.0)


def test_peak_magnitude_is_the_brightest():
    assert source().peak_mag == pytest.approx(18.2)


def test_median_position():
    assert source().median_ra == pytest.approx(12.0)
    assert source().median_dec == pytest.approx(-30.0)


def test_summaries_are_nan_without_detections():
    src = source(photometry=[])
    assert src.ndethist == 0
    assert src.filters == []
    assert math.isnan(src.peak_mag)
    assert math.isnan(src.jdstarthist)


# ---- detections ----


def test_negative_differences_are_not_detections():
    src = source(photometry=[observation(2460000.5, isdiffpos=False)])
    assert src.get_detections().empty


def test_low_signal_to_noise_is_not_a_detection():
    src = source(photometry=[observation(2460000.5, snr=1.0)])
    assert src.get_detections().empty


def test_measurement_fainter_than_the_limit_is_rejected():
    src = source(
        photometry=[observation(2460000.5, magpsf=23.0, diffmaglim=20.0)]
    )
    assert src.get_detections().empty


def test_min_snr_is_configurable():
    src = source(photometry=[observation(2460000.5, snr=5.0)])
    assert src.get_detections(min_snr=10.0).empty
    assert len(src.get_detections(min_snr=1.0)) == 1


def test_lsst_forced_photometry_is_excluded_by_default():
    src = source(
        photometry=[observation(2460000.5, survey="LSST", det_type="fp")]
    )
    assert src.get_detections().empty
    assert len(src.get_detections(include_lsst_fp=True)) == 1


def test_ztf_forced_photometry_is_kept():
    src = source(photometry=[observation(2460000.5, det_type="fp")])
    assert len(src.get_detections()) == 1


def test_photometry_frame_has_the_canonical_columns():
    assert list(source().get_photometry().columns) == COLUMNS


# ---- interchange ----


def test_json_round_trip():
    src = source(redshift=0.03, crossmatches={"NED": [{"z": 0.03}]})
    restored = Source.from_json(src.to_json())
    assert restored.objectid == src.objectid
    assert restored.redshift == pytest.approx(0.03)
    assert restored.crossmatches == {"NED": [{"z": 0.03}]}
    assert len(restored.photometry) == len(src.get_detections())


def test_json_can_keep_every_measurement():
    src = source(photometry=[observation(2460000.5, isdiffpos=False)])
    assert len(Source.from_json(src.to_json()).photometry) == 0
    kept = Source.from_json(src.to_json(detections_only=False))
    assert len(kept.photometry) == 1


def test_alert_style_feeds_the_babamul_plotters():
    document = source().to_alert_style()
    assert document["objectId"] == "ZTF18abcdefg"
    assert document["candidate"]["ra"] == pytest.approx(12.0)
    assert len(document["prv_candidates"]) == 1


def test_plot_lightcurve_runs_without_a_display():
    import matplotlib

    matplotlib.use("Agg")
    source().plot_lightcurve(show=False)


# ---- persistence ----


def test_parquet_round_trip(tmp_path):
    src = source(redshift=0.03, crossmatches={"NED": [{"z": 0.03}]})
    cache = Cache(tmp_path)
    src.save(cache)
    restored = Source.load(cache, src.objectid)
    assert restored is not None
    assert restored.objectid == src.objectid
    assert restored.redshift == pytest.approx(0.03)
    assert restored.crossmatches == {"NED": [{"z": 0.03}]}
    assert len(restored.photometry) == 2
    assert restored.peak_mag == pytest.approx(src.peak_mag)


def test_loading_an_unknown_source_returns_none(tmp_path):
    assert Source.load(Cache(tmp_path), "nope") is None


def test_saving_to_a_disabled_cache_is_a_no_op():
    src = source()
    src.save(Cache())
    assert Source.load(Cache(), src.objectid) is None


def test_photometry_free_source_round_trips(tmp_path):
    cache = Cache(tmp_path)
    source(photometry=[]).save(cache)
    restored = Source.load(cache, "ZTF18abcdefg")
    assert restored is not None
    assert restored.photometry == []
