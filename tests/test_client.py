"""Tests for the HTTP client: auth, retries, queries, error mapping."""

from __future__ import annotations

import json

import astropy.units as u
import httpx
import pytest
from astropy.coordinates import SkyCoord

from boom.client import (
    BoomClient,
    arcsec_to_metres,
    get_user_agent,
    normalize_coordinates,
    normalize_radius,
    normalize_unit,
)
from boom.exceptions import (
    APIAuthenticationError,
    APIError,
    APINotFoundError,
    QueryError,
)


def _bodies(api):
    return [
        json.loads(request.content)
        for request in api.requests
        if request.url.path != "/auth"
    ]


# ---- authentication ----


def test_authenticates_once_then_reuses_the_token(client, api):
    api.route("/queries/count", 3)
    client.count("ZTF_alerts")
    client.count("ZTF_alerts")
    assert api.auth_calls == 1
    assert api.paths().count("/auth") == 1


def test_bearer_token_is_attached(client, api):
    api.route("/catalogs", [{"name": "ZTF_alerts"}])
    client.catalogs()
    assert api.requests[-1].headers["Authorization"] == "Bearer token-1-1"


def test_user_agent_carries_no_machine_identity():
    agent = get_user_agent()
    assert agent.startswith("boom-client-python/")
    assert "Python/" in agent


def test_bad_credentials_raise_immediately(api):
    def unauthorized(request):
        return httpx.Response(401, json={"message": "Invalid client"})

    transport = httpx.MockTransport(unauthorized)
    client = BoomClient(
        username="tester",
        password="wrong",
        url="https://boom.test",
        transport=transport,
    )
    with pytest.raises(APIAuthenticationError, match="Invalid username"):
        client.count("ZTF_alerts")


def test_rejected_token_triggers_one_refresh(client, api):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, text="Unauthorized")
        return httpx.Response(200, json={"message": "ok", "data": 7})

    api.route("/queries/count", handler=flaky)
    assert client.count("ZTF_alerts") == 7
    assert api.auth_calls == 2


def test_a_second_rejection_is_not_retried_forever(client, api):
    api.route("/queries/count", status=401, message="nope")
    with pytest.raises(APIAuthenticationError):
        client.count("ZTF_alerts")
    assert api.auth_calls <= 2


# ---- retries ----


def test_server_errors_are_retried(client, api):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(200, json={"message": "ok", "data": []})

    api.route("/queries/find", handler=flaky)
    assert client.find("ZTF_alerts") == []
    assert calls["n"] == 3


def test_retries_give_up_and_surface_the_status(client, api):
    api.route("/queries/find", status=502, message="bad gateway")
    with pytest.raises(APIError) as excinfo:
        client.find("ZTF_alerts")
    assert excinfo.value.status_code == 502


def test_client_errors_are_not_retried(client, api):
    api.route("/queries/find", status=400, message="Invalid filter")
    with pytest.raises(QueryError, match="Invalid filter"):
        client.find("ZTF_alerts", {"$bogus": 1})
    assert api.paths().count("/queries/find") == 1


def test_transport_errors_are_wrapped(api):
    def boom(request):
        raise httpx.ConnectError("no route to host")

    client = BoomClient(
        username="tester",
        password="secret",
        url="https://boom.test",
        transport=httpx.MockTransport(boom),
        backoff_factor=0.0,
    )
    with pytest.raises(APIError, match="/auth failed"):
        client.count("ZTF_alerts")


# ---- error mapping ----


def test_missing_catalog_mentions_access(client, api):
    api.route(
        "/queries/find", status=404, message="Catalog nope does not exist"
    )
    with pytest.raises(APINotFoundError, match="outside your access"):
        client.find("nope")


def test_forbidden_maps_to_auth_error(client, api):
    api.route("/catalogs", status=403, message="forbidden")
    with pytest.raises(APIAuthenticationError):
        client.catalogs()


# ---- queries ----


def test_find_sends_the_documented_body(client, api):
    api.route("/queries/find", [{"_id": 1}])
    client.find(
        "ZTF_alerts",
        {"objectId": "ZTF18abc"},
        projection={"candidate.jd": 1},
        limit=5,
        skip=2,
        sort={"candidate.jd": -1},
    )
    assert api.body() == {
        "catalog_name": "ZTF_alerts",
        "filter": {"objectId": "ZTF18abc"},
        "projection": {"candidate.jd": 1},
        "limit": 5,
        "skip": 2,
        "sort": {"candidate.jd": -1},
        "max_time_ms": 60000,
    }


def test_find_defaults_to_an_empty_filter(client, api):
    api.route("/queries/find", [])
    client.find("ZTF_alerts")
    assert api.body()["filter"] == {}


