# -*- coding: utf-8 -*-
"""EP-2-3 integration: quota gate at the hub proxy (soft/hard paths)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.quota import UsageSnapshot


def _register(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "pw-123456"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return str(body["token"])


@pytest.fixture(name="client")
def _client(tmp_path: Path) -> TestClient:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as test_client:
        yield test_client
        app.state.quota._cache.clear()  # pylint: disable=protected-access


def _force_snapshot(app, user_id: str, snapshot: UsageSnapshot) -> None:
    """Bypass the usage store: pin the engine's cached snapshot."""
    # pylint: disable=protected-access
    engine = app.state.quota
    engine._cache[user_id] = (engine._clock(), snapshot)


def _reload_quota(app) -> None:
    # pylint: disable=protected-access
    app.state.quota._reload_if_due(force=True)


def test_quota_exceeded_detail_shape(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    (tmp_path / "quota.json").write_text(
        json.dumps({"default": {"daily_requests": 1}}),
        encoding="utf-8",
    )
    with TestClient(app) as client:
        _reload_quota(app)
        token = _register(client, "blocked")
        user_id = client.app.state.auth_service.authenticate(
            "blocked",
            "pw-123456",
        )[0].user_id
        _force_snapshot(app, user_id, UsageSnapshot(requests=5))
        response = client.get(
            "/api/console/ping",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["code"] == "QUOTA_EXCEEDED"
        assert detail["dimension"] == "daily_requests"
        assert detail["used"] == 5 and detail["limit"] == 1


def test_soft_threshold_still_forwards(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    (tmp_path / "quota.json").write_text(
        json.dumps({"default": {"daily_requests": 100}}),
        encoding="utf-8",
    )
    with TestClient(app) as client:
        _reload_quota(app)
        token = _register(client, "warned")
        user_id = client.app.state.auth_service.authenticate(
            "warned",
            "pw-123456",
        )[0].user_id
        _force_snapshot(app, user_id, UsageSnapshot(requests=85))
        response = client.get(
            "/api/console/ping",
            headers={"Authorization": f"Bearer {token}"},
        )
        # request forwarded (upstream answered — 200 from the mock
        # transport or a 503 when no runtime); never 403 QUOTA
        assert response.status_code != 403 or (
            response.json()["detail"].get("code") != "QUOTA_EXCEEDED"
        )


def test_admin_quota_endpoint(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    (tmp_path / "quota.json").write_text(
        json.dumps({"default": {"daily_tokens": 1000}}),
        encoding="utf-8",
    )
    with TestClient(app) as client:
        _reload_quota(app)
        admin = _register(client, "owner")
        client.post(
            "/api/hub/admin/users",
            json={"username": "member1", "password": "pw-123456"},
            headers={"Authorization": f"Bearer {admin}"},
        )
        response = client.get(
            "/api/hub/admin/quota",
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert response.status_code == 200
        rows = response.json()["quota"]
        assert {row["user_id"] for row in rows}
        by_dim = rows[0]["dimensions"]
        assert {d["dimension"] for d in by_dim} == {
            "daily_tokens",
            "daily_requests",
        }
