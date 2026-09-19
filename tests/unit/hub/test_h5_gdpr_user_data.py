# -*- coding: utf-8 -*-
"""H5: GDPR-style user data export / erasure."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app


def _admin(client: TestClient, username: str = "owner") -> str:
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": "pw-123456"},
    ).json()["token"]


def test_export_bundles_profile_groups_usage(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = _admin(client)
        headers = {"Authorization": f"Bearer {token}"}
        users = client.get("/api/hub/admin/users", headers=headers).json()
        victim = next(u for u in users["items"] if u["username"] == "owner")
        groups = app.state.group_store
        gid = groups.create_group("gdpr-team")
        groups.add_member(gid, victim["user_id"])
        app.state.usage_store.upsert_rows(
            "personal-" + victim["user_id"],
            [
                {
                    "date": "2026-09-19",
                    "provider_id": "p",
                    "model": "m",
                    "agent_id": "",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "call_count": 1,
                },
            ],
        )
        response = client.get(
            f"/api/hub/admin/users/{victim['user_id']}/export",
            headers=headers,
        )
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        payload = json.loads(response.text)
        assert payload["user"]["username"] == "owner"
        assert "gdpr-team" in payload["groups"]
        assert payload["usage"]["total"]["call_count"] == 1
        assert any("append-only" in n for n in payload["notes"])


def test_export_unknown_user_404(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = _admin(client)
        missing = client.get(
            "/api/hub/admin/users/nobody/export",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert missing.status_code == 404


def test_erase_anonymizes_and_strips(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = _admin(client)
        headers = {"Authorization": f"Bearer {token}"}
        created = client.post(
            "/api/hub/admin/users",
            headers=headers,
            json={
                "username": "victim",
                "password": "pw-123456",
                "role": "user",
            },
        )
        assert created.status_code == 201, created.text
        user_id = created.json()["user_id"]
        groups = app.state.group_store
        gid = groups.create_group("doomed")
        groups.add_member(gid, user_id)
        vault = app.state.credential_vault
        vault.put(
            tenant_id=f"personal-{user_id}",
            scope="app",
            name="SOME_TOKEN",
            value="secret-value",
        )
        erased = client.delete(
            f"/api/hub/admin/users/{user_id}/data",
            headers=headers,
        )
        assert erased.status_code == 200, erased.text
        body = erased.json()
        assert body["erased"] is True
        assert body["groups_removed"] == 1
        assert body["credentials_deleted"] == 1
        # user gone from live listings
        users = client.get(
            "/api/hub/admin/users",
            headers=headers,
        ).json()["items"]
        assert all(u["user_id"] != user_id for u in users)
        assert groups.group_names_for(user_id) == ()
        assert (
            vault.list_metadata(
                tenant_id=f"personal-{user_id}",
            )
            == []
        )
        # audit kept the trail
        audit = client.get(
            "/api/hub/admin/audit",
            params={"action": "user.data_erased"},
            headers=headers,
        )
        assert any(
            e.get("resource_id") == user_id
            for e in audit.json().get("items", [])
        )


def test_erase_self_forbidden_and_double_erase_conflict(
    tmp_path: Path,
) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = _admin(client)
        headers = {"Authorization": f"Bearer {token}"}
        me = client.get("/api/hub/me", headers=headers).json()
        self_erase = client.delete(
            f"/api/hub/admin/users/{me['user_id']}/data",
            headers=headers,
        )
        assert self_erase.status_code == 422
        # unknown user
        assert (
            client.delete(
                "/api/hub/admin/users/ghost/data",
                headers=headers,
            ).status_code
            == 404
        )


def test_export_requires_admin(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        anonymous = client.get("/api/hub/admin/users/x/export")
        assert anonymous.status_code == 401
