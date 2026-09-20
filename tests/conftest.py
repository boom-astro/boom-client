"""Offline test harness: a fake BOOM API served over httpx.MockTransport."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from boom.client import BoomClient

from .fixtures import CATALOGS


class FakeApi:
    """Records requests and replies with whatever the test registered."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Any] = {}
        self.auth_calls = 0
        self.token = "token-1"

    def route(
        self,
        path: str,
        payload: Any = None,
        *,
        status: int = 200,
        message: str = "success",
        envelope: bool = True,
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> None:
        self.routes[path] = {
            "payload": payload,
            "status": status,
            "message": message,
            "envelope": envelope,
            "handler": handler,
        }

    def body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/auth":
            self.auth_calls += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"{self.token}-{self.auth_calls}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )

        route = self.routes.get(path)
        if route is None:
            return httpx.Response(404, json={"message": f"no route {path}"})
        if route["handler"] is not None:
            return route["handler"](request)

        status = route["status"]
        if status >= 400:
            return httpx.Response(status, json={"message": route["message"]})
        if route["envelope"]:
            return httpx.Response(
                status,
                json={"message": route["message"], "data": route["payload"]},
            )
        return httpx.Response(status, json=route["payload"])


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def boom(api: FakeApi) -> FakeApi:
    """A fake API serving the catalogs from :mod:`tests.fixtures`."""

    def find(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        documents = CATALOGS.get(body["catalog_name"], [])
        documents = [
            document
            for document in documents
            if _matches(document, body.get("filter") or {})
        ]
        limit = body.get("limit")
        return httpx.Response(
            200,
            json={
                "message": "ok",
                "data": documents[:limit] if limit else documents,
            },
        )

    api.route("/queries/find", handler=find)
    return api


def _matches(document: Any, filter: dict[str, Any]) -> bool:
    """Support the handful of filter shapes the package actually sends."""
    for key, expected in filter.items():
        if key.startswith("$") or key == "coordinates.radec_geojson":
            continue
        actual = document.get(key)
        if isinstance(expected, dict):
            if "$eq" in expected and actual != expected["$eq"]:
                return False
        elif actual != expected:
            return False
    return True


@pytest.fixture
def client(api: FakeApi) -> BoomClient:
    return BoomClient(
        username="tester",
        password="secret",
        url="https://boom.test",
        transport=api.transport,
        backoff_factor=0.0,
    )
