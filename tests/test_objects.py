"""Tests for the object assembly and photometry layer."""

from __future__ import annotations

import httpx
import pytest

from boom.cache import Cache
from boom.exceptions import APINotFoundError
from boom.objects import (
    alert_collection,
    aux_collection,
    get_object,
    get_radec,
    photometry,
)


def _geojson(ra, dec):
    return {
        "radec_geojson": {"type": "Point", "coordinates": [ra - 180.0, dec]}
    }


ZTF_ALERT = {
    "_id": 123,
    "objectId": "ZTF18abcdefg",
    "candidate": {"jd": 2460000.5, "magpsf": 18.2, "band": "g"},
    "coordinates": _geojson(12.0, -30.0),
}

ZTF_AUX = {
    "_id": "ZTF18abcdefg",
    "prv_candidates": [
        {
            "jd": 2459999.5,
            "magpsf": 18.5,
            "sigmapsf": 0.1,
            "snr": 10,
            "band": "g",
        },
        {"jd": 2459998.5, "diffmaglim": 20.1, "snr": 1, "band": "r"},
    ],
    "prv_nondetections": [{"jd": 2459997.5, "diffmaglim": 20.5, "band": "g"}],
    "fp_hists": [
        {
            "jd": 2460000.5,
            "magpsf": 18.2,
            "sigmapsf": 0.05,
            "snr": 30,
            "band": "g",
        }
    ],
    "cross_matches": {"NED": [{"z": 0.03}]},
    "coordinates": _geojson(12.0, -30.0),
}

LSST_AUX = {
    "_id": "42",
    "prv_candidates": [
        {
            "jd": 2459999.0,
            "magpsf": 19.0,
            "sigmapsf": 0.1,
            "snr": 8,
            "band": "i",
        }
    ],
    "fp_hists": [],
    "coordinates": _geojson(12.0, -30.0),
}


@pytest.fixture
def boom(api):
    """Serve the two find queries get_object makes, plus the cone search."""

    def find(request):
        import json

        body = json.loads(request.content)
        catalog = body["catalog_name"]
        if catalog == "ZTF_alerts":
            data = [ZTF_ALERT]
        elif catalog == "ZTF_alerts_aux":
            data = [ZTF_AUX]
        else:
            data = []
        return httpx.Response(200, json={"message": "ok", "data": data})

    api.route("/queries/find", handler=find)
    api.route("/queries/cone_search", {"target": [LSST_AUX]})
    return api


def test_collection_names():
    assert alert_collection("ZTF") == "ZTF_alerts"
    assert aux_collection("lsst") == "LSST_alerts_aux"


def test_geojson_longitude_is_shifted_back():
    assert get_radec(ZTF_ALERT) == (12.0, -30.0)


def test_missing_coordinates_return_none():
    assert get_radec({"coordinates": {}}) is None


def test_object_merges_alert_and_aux(client, boom):
    obj = get_object(client, "ZTF", "ZTF18abcdefg", cache=Cache())
    assert obj["objectId"] == "ZTF18abcdefg"
    assert obj["candidate"]["magpsf"] == 18.2
    assert len(obj["prv_candidates"]) == 2
    assert obj["cross_matches"] == {"NED": [{"z": 0.03}]}


def test_object_attaches_the_other_survey(client, boom):
    obj = get_object(client, "ZTF", "ZTF18abcdefg", cache=Cache())
    assert obj["survey_matches"]["lsst"]["objectId"] == "42"


def test_survey_matches_can_be_skipped(client, boom):
    obj = get_object(
        client,
        "ZTF",
        "ZTF18abcdefg",
        include_survey_matches=False,
        cache=Cache(),
    )
    assert "survey_matches" not in obj
    assert "/queries/cone_search" not in boom.paths()


def test_unknown_object_raises(client, api):
    api.route("/queries/find", [])
    with pytest.raises(APINotFoundError, match="No ZTF alert"):
        get_object(client, "ZTF", "nope", cache=Cache())


def test_missing_aux_still_yields_an_object(client, api):
    def find(request):
        import json

        body = json.loads(request.content)
        data = [ZTF_ALERT] if body["catalog_name"] == "ZTF_alerts" else []
        return httpx.Response(200, json={"message": "ok", "data": data})

    api.route("/queries/find", handler=find)
    api.route("/queries/cone_search", {"target": []})
    obj = get_object(client, "ZTF", "ZTF18abcdefg", cache=Cache())
    assert obj["prv_candidates"] == []
    assert obj["fp_hists"] == []


def test_second_call_is_served_from_the_cache(client, boom, tmp_path):
    cache = Cache(tmp_path)
    get_object(client, "ZTF", "ZTF18abcdefg", cache=cache)
    before = len(boom.requests)
    again = get_object(client, "ZTF", "ZTF18abcdefg", cache=cache)
    assert len(boom.requests) == before
    assert again["objectId"] == "ZTF18abcdefg"


def test_cache_keys_separate_the_two_shapes(client, boom, tmp_path):
    cache = Cache(tmp_path)
    get_object(
        client,
        "ZTF",
        "ZTF18abcdefg",
        include_survey_matches=False,
        cache=cache,
    )
    obj = get_object(client, "ZTF", "ZTF18abcdefg", cache=cache)
    assert "survey_matches" in obj


# ---- photometry ----


def test_photometry_merges_every_source(client, boom):
    obj = get_object(client, "ZTF", "ZTF18abcdefg", cache=Cache())
    frame = photometry(obj)
    assert set(frame["origin"]) == {
        "prv_candidates",
        "fp_hists",
        "prv_nondetections",
    }
    assert set(frame["survey"]) == {"ZTF", "LSST"}


def test_photometry_is_sorted_by_time(client, boom):
    obj = get_object(client, "ZTF", "ZTF18abcdefg", cache=Cache())
    frame = photometry(obj)
    assert frame["mjd"].is_monotonic_increasing


def test_photometry_marks_upper_limits(client, boom):
    obj = get_object(client, "ZTF", "ZTF18abcdefg", cache=Cache())
    frame = photometry(obj)
    limits = frame[frame["lim"]]
    assert limits["magerr"].isna().all()


def test_photometry_of_an_empty_object_has_the_right_columns():
    frame = photometry({"objectId": "ZTF18abcdefg"})
    assert frame.empty
    assert list(frame.columns) == [
        "mjd",
        "mag",
        "magerr",
        "band",
        "lim",
        "survey",
        "origin",
    ]
