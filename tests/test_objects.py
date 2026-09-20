"""Tests for the object and source assembly layer."""

from __future__ import annotations

import json

import httpx
import pytest

from boom.cache import Cache
from boom.exceptions import APINotFoundError
from boom.models import COLUMNS
from boom.objects import (
    alert_collection,
    aux_collection,
    get_alerts,
    get_object,
    get_radec,
    get_source,
    photometry,
)

from .fixtures import LSST_ID, ZTF_ALERT, ZTF_ID


def bodies(api, catalog=None):
    found = [
        json.loads(request.content)
        for request in api.requests
        if request.url.path == "/queries/find"
    ]
    if catalog is None:
        return found
    return [body for body in found if body["catalog_name"] == catalog]


def test_collection_names():
    assert alert_collection("ZTF") == "ZTF_alerts"
    assert aux_collection("lsst") == "LSST_alerts_aux"


def test_geojson_longitude_is_shifted_back():
    assert get_radec(ZTF_ALERT) == (12.0, -30.0)


def test_missing_coordinates_return_none():
    assert get_radec({"coordinates": {}}) is None


def test_alerts_are_returned_newest_first(client, boom):
    get_alerts(client, "ZTF", ZTF_ID)
    assert bodies(boom, "ZTF_alerts")[0]["sort"] == {"candidate.jd": -1}


# ---- get_object ----


def test_alert_and_history_are_merged(client, boom):
    obj = get_object(client, "ZTF", ZTF_ID, cache=Cache())
    assert obj["objectId"] == ZTF_ID
    assert obj["candidate"]["magpsf"] == pytest.approx(18.2)
    assert len(obj["prv_candidates"]) == 3
    assert len(obj["fp_hists"]) == 2
    assert obj["cross_matches"]["NED"] == [{"z": 0.03}]


def test_trimming_sends_a_projection(client, boom):
    get_object(client, "ZTF", ZTF_ID, cache=Cache())
    assert bodies(boom, "ZTF_alerts_aux")[0]["projection"]


def test_trimming_can_be_disabled(client, boom):
    get_object(client, "ZTF", ZTF_ID, trim=False, cache=Cache())
    assert "projection" not in bodies(boom, "ZTF_alerts_aux")[0]


def test_alias_locates_the_other_survey(client, boom):
    obj = get_object(client, "ZTF", ZTF_ID, cache=Cache())
    assert obj["survey_matches"]["lsst"]["objectId"] == LSST_ID
    assert len(obj["survey_matches"]["lsst"]["prv_candidates"]) == 2


def test_alias_lookup_avoids_a_positional_search(client, boom):
    get_object(client, "ZTF", ZTF_ID, cache=Cache())
    filters = [body.get("filter", {}) for body in bodies(boom)]
    assert not any("coordinates.radec_geojson" in f for f in filters)


def test_positional_fallback_when_there_is_no_alias(client, api):
    documents = {
        "ZTF_alerts": [ZTF_ALERT],
        "ZTF_alerts_aux": [{"_id": ZTF_ID, "prv_candidates": []}],
        "LSST_alerts_aux": [{"_id": LSST_ID, "prv_candidates": []}],
        "LSST_alerts": [],
    }

    def find(request):
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "message": "ok",
                "data": documents.get(body["catalog_name"], []),
            },
        )

    api.route("/queries/find", handler=find)
    obj = get_object(client_of(api), "ZTF", ZTF_ID, cache=Cache())
    filters = [body.get("filter", {}) for body in bodies(api)]
    assert any("coordinates.radec_geojson" in f for f in filters)
    assert obj["survey_matches"]["lsst"]["objectId"] == LSST_ID


def client_of(api):
    from boom.client import BoomClient

    return BoomClient(
        username="tester",
        password="secret",
        url="https://boom.test",
        transport=api.transport,
        backoff_factor=0.0,
    )


def test_survey_matches_can_be_skipped(client, boom):
    obj = get_object(
        client, "ZTF", ZTF_ID, include_survey_matches=False, cache=Cache()
    )
    assert "survey_matches" not in obj
    assert not bodies(boom, "LSST_alerts")