def test_max_time_can_be_disabled(client, api):
    api.route("/queries/find", [])
    client.find("ZTF_alerts", max_time_ms=None)
    assert "max_time_ms" not in api.body()


def test_sort_accepts_a_list_of_pairs(client, api):
    api.route("/queries/find", [])
    client.find("ZTF_alerts", sort=[("candidate.jd", -1)])
    assert api.body()["sort"] == {"candidate.jd": -1}


def test_find_one_returns_none_when_empty(client, api):
    api.route("/queries/find", [])
    assert client.find_one("ZTF_alerts") is None


def test_find_one_asks_for_a_single_document(client, api):
    api.route("/queries/find", [{"_id": 1}])
    assert client.find_one("ZTF_alerts") == {"_id": 1}
    assert api.body()["limit"] == 1


def test_iter_find_pages_until_short_read(client, api):
    pages = [
        [{"_id": i} for i in range(3)],
        [{"_id": i} for i in range(3, 5)],
    ]

    def paged(request):
        page = pages.pop(0) if pages else []
        return httpx.Response(200, json={"message": "ok", "data": page})

    api.route("/queries/find", handler=paged)
    documents = list(client.iter_find("ZTF_alerts", page_size=3))
    assert [d["_id"] for d in documents] == [0, 1, 2, 3, 4]


def test_iter_find_sorts_by_id_so_pages_do_not_overlap(client, api):
    api.route("/queries/find", [])
    list(client.iter_find("ZTF_alerts"))
    assert api.body()["sort"] == {"_id": 1}


def test_iter_find_honours_a_total_limit(client, api):
    def page(request):
        size = json.loads(request.content)["limit"]
        return httpx.Response(
            200, json={"message": "ok", "data": [{"_id": 1}] * size}
        )

    api.route("/queries/find", handler=page)
    documents = list(client.iter_find("ZTF_alerts", page_size=2, limit=3))
    assert len(documents) == 3
    assert [b["limit"] for b in _bodies(api)] == [2, 1]


def test_count_returns_an_int(client, api):
    api.route("/queries/count", 12)
    assert client.count("ZTF_alerts", {"objectId": "x"}) == 12


def test_estimated_count_sends_only_the_catalog(client, api):
    api.route("/queries/estimated_count", 10**9)
    assert client.estimated_count("ZTF_alerts") == 10**9
    assert api.body() == {"catalog_name": "ZTF_alerts"}


def test_pipeline_refuses_to_write_to_the_database(client):
    with pytest.raises(QueryError, match=r"\$out"):
        client.pipeline("ZTF_alerts", [{"$out": "scratch"}])
    with pytest.raises(QueryError, match=r"\$merge"):
        client.pipeline("ZTF_alerts", [{"$merge": {"into": "scratch"}}])


def test_pipeline_passes_stages_through(client, api):
    api.route("/queries/pipeline", [{"_id": "g", "n": 2}])
    stages = [{"$match": {"objectId": "x"}}, {"$count": "n"}]
    assert client.pipeline("ZTF_alerts", stages) == [{"_id": "g", "n": 2}]
    assert api.body()["pipeline"] == stages


# ---- cone search ----


def test_cone_search_body(client, api):
    api.route("/queries/cone_search", {"0": []})
    client.cone_search("ZTF_alerts_aux", (12.0, -30.0), 5, "arcsec")
    body = api.body()
    assert body["object_coordinates"] == {"0": [12.0, -30.0]}
    assert body["radius"] == 5.0
    assert body["unit"] == "Arcseconds"


def test_cone_search_accepts_named_positions(client, api):
    api.route("/queries/cone_search", {"sn2024x": []})
    client.cone_search(
        "ZTF_alerts_aux", {"sn2024x": (12.0, -30.0)}, 2, "arcsec"
    )
    assert api.body()["object_coordinates"] == {"sn2024x": [12.0, -30.0]}


def test_cone_search_accepts_a_quantity_radius(client, api):
    api.route("/queries/cone_search", {"0": []})
    client.cone_search("ZTF_alerts_aux", (12.0, -30.0), 3600 * u.arcsec)
    body = api.body()
    assert body["unit"] == "Degrees"
    assert body["radius"] == pytest.approx(1.0)


def test_cone_search_accepts_a_skycoord(client, api):
    api.route("/queries/cone_search", {"0": []})
    client.cone_search(
        "ZTF_alerts_aux", SkyCoord(12.0, -30.0, unit="deg"), 2, "arcsec"
    )
    assert api.body()["object_coordinates"] == {"0": [12.0, -30.0]}


# ---- near search ----


