"""Tests for credential and URL resolution."""

from __future__ import annotations

import pytest

from boom.config import API_URLS, BoomConfig, get_base_url
from boom.exceptions import ConfigurationError


def test_defaults_to_production(monkeypatch):
    monkeypatch.delenv("BOOM_API_URL", raising=False)
    monkeypatch.delenv("BOOM_ENV", raising=False)
    assert get_base_url() == API_URLS["production"]


def test_env_selects_deployment(monkeypatch):
    monkeypatch.delenv("BOOM_API_URL", raising=False)
    monkeypatch.setenv("BOOM_ENV", "local")
    assert get_base_url() == API_URLS["local"]


def test_explicit_url_wins_over_env(monkeypatch):
    monkeypatch.setenv("BOOM_ENV", "local")
    monkeypatch.setenv("BOOM_API_URL", "https://elsewhere.test/")
    assert get_base_url() == "https://elsewhere.test"


def test_unknown_env_is_rejected(monkeypatch):
    monkeypatch.delenv("BOOM_API_URL", raising=False)
    monkeypatch.setenv("BOOM_ENV", "staging")
    with pytest.raises(ConfigurationError, match="Invalid BOOM_ENV"):
        get_base_url()


def test_credentials_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("BOOM_API_USER", "alice")
    monkeypatch.setenv("BOOM_API_PASSWORD", "hunter2")
    monkeypatch.setenv("BOOM_API_URL", "https://boom.test")
    config = BoomConfig.from_env()
    assert (config.username, config.password) == ("alice", "hunter2")


def test_missing_username_is_reported(monkeypatch):
    monkeypatch.delenv("BOOM_API_USER", raising=False)
    monkeypatch.setenv("BOOM_API_PASSWORD", "hunter2")
    with pytest.raises(ConfigurationError, match="BOOM_API_USER"):
        BoomConfig.from_env()


def test_missing_password_is_reported(monkeypatch):
    monkeypatch.delenv("BOOM_API_PASSWORD", raising=False)
    with pytest.raises(ConfigurationError, match="BOOM_API_PASSWORD"):
        BoomConfig.from_env(username="alice")


def test_babamul_token_is_rejected_with_a_pointer():
    with pytest.raises(ConfigurationError, match="babamul package"):
        BoomConfig.from_env(username="alice", password="bbml_abc123")


def test_trailing_slash_is_stripped():
    config = BoomConfig(username="a", password="b", url="https://boom.test/")
    assert config.url == "https://boom.test"
