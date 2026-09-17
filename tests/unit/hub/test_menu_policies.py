# -*- coding: utf-8 -*-
"""B5: per-tenant/group menu whitelisting via menu:/route: policies."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw.hub.acl.console_map import (
    effective_permissions,
    permissions_payload,
)
from qwenpaw.hub.acl.groups import Policy
from qwenpaw.hub.control_app import create_hub_app


def _policy(subject: str, resource: str, effect: str) -> Policy:
    return Policy(
        policy_id=subject.replace(":", "") + resource.replace(":", ""),
        subject=subject,
        resource=resource,
        effect=effect,
    )


# ----------------------------------------------------- unit


def test_no_policies_equals_static_table() -> None:
    assert effective_permissions("user") == permissions_payload("user")


def test_menu_deny_adds_group() -> None:
    payload = effective_permissions(
        "user",
        (_policy("group:finance", "menu:control", "deny"),),
    )
    assert "control" in payload["denied_groups"]


def test_route_allow_releases_static_denial() -> None:
    payload = effective_permissions(
        "user",
        (_policy("user:u1", "route:core.models", "allow"),),
    )
    assert "core.models" not in payload["denied_routes"]
    # other static denials untouched
    assert "core.debug" in payload["denied_routes"]


def test_deny_beats_allow_at_equal_target() -> None:
    payload = effective_permissions(
        "user",
        (
            _policy("user:u1", "route:core.models", "allow"),
            _policy("group:locked", "route:core.models", "deny"),
        ),
    )
    assert "core.models" in payload["denied_routes"]


def test_non_menu_resources_ignored() -> None:
    payload = effective_permissions(
        "user",
        (_policy("user:u1", "apigroup:admin", "deny"),),
    )
    assert payload == permissions_payload("user")


def test_static_release_via_group_allow() -> None:
    payload = effective_permissions(
        "user",
        (_policy("group:power", "menu:settings", "allow"),),
    )
    assert "settings" not in payload["denied_groups"]


# ----------------------------------------------------- API


def _register_admin(client: TestClient) -> str:
    response = client.post(
        "/api/auth/register",
        json={"username": "owner", "password": "pw-123456"},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def test_endpoint_applies_group_route_policies(
    tmp_path: Path,
) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = _register_admin(client)
        headers = {"Authorization": f"Bearer {token}"}
        client.post(
            "/api/hub/admin/users",
            json={"username": "member", "password": "pw-123456"},
            headers=headers,
        )
        member = client.app.state.auth_service.authenticate(
            "member",
            "pw-123456",
        )
        group_id = client.post(
            "/api/hub/admin/groups",
            json={"name": "analysts"},
            headers=headers,
        ).json()["group_id"]
        client.post(
            f"/api/hub/admin/groups/{group_id}/members",
            json={"user_id": member[0].user_id},
            headers=headers,
        )
        client.post(
            "/api/hub/admin/policies",
            json={
                "subject_kind": "group",
                "subject_value": "analysts",
                "resource": "route:core.models",
                "effect": "allow",
            },
            headers=headers,
        )
        client.post(
            "/api/hub/admin/policies",
            json={
                "subject_kind": "group",
                "subject_value": "analysts",
                "resource": "menu:agent",
                "effect": "deny",
            },
            headers=headers,
        )
        payload = client.get(
            "/api/hub/me/permissions",
            headers={"Authorization": f"Bearer {member[1]}"},
        ).json()
        assert "core.models" not in payload["denied_routes"]
        assert "agent" in payload["denied_groups"]
        # admin is untouched by group policies
        admin_payload = client.get(
            "/api/hub/me/permissions",
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        assert "agent" not in admin_payload["denied_groups"]
