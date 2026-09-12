# -*- coding: utf-8 -*-
"""Integration tests for hub proxy ACL enforcement (EP-0-6).

Exercises the wired control_app: HTTP 403 with ACL_DENIED detail +
audit events, admin bypass, the /api/hub/me/permissions endpoint,
websocket close 1008, and the acl.json overlay via env override.
"""

import json
from pathlib import Path

import httpx
import pytest
from httpx import MockTransport
from starlette.websockets import WebSocketDisconnect

from tests.unit.hub.test_control_app import (
    _ProxyStream,
    _client,
    _create_user,
    _headers,
    _register,
)


def _ok_transport() -> MockTransport:
    # A fresh streaming Response per request — mirrors the upstream
    # fixtures (the proxy consumes the upstream body as a stream).
    return MockTransport(
        lambda request: httpx.Response(200, stream=_ProxyStream()),
    )


def test_member_gets_403_with_acl_detail_and_audit(tmp_path: Path) -> None:
    with _client(tmp_path, _ok_transport()) as client:
        _register(client, "owner")
        member, member_token = _create_user(client, "member")

        response = client.get("/api/config", headers=_headers(member_token))

        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["code"] == "ACL_DENIED"
        assert detail["reason"] == "default-deny"

        events, total = client.app.state.operations.list_events(
            page=1,
            page_size=10,
            action="acl.denied",
        )
        assert total == 1
        assert events[0]["actor_user_id"] == member.user_id
        assert events[0]["resource_id"] == "/api/config"


def test_member_chat_plane_passes_and_admin_bypasses(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, _ok_transport()) as client:
        admin_token = _register(client, "owner")
        _, member_token = _create_user(client, "member")

        # chat plane
        assert (
            client.post(
                "/api/console/chat",
                headers=_headers(member_token),
                json={},
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/api/agents",
                headers=_headers(member_token),
            ).status_code
            == 200
        )
        # admin plane for admin role is untouched
        assert (
            client.get(
                "/api/config",
                headers=_headers(admin_token),
            ).status_code
            == 200
        )


def test_permissions_endpoint_per_role(tmp_path: Path) -> None:
    with _client(tmp_path, _ok_transport()) as client:
        admin_token = _register(client, "owner")
        _, member_token = _create_user(client, "member")

        member_payload = client.get(
            "/api/hub/me/permissions",
            headers=_headers(member_token),
        ).json()
        assert member_payload["role"] == "user"
        assert "core.import" in member_payload["denied_routes"]
        assert "core.channels" in member_payload["denied_routes"]
        assert "settings" in member_payload["denied_groups"]

        admin_payload = client.get(
            "/api/hub/me/permissions",
            headers=_headers(admin_token),
        ).json()
        assert admin_payload == {
            "role": "admin",
            "denied_groups": [],
            "denied_routes": [],
        }


def test_websocket_denied_for_user_closes_1008(tmp_path: Path) -> None:
    with _client(tmp_path, _ok_transport()) as client:
        _register(client, "owner")
        _, member_token = _create_user(client, "member")

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(
                "/api/voice/ws",
                headers={"Authorization": f"Bearer {member_token}"},
            ):
                pass  # pragma: no cover - server must close first
        assert excinfo.value.code == 1008


def test_acl_json_overlay_via_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "acl.json"
    config.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "no-market",
                        "effect": "deny",
                        "pattern": "^/api/market",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("QWENPAW_HUB_ACL_CONFIG", str(config))

    with _client(tmp_path, _ok_transport()) as client:
        _register(client, "owner")
        _, member_token = _create_user(client, "member")
        admin_token = client.app.state.auth_service.authenticate(
            "owner",
            "safe-password",
        )[1]

        assert (
            client.get(
                "/api/market/providers",
                headers=_headers(member_token),
            ).status_code
            == 403
        )
        # admin is unaffected by user-role overlays
        assert (
            client.get(
                "/api/market/providers",
                headers=_headers(admin_token),
            ).status_code
            == 200
        )