def test_near_search_builds_a_nearsphere_filter(client, api):
    api.route("/queries/find", [])
    client.near_search("NED", 12.0, -30.0, 5.0)
    near = api.body()["filter"]["coordinates.radec_geojson"]["$nearSphere"]
    assert near["$geometry"]["coordinates"] == [12.0 - 180.0, -30.0]
    assert near["$maxDistance"] == pytest.approx(
        arcsec_to_metres(5.0), rel=1e-9
    )


def test_arcsec_to_metres_matches_one_arcminute():
    assert arcsec_to_metres(60.0) == pytest.approx(1853.2, rel=1e-3)


def test_near_search_reports_the_separation(client, api):
    api.route(
        "/queries/find",
        [
            {
                "_id": "NGC1234",
                "coordinates": {
                    "radec_geojson": {
                        "type": "Point",
                        "coordinates": [12.0 - 180.0, -30.0 + 1 / 3600],
                    }
                },
            }
        ],
    )
    match = client.near_search("NED", 12.0, -30.0, 5.0)[0]
    assert match["distance_arcsec"] == pytest.approx(1.0, abs=0.01)


def test_near_search_leaves_a_positionless_match_alone(client, api):
    api.route("/queries/find", [{"_id": "NGC1234"}])
    assert client.near_search("NED", 12.0, -30.0)[0] == {"_id": "NGC1234"}


def test_near_search_merges_an_extra_filter(client, api):
    api.route("/queries/find", [])
    client.near_search("NED", 12.0, -30.0, filter={"z": {"$gt": 0.01}})
    assert api.body()["filter"]["z"] == {"$gt": 0.01}


def test_near_search_defaults_to_the_single_nearest(client, api):
    api.route("/queries/find", [])
    client.near_search("NED", 12.0, -30.0)
    assert api.body()["limit"] == 1


# ---- catalogs ----


def test_catalog_names_are_flattened(client, api):
    api.route("/catalogs", [{"name": "ZTF_alerts"}, {"name": "LSST_alerts"}])
    assert client.catalog_names() == ["ZTF_alerts", "LSST_alerts"]


def test_catalog_sample_size_is_bounded(client):
    with pytest.raises(ValueError, match="between 1 and 1000"):
        client.catalog_sample("ZTF_alerts", size=5000)


def test_catalog_indexes_hits_the_right_path(client, api):
    api.route("/catalogs/ZTF_alerts/indexes", [{"name": "_id_"}])
    assert client.catalog_indexes("ZTF_alerts") == [{"name": "_id_"}]


# ---- cutouts ----


def test_cutouts_are_base64_decoded(client, api):
    api.route(
        "/surveys/ztf/cutouts",
        {
            "candid": 1,
            "cutoutScience": "aGVsbG8=",
            "cutoutTemplate": "aGVsbG8=",
            "cutoutDifference": "aGVsbG8=",
        },
    )
    payload = client.cutouts("ZTF", candid=1)
    assert payload["cutoutScience"] == b"hello"


def test_cutouts_need_exactly_one_selector(client):
    with pytest.raises(ValueError, match="exactly one"):
        client.cutouts("ZTF")
    with pytest.raises(ValueError, match="exactly one"):
        client.cutouts("ZTF", candid=1, object_id="ZTF18abc")


# ---- normalization helpers ----


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("deg", "Degrees"),
        ("DEGREES", "Degrees"),
        ("arcsec", "Arcseconds"),
        ("arcmin", "Arcminutes"),
        ("rad", "Radians"),
    ],
)
def test_unit_names_are_mapped_to_the_api_enum(given, expected):
    assert normalize_unit(given) == expected


def test_unknown_unit_is_rejected():
    with pytest.raises(ValueError, match="Unknown unit"):
        normalize_unit("parsec")


def test_radius_quantity_overrides_the_unit_argument():
    assert normalize_radius(60 * u.arcmin, "arcsec") == (1.0, "Degrees")


def test_sequence_of_pairs_is_indexed():
    assert normalize_coordinates([(1.0, 2.0), (3.0, 4.0)]) == {
        "0": [1.0, 2.0],
        "1": [3.0, 4.0],
    }


def test_array_skycoord_is_indexed():
    coords = SkyCoord([1.0, 3.0], [2.0, 4.0], unit="deg")
    assert normalize_coordinates(coords) == {
        "0": [1.0, 2.0],
        "1": [3.0, 4.0],
    }


def test_unsupported_coordinates_are_rejected():
    with pytest.raises(TypeError, match="coordinates must be"):
        normalize_coordinates(object())


# ---- lifecycle ----


def test_client_works_as_a_context_manager(api):
    with BoomClient(
        username="tester",
        password="secret",
        url="https://boom.test",
        transport=api.transport,
    ) as client:
        api.route("/queries/count", 1)
        assert client.count("ZTF_alerts") == 1


def test_repr_does_not_leak_the_password(client):
    assert "secret" not in repr(client)
