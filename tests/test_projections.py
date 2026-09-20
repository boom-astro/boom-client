"""Tests for the catalog field projections."""

from __future__ import annotations

import pytest

from boom.projections import alert_projection, aux_projection


def test_alert_projection_keeps_the_identity_and_position():
    projection = alert_projection()
    assert projection["objectId"] == 1
    assert projection["candidate.ra"] == 1
    assert projection["coordinates"] == 1


@pytest.mark.parametrize("survey", ["ZTF", "LSST", "ztf", "lsst"])
def test_aux_projection_covers_the_histories(survey):
    projection = aux_projection(survey)
    assert projection["prv_candidates.jd"] == 1
    assert projection["fp_hists.magpsf"] == 1
    assert projection["cross_matches"] == 1
    assert projection["aliases"] == 1


def test_ztf_asks_for_its_own_quality_columns():
    projection = aux_projection("ZTF")
    assert "prv_candidates.drb" in projection
    assert "prv_candidates.nbad" in projection
    assert "prv_nondetections.jd" in projection


def test_lsst_asks_for_its_own_flags():
    projection = aux_projection("LSST")
    assert "prv_candidates.reliability" in projection
    assert "prv_candidates.glint_trail" in projection


def test_lsst_has_no_nondetections():
    assert not any(
        key.startswith("prv_nondetections") for key in aux_projection("LSST")
    )


def test_every_projected_quality_column_is_actually_used():
    from boom.photometry import CUTS

    for survey in ("ZTF", "LSST"):
        projection = aux_projection(survey)
        cuts = CUTS[survey]
        wanted = set(cuts.flags) | set(cuts.min_values) | set(cuts.max_values)
        projected = {key.split(".", 1)[1] for key in projection if "." in key}
        assert wanted <= projected
