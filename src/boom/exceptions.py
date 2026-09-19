"""Exceptions raised by the BOOM client."""

from __future__ import annotations


class BoomError(Exception):
    """Base class for every error raised by this package."""


class ConfigurationError(BoomError):
    """Missing or invalid configuration (credentials, URL, cache dir)."""


class APIError(BoomError):
    """The API returned an unexpected response."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class APIAuthenticationError(APIError):
    """Credentials are missing, wrong, or the token was rejected."""


class APINotFoundError(APIError):
    """The requested resource does not exist.

    The API answers 404 both for a catalog that does not exist and for one
    the account is not allowed to read, so this is also what an access
    denial looks like.
    """


class QueryError(APIError):
    """The server rejected the query itself (filter, projection, pipeline)."""
