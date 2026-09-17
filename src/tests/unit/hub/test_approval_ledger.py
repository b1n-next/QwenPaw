# -*- coding: utf-8 -*-
"""EP-2-12: hub-side approval resolution ledger tests.

Proxied approve/deny requests are mirrored into the hub audit ledger
with the cross-plane trace id (EP-2-11) attached.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from httpx import MockTransport

from qwenpaw.hub.trace import TRACE_HEADER, new_trace_id

from tests.unit.hub.test_control_app import (
    _ProxyStream,
    _client,
    _create_user,
    _headers,
    _register,
)


def _ok_transport() -> MockTransport:
    return MockTransport(
        lambda request: httpx.Response(200, stream=_ProxyStream()),
    )


def _approve(client, token: str, path: str, body: dict) -> httpx.Response:
    return client.post(path, headers=_headers(token), json=body)


def test_approve_mirrored_to_audit_ledger(tmp_path: Path) -> None:
    with _client(tmp_path, _ok_transport()) as client:
        _register(client, "owner")
        _, member_token = _create_user(client, "member")

        tid = new_trace_id()
        response = client.post(
            "/api/approval/approve",
            headers={**_headers(member_token), TRACE_HEADER: tid},
            json={"request_id": "req-42", "session_id": "s1"},
        )
        assert response.status_code == 200

        events, total = client.app.state.operations.list_events(
            page=1,
            page_size=10,
            action="approval.resolved",
        )
        assert total == 1
        event = events[0]
        assert event["resource_id"] == "req-42"
        assert event["trace_id"] == tid
        assert event["detail"]["action"] == "approve"
        assert event["detail"]["upstream_status"] == 200


def test_deny_mirrored_with_action_and_reason(tmp_path: Path) -> None:
    with _client(tmp_path, _ok_transport()) as client:
        _register(client, "owner")
        _, member_token = _create_user(client, "member")

        response = client.post(
            "/api/approval/deny",
            headers=_headers(member_token),
            json={
                "request_id": "req-43",
                "session_id": "s1",
                "reason": "not allowed",
            },
        )
        assert response.status_code == 200

        events, total = client.app.state.operations.list_events(
            page=1,
            page_size=10,
            action="approval.resolved",
        )
        assert total == 1
        assert events[0]["detail"]["action"] == "deny"


def test_unrelated_post_not_mirrored(tmp_path: Path) -> None:
    with _client(tmp_path, _ok_transport()) as client:
        _register(client, "owner")
        _, member_token = _create_user(client, "member")

        client.post(
            "/api/console/chat",
            headers=_headers(member_token),
            json={},
        )
        _, total = client.app.state.operations.list_events(
            page=1,
            page_size=10,
            action="approval.resolved",
        )
        assert total == 0


def test_approval_list_not_mirrored(tmp_path: Path) -> None:
    with _client(tmp_path, _ok_transport()) as client:
        _register(client, "owner")
        _, member_token = _create_user(client, "member")

        client.get("/api/approval/list", headers=_headers(member_token))
        _, total = client.app.state.operations.list_events(
            page=1,
            page_size=10,
            action="approval.resolved",
        )
        assert total == 0
