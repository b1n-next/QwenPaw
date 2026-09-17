# -*- coding: utf-8 -*-
"""EP-2-11: cross-plane trace id propagation tests (hub side)."""

from __future__ import annotations

from pathlib import Path

import httpx
from httpx import MockTransport

from qwenpaw.hub.trace import (
    TRACE_HEADER,
    new_trace_id,
    sanitize_trace_id,
    trace_id_from_headers,
)

from tests.unit.hub.test_control_app import (
    _ProxyStream,
    _client,
    _create_user,
    _headers,
    _register,
)


# ---------------------------------------------------------------- helpers


def _echo_transport(captured: dict) -> MockTransport:
    """Echo transport recording the upstream request headers."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured["trace"] = request.headers.get(TRACE_HEADER)
        return httpx.Response(200, stream=_ProxyStream())

    return MockTransport(handler)


# ------------------------------------------------------------ trace module


def test_trace_module_basics() -> None:
    tid = new_trace_id()
    assert len(tid) == 32
    assert sanitize_trace_id(tid) == tid
    assert sanitize_trace_id(None) is None
    assert sanitize_trace_id("") is None
    assert sanitize_trace_id("not-a-trace-id") is None
    assert sanitize_trace_id(f" {tid.upper()} ") == tid


def test_trace_id_from_headers_mints_and_accepts() -> None:
    minted = trace_id_from_headers({})
    assert len(minted) == 32

    tid = new_trace_id()
    headers = httpx.Headers({TRACE_HEADER: tid})
    assert trace_id_from_headers(headers) == tid

    # malformed inbound value is replaced, not trusted
    headers_bad = httpx.Headers({TRACE_HEADER: "garbage"})
    assert trace_id_from_headers(headers_bad) != "garbage"


# --------------------------------------------------------- proxy behavior


def test_proxy_mints_trace_and_echoes_on_response(tmp_path: Path) -> None:
    captured: dict = {}
    with _client(tmp_path, _echo_transport(captured)) as client:
        _register(client, "owner")
        _, member_token = _create_user(client, "member")

        response = client.get(
            "/api/agents",
            headers=_headers(member_token),
        )

        assert response.status_code == 200
        echoed = response.headers.get(TRACE_HEADER)
        assert echoed is not None
        assert sanitize_trace_id(echoed) == echoed
        assert captured["trace"] == echoed


def test_proxy_honors_wellformed_inbound_trace(tmp_path: Path) -> None:
    captured: dict = {}
    with _client(tmp_path, _echo_transport(captured)) as client:
        _register(client, "owner")
        _, member_token = _create_user(client, "member")

        tid = new_trace_id()
        response = client.get(
            "/api/agents",
            headers={**_headers(member_token), TRACE_HEADER: tid},
        )

        assert response.status_code == 200
        assert response.headers.get(TRACE_HEADER) == tid
        assert captured["trace"] == tid


def test_proxy_replaces_malformed_inbound_trace(tmp_path: Path) -> None:
    captured: dict = {}
    with _client(tmp_path, _echo_transport(captured)) as client:
        _register(client, "owner")
        _, member_token = _create_user(client, "member")

        response = client.get(
            "/api/agents",
            headers={**_headers(member_token), TRACE_HEADER: "junk"},
        )

        assert response.status_code == 200
        assert sanitize_trace_id(response.headers.get(TRACE_HEADER, ""))


# ------------------------------------------------------------ audit plane


def test_acl_denied_audit_carries_trace_id(tmp_path: Path) -> None:
    with _client(tmp_path, _ok_transport_unused()) as client:
        _register(client, "owner")
        _, member_token = _create_user(client, "member")

        tid = new_trace_id()
        response = client.get(
            "/api/config",
            headers={**_headers(member_token), TRACE_HEADER: tid},
        )
        assert response.status_code == 403

        events, total = client.app.state.operations.list_events(
            page=1,
            page_size=10,
            action="acl.denied",
        )
        assert total == 1
        assert events[0]["trace_id"] == tid

        # trace filter replays exactly that row
        replay, replay_total = client.app.state.operations.list_events(
            page=1,
            page_size=10,
            trace_id=tid,
        )
        assert replay_total == 1
        assert replay[0]["event_id"] == events[0]["event_id"]


def test_admin_audit_endpoint_filters_by_trace(tmp_path: Path) -> None:
    with _client(tmp_path, _ok_transport_unused()) as client:
        admin_token = _register(client, "owner")
        _, member_token = _create_user(client, "member")

        tid = new_trace_id()
        client.get(
            "/api/config",
            headers={**_headers(member_token), TRACE_HEADER: tid},
        )

        payload = client.get(
            "/api/hub/admin/audit",
            params={"trace_id": tid},
            headers=_headers(admin_token),
        ).json()
        assert payload["total"] == 1
        assert payload["items"][0]["trace_id"] == tid

        empty = client.get(
            "/api/hub/admin/audit",
            params={"trace_id": new_trace_id()},
            headers=_headers(admin_token),
        ).json()
        assert empty["total"] == 0


# ------------------------------------------------------- schema migration


def test_v1_database_migrates_trace_column(tmp_path: Path) -> None:
    """A hub-v1 control.db upgrades in place with the trace column."""
    from qwenpaw.hub.database import (
        _SCHEMA_SQL,
        connect_hub_database,
        initialize_hub_database,
    )

    legacy = tmp_path / "control.db"
    with connect_hub_database(legacy) as connection:
        connection.executescript(_SCHEMA_SQL)
        # a real v1 store always carries its generation row
        connection.execute(
            "INSERT INTO hub_schema(key, value) VALUES "
            "('schema_generation', 'hub-v1')",
        )
        # simulate v1: rebuild the audit table without trace_id
        connection.execute("DROP TABLE hub_audit_events")
        connection.execute(
            """
            CREATE TABLE hub_audit_events (
                event_id TEXT PRIMARY KEY,
                actor_user_id TEXT NOT NULL,
                actor_username TEXT NOT NULL,
                action TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                request_id TEXT,
                correlation_id TEXT,
                remote_address TEXT,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
        )
        connection.execute(
            "UPDATE hub_schema SET value = 'hub-v1' "
            "WHERE key = 'schema_generation'",
        )

    initialize_hub_database(legacy)  # must not raise

    with connect_hub_database(legacy) as connection:
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(hub_audit_events)",
            ).fetchall()
        }
        assert "trace_id" in columns
        generation = connection.execute(
            "SELECT value FROM hub_schema WHERE key = 'schema_generation'",
        ).fetchone()
        assert str(generation["value"]) == "hub-v2"


def test_record_persists_trace_id(tmp_path: Path) -> None:
    from qwenpaw.hub.operations import HubOperationsStore

    store = HubOperationsStore(
        tmp_path / "control.db",
        tmp_path,
    )
    tid = new_trace_id()
    store.record(
        actor_user_id="u1",
        actor_username="owner",
        action="acl.denied",
        resource_type="api",
        resource_id="/api/config",
        trace_id=tid,
    )
    store.record(
        actor_user_id="u1",
        actor_username="owner",
        action="runtime.start",
        resource_type="runtime",
        resource_id="r1",
    )
    traced, total = store.list_events(page=1, page_size=10, trace_id=tid)
    assert total == 1
    assert traced[0]["trace_id"] == tid


def _ok_transport_unused() -> MockTransport:
    return MockTransport(
        lambda request: httpx.Response(200, stream=_ProxyStream()),
    )
