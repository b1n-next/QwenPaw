# -*- coding: utf-8 -*-
"""EP-2-11: runtime-side trace context tests.

Covers the pure-ASGI middleware (header -> ContextVar) and the audit
``extra`` tagging that makes one trace id replay across the hub audit
store and the runtime governance audit store.
"""

# Test touches AuditLog internals to verify the extra-payload tagging.
# pylint: disable=protected-access

from __future__ import annotations

import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from qwenpaw.app.trace_context import (
    TraceContextMiddleware,
    current_trace_id,
)
from qwenpaw.governance.audit import AuditLog


# ---------------------------------------------------------- middleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceContextMiddleware)

    @app.get("/probe")
    async def probe() -> dict[str, str | None]:
        return {"trace_id": current_trace_id()}

    return app


def test_middleware_sets_context_from_header() -> None:
    with TestClient(_app()) as client:
        response = client.get(
            "/probe",
            headers={"X-QwenPaw-Trace-Id": "a" * 32},
        )
        assert response.json() == {"trace_id": "a" * 32}


def test_middleware_no_header_yields_none() -> None:
    with TestClient(_app()) as client:
        assert client.get("/probe").json() == {"trace_id": None}


def test_middleware_resets_between_requests() -> None:
    with TestClient(_app()) as client:
        client.get("/probe", headers={"X-QwenPaw-Trace-Id": "b" * 32})
        # next request without the header must not see the previous value
        assert client.get("/probe").json() == {"trace_id": None}


def test_middleware_rejects_oversized_header_value() -> None:
    with TestClient(_app()) as client:
        response = client.get(
            "/probe",
            headers={"X-QwenPaw-Trace-Id": "c" * 65},
        )
        assert response.json() == {"trace_id": None}


# ------------------------------------------------------------- audit tag


class _Spec:
    """Minimal ToolCallSpec stand-in for AuditLog.record."""

    agent_id = "agent-x"
    session_id = "session-y"
    tool_name = "shell"
    target = "/tmp"


class _Decision:
    class action:  # noqa: N801 - mimics enum member access
        value = "allow"

    reason = "baseline"


def _audit_log(tmp_path):
    log = AuditLog.__new__(AuditLog)
    log.db_path = str(tmp_path / "audit.db")
    log._conn = sqlite3.connect(log.db_path)
    log._conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            ts INTEGER NOT NULL,
            workspace_dir TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            target TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            extra TEXT NOT NULL DEFAULT '{}'
        );
        """,
    )
    log._lock = __import__("threading").Lock()
    log._insert_count = 0
    return log


def test_audit_extra_empty_without_trace(tmp_path) -> None:
    import threading

    log = _audit_log(tmp_path)
    log._lock = threading.Lock()
    log.record("/ws", _Spec(), _Decision())  # type: ignore[arg-type]
    row = log._conn.execute(
        "SELECT extra FROM audit_events",
    ).fetchone()
    assert json.loads(row[0]) == {}


def test_audit_extra_carries_trace_in_context(tmp_path) -> None:
    import threading

    from qwenpaw.app.trace_context import trace_id_var

    log = _audit_log(tmp_path)
    log._lock = threading.Lock()
    token = trace_id_var.set("d" * 32)
    try:
        log.record("/ws", _Spec(), _Decision())  # type: ignore[arg-type]
    finally:
        trace_id_var.reset(token)
    row = log._conn.execute(
        "SELECT extra FROM audit_events",
    ).fetchone()
    assert json.loads(row[0]) == {"trace_id": "d" * 32}
