# -*- coding: utf-8 -*-
"""D5 template review flow + C8 time-scoped delegation policies."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app

MANIFEST = {
    "name": "Helper",
    "description": "demo",
    "prompt": "help",
    "graph": {"nodes": [], "edges": []},
    "skills": [],
}


def _admin_and_member(client: TestClient) -> tuple[dict, dict]:
    admin_token = client.post(
        "/api/auth/register",
        json={"username": "owner", "password": "pw-123456"},
    ).json()["token"]
    created = client.post(
        "/api/hub/admin/users",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "username": "member",
            "password": "pw-123456",
            "role": "user",
        },
    )
    assert created.status_code == 201, created.text
    member_token = client.post(
        "/api/auth/login",
        json={"username": "member", "password": "pw-123456"},
    ).json()["token"]
    return (
        {"Authorization": f"Bearer {admin_token}"},
        {"Authorization": f"Bearer {member_token}"},
    )


# ── D5 ──────────────────────────────────────────────────────────────


def test_submission_review_publish_lifecycle(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        admin, member = _admin_and_member(client)
        # member submits
        submitted = client.post(
            "/api/hub/templates/submit",
            headers=member,
            json={"template_id": "t1", "manifest": MANIFEST},
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["template"]["status"] == "pending_review"
        # invisible in the hall
        hall = client.get("/api/hub/templates", headers=member).json()
        assert all(t["template_id"] != "t1" for t in hall["templates"])
        # double-submit blocked while open
        again = client.post(
            "/api/hub/templates/submit",
            headers=member,
            json={"template_id": "t1", "manifest": MANIFEST},
        )
        assert again.status_code == 409
        # admin queue sees it
        queue = client.get(
            "/api/hub/admin/templates/pending",
            headers=admin,
        ).json()["templates"]
        assert [t["template_id"] for t in queue] == ["t1"]
        # admin publishes -> visible
        published = client.patch(
            "/api/hub/admin/templates/t1",
            headers=admin,
            json={"status": "published"},
        )
        assert published.status_code == 200
        hall = client.get("/api/hub/templates", headers=member).json()
        assert any(t["template_id"] == "t1" for t in hall["templates"])
        # published immutable for member overwrite
        blocked = client.post(
            "/api/hub/templates/submit",
            headers=member,
            json={"template_id": "t1", "manifest": MANIFEST},
        )
        assert blocked.status_code == 409


def test_mine_lists_own_proposals(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        admin, member = _admin_and_member(client)
        client.post(
            "/api/hub/templates/submit",
            headers=member,
            json={"template_id": "mine-1", "manifest": MANIFEST},
        )
        mine = client.get("/api/hub/templates/mine", headers=member)
        assert mine.status_code == 200
        ids = [t["template_id"] for t in mine.json()["templates"]]
        assert ids == ["mine-1"]
        # other users see nothing
        empty = client.get("/api/hub/templates/mine", headers=admin)
        assert empty.json()["templates"] == []


def test_review_requires_admin(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        _, member = _admin_and_member(client)
        queue = client.get(
            "/api/hub/admin/templates/pending",
            headers=member,
        )
        assert queue.status_code in (401, 403)


# ── C8 ──────────────────────────────────────────────────────────────


def _create_policy(client, headers, **extra):
    body = {
        "subject_kind": "user",
        "subject_value": "u-delegate",
        "resource": "menu:analytics",
        "effect": "allow",
    }
    body.update(extra)
    return client.post(
        "/api/hub/admin/policies",
        headers=headers,
        json=body,
    )


def test_future_expiry_grants_now(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        admin, _ = _admin_and_member(client)
        later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        created = _create_policy(client, admin, expires_at=later)
        assert created.status_code == 200, created.text
        policies = client.get(
            "/api/hub/admin/policies",
            headers=admin,
        ).json()["policies"]
        row = next(p for p in policies if p["resource"] == "menu:analytics")
        assert row["expires_at"] == later
        store = app.state.group_store
        live = store.policies_for(
            user_id="u-delegate",
            groups=[],
            role="user",
        )
        assert any(p.resource == "menu:analytics" for p in live)


def test_expired_policy_stops_matching(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        admin, _ = _admin_and_member(client)
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        _create_policy(client, admin, expires_at=past)
        store = app.state.group_store
        live = store.policies_for(
            user_id="u-delegate",
            groups=[],
            role="user",
        )
        assert not any(p.resource == "menu:analytics" for p in live)
        # unparseable expiry never grants (fail-closed)
        _create_policy(client, admin, expires_at="not-a-date")
        live2 = store.policies_for(
            user_id="u-delegate",
            groups=[],
            role="user",
        )
        assert not any(p.resource == "menu:analytics" for p in live2)


def test_purge_removes_only_expired(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        admin, _ = _admin_and_member(client)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        _create_policy(
            client,
            admin,
            subject_value="u-old",
            resource="menu:old",
            expires_at=past,
        )
        _create_policy(
            client,
            admin,
            subject_value="u-new",
            resource="menu:new",
            expires_at=future,
        )
        purged = client.post(
            "/api/hub/admin/policies/purge-expired",
            headers=admin,
        )
        assert purged.status_code == 200
        assert purged.json()["removed"] == 1
        policies = client.get(
            "/api/hub/admin/policies",
            headers=admin,
        ).json()["policies"]
        assert all(p["resource"] != "menu:old" for p in policies)
        assert any(p["resource"] == "menu:new" for p in policies)
