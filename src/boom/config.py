"""Configuration for connecting to the BOOM API."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .exceptions import ConfigurationError

API_URLS = {
    "local": "http://localhost:4000",
    "production": "https://api.kaboom.caltech.edu",  # Caltech, default
    "backup": "https://babamul.umn.edu/api",  # University of Minnesota
}

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_FACTOR = 0.5


def _get_env() -> str:
    """Get the validated BOOM_ENV value (defaults to "production")."""
    env = os.environ.get("BOOM_ENV", "production").lower()
    if env not in API_URLS:
        raise ConfigurationError(
            f"Invalid BOOM_ENV value: {env}. Must be one of {list(API_URLS)}."
        )
    return env


def get_base_url() -> str:
    """Resolve the API base URL.

    ``BOOM_API_URL`` wins if set, otherwise the URL is picked from
    ``BOOM_ENV`` ("local", "backup" or "production", the default).
    """
    url = os.environ.get("BOOM_API_URL")
    return (url or API_URLS[_get_env()]).rstrip("/")


@dataclass
class BoomConfig:
    """Credentials and endpoint for a :class:`~boom.client.BoomClient`."""

    username: str
    password: str
    url: str = ""
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR

    def __post_init__(self) -> None:
        self.url = (self.url or get_base_url()).rstrip("/")

    @classmethod
    def from_env(
        cls,
        username: str | None = None,
        password: str | None = None,
        url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    ) -> BoomConfig:
        """Build a config, falling back to environment variables.

        Parameters
        ----------
        username : str | None
            BOOM account username. Falls back to ``BOOM_API_USER``.
        password : str | None
            BOOM account password. Falls back to ``BOOM_API_PASSWORD``.
        url : str | None
            API base URL. Falls back to :func:`get_base_url`.
        timeout : float
            Per-request timeout in seconds.
        max_retries : int
            How many times to retry a request that failed on a transport
            error or a 5xx.
        backoff_factor : float
            Base delay in seconds for the exponential backoff.

        Raises
        ------
        ConfigurationError
            If no username or no password could be resolved.
        """
        final_username = username or os.environ.get("BOOM_API_USER")
        final_password = password or os.environ.get("BOOM_API_PASSWORD")

        if not final_username:
            raise ConfigurationError(
                "Username is required. Pass it directly or set the "
                "BOOM_API_USER environment variable."
            )
        if not final_password:
            raise ConfigurationError(
                "Password is required. Pass it directly or set the "
                "BOOM_API_PASSWORD environment variable."
            )
        if final_password.startswith("bbml_"):
            raise ConfigurationError(
                "This is a Babamul API token, which the BOOM API rejects. "
                "Use your BOOM account password, or the babamul package "
                "if you only have a Babamul account."
            )

        return cls(
            username=final_username,
            password=final_password,
            url=url or get_base_url(),
            timeout=timeout,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
        )
