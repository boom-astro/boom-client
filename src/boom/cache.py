"""On-disk cache for data pulled from the BOOM API.

The cache is opt-in: it does nothing unless a directory is configured,
through ``BOOM_CACHE_DIR`` or the *directory* argument. Entries expire,
because alert catalogs keep growing and a cached object that never goes
stale would quietly hide new photometry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import pandas as pd

from .exceptions import ConfigurationError

#: How long an entry stays usable, in seconds. One day, so a re-run the
#: next morning picks up the night's alerts.
DEFAULT_MAX_AGE = 86_400

_SAFE_KEY = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def _slug(key: str) -> str:
    """Turn an arbitrary key into a safe, collision-free file name."""
    if _SAFE_KEY.match(key):
        return key
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", key)[:60]
    return f"{stem}-{digest}"


class Cache:
    """A directory of cached payloads, keyed by namespace and key.

    Parameters
    ----------
    directory : str | Path | None
        Where to store entries. ``None`` disables the cache.
    max_age : float | None
        Seconds before an entry is considered stale. ``None`` or ``0``
        keeps entries forever.

    Examples
    --------
    >>> cache = Cache("/tmp/boom-cache")
    >>> cache.set_json("objects", "ZTF18abcdefg", {"objectId": "ZTF18abcdefg"})
    >>> cache.get_json("objects", "ZTF18abcdefg")["objectId"]
    'ZTF18abcdefg'
    """

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        max_age: float | None = DEFAULT_MAX_AGE,
    ) -> None:
        self.directory = Path(directory).expanduser() if directory else None
        self.max_age = max_age

    @classmethod
    def from_env(cls, max_age: float | None = None) -> Cache:
        """Build a cache from ``BOOM_CACHE_DIR`` and ``BOOM_CACHE_MAX_AGE``.

        Returns a disabled cache when ``BOOM_CACHE_DIR`` is unset.
        """
        directory = os.environ.get("BOOM_CACHE_DIR")
        if max_age is None:
            raw = os.environ.get("BOOM_CACHE_MAX_AGE")
            try:
                max_age = float(raw) if raw else DEFAULT_MAX_AGE
            except ValueError as error:
                raise ConfigurationError(
                    f"BOOM_CACHE_MAX_AGE must be a number, got {raw!r}"
                ) from error
        return cls(directory, max_age=max_age)

    @property
    def enabled(self) -> bool:
        return self.directory is not None

    def __repr__(self) -> str:
        if not self.enabled:
            return "Cache(disabled)"
        return f"Cache({str(self.directory)!r}, max_age={self.max_age})"

    def path(self, namespace: str, key: str, suffix: str) -> Path | None:
        """Resolve the file backing an entry, or ``None`` when disabled."""
        if self.directory is None:
            return None
        return self.directory / _slug(namespace) / f"{_slug(key)}{suffix}"

    def _fresh(self, path: Path | None) -> Path | None:
        if path is None or not path.exists():
            return None
        if self.max_age and time.time() - path.stat().st_mtime > self.max_age:
            return None
        return path

    def get_json(self, namespace: str, key: str) -> Any | None:
        """Read a JSON entry, or ``None`` if absent, stale or unreadable."""
        path = self._fresh(self.path(namespace, key, ".json"))
        if path is None:
            return None
        try:
            with path.open() as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None

    def set_json(self, namespace: str, key: str, value: Any) -> None:
        """Write a JSON entry. A no-op when the cache is disabled."""
        path = self.path(namespace, key, ".json")
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, lambda tmp: tmp.write_text(json.dumps(value)))

    def get_frame(self, namespace: str, key: str) -> pd.DataFrame | None:
        """Read a Parquet entry, or ``None`` if absent, stale or unreadable."""
        path = self._fresh(self.path(namespace, key, ".parquet"))
        if path is None:
            return None
        try:
            return pd.read_parquet(path)
        except (OSError, ValueError):
            return None

    def set_frame(self, namespace: str, key: str, frame: pd.DataFrame) -> None:
        """Write a DataFrame as Parquet. A no-op when the cache is disabled."""
        path = self.path(namespace, key, ".parquet")
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, lambda tmp: frame.to_parquet(tmp, index=False))

    def clear(self, namespace: str | None = None) -> None:
        """Delete a namespace, or the whole cache when *namespace* is None."""
        if self.directory is None:
            return
        target = (
            self.directory
            if namespace is None
            else self.directory / _slug(namespace)
        )
        if target.exists():
            shutil.rmtree(target)


def _atomic_write(path: Path, write: Any) -> None:
    """Write through a temporary file so a crash leaves no half entry."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        write(tmp)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


__all__ = ["Cache", "DEFAULT_MAX_AGE"]