def test_unknown_object_raises(client, api):
    api.route("/queries/find", [])
    with pytest.raises(APINotFoundError, match="No ZTF alert"):
        get_object(client, "ZTF", "nope", cache=Cache())


def test_missing_history_still_yields_an_object(client, api):
    def find(request):
        body = json.loads(request.content)
        data = [ZTF_ALERT] if body["catalog_name"] == "ZTF_alerts" else []
        return httpx.Response(200, json={"message": "ok", "data": data})

    api.route("/queries/find", handler=find)
    obj = get_object(client, "ZTF", ZTF_ID, cache=Cache())
    assert obj["prv_candidates"] == []
    assert obj["cross_matches"] == {}


def test_objects_are_cached(client, boom, tmp_path):
    cache = Cache(tmp_path)
    get_object(client, "ZTF", ZTF_ID, cache=cache)
    before = len(boom.requests)
    get_object(client, "ZTF", ZTF_ID, cache=cache)
    assert len(boom.requests) == before


def test_cache_keys_separate_the_variants(client, boom, tmp_path):
    cache = Cache(tmp_path)
    get_object(
        client, "ZTF", ZTF_ID, include_survey_matches=False, cache=cache
    )
    assert "survey_matches" in get_object(client, "ZTF", ZTF_ID, cache=cache)
    assert "prv_candidates" in get_object(
        client, "ZTF", ZTF_ID, trim=False, cache=cache
    )


# ---- photometry ----


def test_photometry_merges_both_surveys(client, boom):
    obj = get_object(client, "ZTF", ZTF_ID, cache=Cache())
    table = photometry(obj)
    assert set(table["survey"]) == {"ZTF", "LSST"}
    assert list(table.columns) == COLUMNS
    assert table["jd"].is_monotonic_increasing


def test_photometry_infers_the_survey_from_the_id(client, boom):
    obj = get_object(client, "ZTF", ZTF_ID, cache=Cache())
    assert not photometry(obj).empty


def test_photometry_of_an_empty_object_has_the_right_columns():
    table = photometry({"objectId": ZTF_ID})
    assert table.empty
    assert list(table.columns) == COLUMNS


# ---- get_source ----


def test_source_carries_identity_across_surveys(client, boom):
    src = get_source(client, "ZTF", ZTF_ID, cache=Cache())
    assert src.objectid == ZTF_ID
    assert src.ztfid == ZTF_ID
    assert src.lsstid == LSST_ID
    assert src.survey == "ZTF"


def test_source_position_comes_from_the_alert(client, boom):
    src = get_source(client, "ZTF", ZTF_ID, cache=Cache())
    assert src.ra == pytest.approx(12.0)
    assert src.dec == pytest.approx(-30.0)


def test_source_photometry_spans_both_surveys(client, boom):
    src = get_source(client, "ZTF", ZTF_ID, cache=Cache())
    assert {obs.survey for obs in src.photometry} == {"ZTF", "LSST"}
    assert src.ndethist > 0


def test_host_is_taken_from_the_crossmatches(client, boom):
    src = get_source(client, "ZTF", ZTF_ID, cache=Cache())
    assert src.host_origin == "NED"
    assert src.redshift == pytest.approx(0.031)
    assert src.redshift_origin == "NED_SPEC"
    assert src.offset == pytest.approx(2.3, abs=0.5)


def test_empty_crossmatch_key_is_dropped(client, boom):
    src = get_source(client, "ZTF", ZTF_ID, cache=Cache())
    assert "" not in src.crossmatches


def test_crossmatch_enrichment_can_be_skipped(client, boom):
    src = get_source(
        client, "ZTF", ZTF_ID, enrich_crossmatches=False, cache=Cache()
    )
    assert not bodies(boom, "NED")
    assert src.offset == pytest.approx(1.4)
    assert src.host_origin == "PS1"


def test_sources_are_cached(client, boom, tmp_path):
    cache = Cache(tmp_path)
    get_source(client, "ZTF", ZTF_ID, cache=cache)
    before = len(boom.requests)
    restored = get_source(client, "ZTF", ZTF_ID, cache=cache)
    assert len(boom.requests) == before
    assert restored.objectid == ZTF_ID
    assert restored.lsstid == LSST_ID
