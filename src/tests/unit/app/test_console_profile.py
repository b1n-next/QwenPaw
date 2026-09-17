# -*- coding: utf-8 -*-
"""EP-2-9 (B6): restricted console profile for direct-runtime use."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qwenpaw.app.routers.console_profile import router


@pytest.fixture(name="app")
def _app() -> FastAPI:
    application = FastAPI()
    application.include_router(router)
    return application


def test_unset_profile_keeps_upstream_404(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QWENPAW_CONSOLE_PROFILE", raising=False)
    response = TestClient(app).get("/api/console/profile")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PROFILE_NOT_CONFIGURED"


def test_full_profile_is_also_404(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_CONSOLE_PROFILE", "full")
    assert TestClient(app).get("/api/console/profile").status_code == 404


def test_restricted_serves_user_role_payload(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_CONSOLE_PROFILE", "RESTRICTED")
    response = TestClient(app).get("/api/console/profile")
    assert response.status_code == 200
    payload = response.json()
    assert payload["profile"] == "restricted"
    assert payload["role"] == "user"
    assert payload["model_readonly"] is True
    assert isinstance(payload["denied_routes"], list)
    assert payload["denied_routes"]  # user role denies something


def test_payload_shape_matches_hub_contract(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console store reads the same keys as /hub/me/permissions."""
    monkeypatch.setenv("QWENPAW_CONSOLE_PROFILE", "restricted")
    payload = TestClient(app).get("/api/console/profile").json()
    for key in (
        "role",
        "denied_groups",
        "denied_routes",
        "model_readonly",
    ):
        assert key in payload
    assert response_cache_headers(app)


def response_cache_headers(app: FastAPI) -> bool:
    with TestClient(app) as client:
        headers = client.get("/api/console/profile").headers
    return headers.get("cache-control") == "no-store"
