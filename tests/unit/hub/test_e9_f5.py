# -*- coding: utf-8 -*-
"""E9 catalog visibility + F5 W3C tracecontext (with F9 pinned elsewhere)."""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.trace import (
    sanitize_traceparent,
    trace_id_from_headers,
    trace_id_from_traceparent,
)


# ------------------------------------------------- F5 unit


def test_traceparent_shapes() -> None:
    good = "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
    assert sanitize_traceparent(good) == good
    assert sanitize_traceparent("garbage") is None
    assert sanitize_traceparent("00-ZZ-1234567890abcdef-01") is None
    assert (
        trace_id_from_traceparent(good) == "1234567890abcdef1234567890abcdef"
    )
    headers = {"traceparent": good}
    assert trace_id_from_headers(headers) == good.split("-")[1]
    # malformed traceparent must not leak into the trace id
    bad = {"traceparent": "nope"}
    assert len(trace_id_from_headers(bad)) == 32  # minted fresh


# ------------------------------------------------- F5 proxy


class _ProxyStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"ok": true}'


def test_proxy_forwards_valid_traceparent(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    async def proxy_handler(request: httpx.Request) -> httpx.Response:
        for name in ("traceparent", "x-qwenpaw-trace-id"):
            if name in request.headers:
                seen[name] = str(request.headers[name])
        return httpx.Response(
            200,
            stream=_ProxyStream(),
            headers={"Content-Type": "application/json"},
        )

    app = create_hub_app(
        root_dir=tmp_path,
        public_bind=False,
        proxy_transport=httpx.MockTransport(proxy_handler),
    )
    good = "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        response = client.get(
            "/api/config",
            headers={
                "Authorization": f"Bearer {token}",
                "traceparent": good,
            },
        )
        assert response.status_code == 200
        assert seen["traceparent"] == good
        assert len(seen["x-qwenpaw-trace-id"]) == 32

        # malformed inbound is dropped, hub trace id still flows
        seen.clear()
        client.get(
            "/api/config",
            headers={
                "Authorization": f"Bearer {token}",
                "traceparent": "not-a-traceparent",
            },
        )
        assert "traceparent" not in seen
        assert len(seen["x-qwenpaw-trace-id"]) == 32


# ------------------------------------------------- E9 catalog


def _admin_and_member(client: TestClient) -> tuple[str, str, str]:
    token = client.post(
        "/api/auth/register",
        json={"username": "owner", "password": "pw-123456"},
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/hub/admin/users",
        json={"username": "member", "password": "pw-123456"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    member_id = str(created.json()["user_id"])
    member_token = str(
        client.app.state.auth_service.authenticate(
            "member",
            "pw-123456",
        )[1],
    )
    return token, member_id, member_token  # type: ignore[return-value]


def test_catalog_visibility_follows_policies(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token, member_id, member_token = _admin_and_member(client)
        headers = {"Authorization": f"Bearer {token}"}
        client.post(
            "/api/hub/admin/models/providers",
            json={
                "provider_id": "p1",
                "name": "P1",
                "base_url": "https://example.invalid/v1",
                "models": ["m-open", "m-vip"],
                "api_key": "sk-x",
            },
            headers=headers,
        )
        # member sees the whole enabled catalog by default
        default = client.get(
            "/api/hub/models",
            headers={"Authorization": f"Bearer {member_token}"},
        ).json()
        names = {row["model"] for row in default["models"]}
        assert names == {"m-open", "m-vip"}
        # group policy hides the vip model from finance
        group_id = client.post(
            "/api/hub/admin/groups",
            json={"name": "finance"},
            headers=headers,
        ).json()["group_id"]
        client.post(
            f"/api/hub/admin/groups/{group_id}/members",
            json={"user_id": member_id},
            headers=headers,
        )
        client.post(
            "/api/hub/admin/policies",
            json={
                "subject_kind": "group",
                "subject_value": "finance",
                "resource": "model:m-vip",
                "effect": "deny",
            },
            headers=headers,
        )
        filtered = client.get(
            "/api/hub/models",
            headers={"Authorization": f"Bearer {member_token}"},
        ).json()
        names = {row["model"] for row in filtered["models"]}
        assert names == {"m-open"}
        # admin (no membership) still sees everything
        admin_view = client.get(
            "/api/hub/models",
            headers=headers,
        ).json()
        assert {row["model"] for row in admin_view["models"]} == {
            "m-open",
            "m-vip",
        }
