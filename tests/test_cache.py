"""Tests for the on-disk cache."""

from __future__ import annotations

import os
import time

import pandas as pd
import pytest

from boom.cache import Cache
from boom.exceptions import ConfigurationError


def test_disabled_without_a_directory():
    cache = Cache()
    assert not cache.enabled
    cache.set_json("objects", "ZTF18abc", {"a": 1})
    assert cache.get_json("objects", "ZTF18abc") is None


def test_from_env_is_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("BOOM_CACHE_DIR", raising=False)
    monkeypatch.delenv("BOOM_CACHE_MAX_AGE", raising=False)
    assert not Cache.from_env().enabled


def test_from_env_reads_the_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("BOOM_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("BOOM_CACHE_MAX_AGE", raising=False)
    assert Cache.from_env().enabled


def test_from_env_rejects_a_bad_max_age(monkeypatch, tmp_path):
    monkeypatch.setenv("BOOM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("BOOM_CACHE_MAX_AGE", "soon")
    with pytest.raises(ConfigurationError, match="BOOM_CACHE_MAX_AGE"):
        Cache.from_env()


def test_json_roundtrip(tmp_path):
    cache = Cache(tmp_path)
    cache.set_json("objects", "ZTF18abc", {"objectId": "ZTF18abc"})
    assert cache.get_json("objects", "ZTF18abc") == {"objectId": "ZTF18abc"}


def test_frame_roundtrip(tmp_path):
    cache = Cache(tmp_path)
    frame = pd.DataFrame({"mjd": [1.0, 2.0], "mag": [19.0, 18.5]})
    cache.set_frame("photometry", "ZTF18abc", frame)
    restored = cache.get_frame("photometry", "ZTF18abc")
    pd.testing.assert_frame_equal(restored, frame)


def test_stale_entries_are_ignored(tmp_path):
    cache = Cache(tmp_path, max_age=1)
    cache.set_json("objects", "ZTF18abc", {"a": 1})
    path = cache.path("objects", "ZTF18abc", ".json")
    os.utime(path, (time.time() - 10, time.time() - 10))
    assert cache.get_json("objects", "ZTF18abc") is None


def test_max_age_none_keeps_entries(tmp_path):
    cache = Cache(tmp_path, max_age=None)
    cache.set_json("objects", "ZTF18abc", {"a": 1})
    path = cache.path("objects", "ZTF18abc", ".json")
    os.utime(path, (0, 0))
    assert cache.get_json("objects", "ZTF18abc") == {"a": 1}


def test_corrupt_entries_are_treated_as_misses(tmp_path):
    cache = Cache(tmp_path)
    cache.set_json("objects", "ZTF18abc", {"a": 1})
    cache.path("objects", "ZTF18abc", ".json").write_text("{not json")
    assert cache.get_json("objects", "ZTF18abc") is None


def test_unsafe_keys_stay_inside_the_cache_directory(tmp_path):
    cache = Cache(tmp_path)
    cache.set_json("objects", "../../escape", {"a": 1})
    assert cache.get_json("objects", "../../escape") == {"a": 1}
    written = list(tmp_path.rglob("*.json"))
    assert len(written) == 1
    assert tmp_path in written[0].parents


def test_distinct_unsafe_keys_do_not_collide(tmp_path):
    cache = Cache(tmp_path)
    cache.set_json("objects", "a/b", {"which": "first"})
    cache.set_json("objects", "a:b", {"which": "second"})
    assert cache.get_json("objects", "a/b") == {"which": "first"}
    assert cache.get_json("objects", "a:b") == {"which": "second"}


def test_clear_namespace(tmp_path):
    cache = Cache(tmp_path)
    cache.set_json("objects", "one", {"a": 1})
    cache.set_json("photometry", "one", {"a": 1})
    cache.clear("objects")
    assert cache.get_json("objects", "one") is None
    assert cache.get_json("photometry", "one") == {"a": 1}


def test_clear_everything(tmp_path):
    cache = Cache(tmp_path)
    cache.set_json("objects", "one", {"a": 1})
    cache.clear()
    assert cache.get_json("objects", "one") is None


def test_no_temporary_file_is_left_behind(tmp_path):
    cache = Cache(tmp_path)
    cache.set_json("objects", "one", {"a": 1})
    assert list(tmp_path.rglob("*.tmp")) == []


def test_repr_mentions_whether_it_is_on(tmp_path):
    assert repr(Cache()) == "Cache(disabled)"
    assert "max_age" in repr(Cache(tmp_path))


# ---- bulk reads ----


def test_load_frames_concatenates_a_namespace(tmp_path):
    cache = Cache(tmp_path)
    for i, name in enumerate(["ZTF18a", "ZTF18b", "ZTF18c"]):
        cache.set_frame(
            "sources", name, pd.DataFrame([{"objectid": name, "n": i}])
        )
    table = cache.load_frames("sources")
    assert len(table) == 3
    assert sorted(table["objectid"]) == ["ZTF18a", "ZTF18b", "ZTF18c"]


def test_load_frames_keeps_stale_entries_by_default(tmp_path):
    cache = Cache(tmp_path, max_age=1)
    cache.set_frame(
        "sources", "ZTF18a", pd.DataFrame([{"objectid": "ZTF18a"}])
    )
    os.utime(cache.path("sources", "ZTF18a", ".parquet"), (0, 0))
    assert len(cache.load_frames("sources")) == 1
    assert cache.load_frames("sources", fresh_only=True).empty


def test_load_frames_on_an_unknown_namespace_is_empty(tmp_path):
    assert Cache(tmp_path).load_frames("nope").empty


def test_load_frames_on_a_disabled_cache_is_empty():
    assert Cache().load_frames("sources").empty


def test_load_frames_skips_a_corrupt_entry(tmp_path):
    cache = Cache(tmp_path)
    cache.set_frame("sources", "good", pd.DataFrame([{"objectid": "good"}]))
    (tmp_path / "sources" / "bad.parquet").write_text("not parquet")
    assert len(cache.load_frames("sources")) == 1


def test_keys_lists_what_is_stored(tmp_path):
    cache = Cache(tmp_path)
    cache.set_json("objects", "ZTF18b", {"a": 1})
    cache.set_json("objects", "ZTF18a", {"a": 1})
    assert cache.keys("objects") == ["ZTF18a", "ZTF18b"]


def test_keys_on_a_disabled_cache_is_empty():
    assert Cache().keys("objects") == []
