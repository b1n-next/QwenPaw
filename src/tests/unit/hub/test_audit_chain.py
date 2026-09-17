# -*- coding: utf-8 -*-
"""H2: audit hash chain — append-only tamper evidence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.database import initialize_hub_database
from qwenpaw.hub.operations import HubOperationsStore


def _store(tmp_path: Path) -> HubOperationsStore:
    return HubOperationsStore(tmp_path / "control.db", tmp_path)


def _events(store: HubOperationsStore, count: int) -> None:
    for i in range(count):
        store.record(
            actor_user_id="u1",
            actor_username="u1",
            action="test",
            resource_type="x",
            resource_id=str(i),
            detail={"i": i},
        )


def test_chain_builds_and_verifies(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _events(store, 4)
    result = store.verify_chain()
    assert result["valid"] is True
    assert result["checked"] == 4
    assert len(result["head_hash"]) == 64


def test_tampered_row_detected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _events(store, 3)
    with sqlite3.connect(tmp_path / "control.db") as connection:
        connection.execute(
            "UPDATE hub_audit_events SET detail_json = '{\"i\": 999}' "
            "WHERE resource_id = '1'",
        )
    result = store.verify_chain()
    assert result["valid"] is False
    assert result["reason"] == "row-hash-mismatch"
    with sqlite3.connect(tmp_path / "control.db") as connection:
        bad = connection.execute(
            "SELECT event_id FROM hub_audit_events WHERE resource_id = '1'",
        ).fetchone()
    assert result["at_event_id"] == bad[0]


def test_deleted_row_breaks_link(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _events(store, 3)
    with sqlite3.connect(tmp_path / "control.db") as connection:
        connection.execute(
            "DELETE FROM hub_audit_events WHERE resource_id = '1'",
        )
    result = store.verify_chain()
    assert result["valid"] is False
    assert result["reason"] == "broken-link"


def test_legacy_rows_backfill_on_init(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    initialize_hub_database(database)
    # simulate a pre-chain store: rows without hashes
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hub_audit_events(event_id, actor_user_id, "
            "actor_username, action, resource_type, resource_id, "
            "outcome, detail_json, created_at) "
            "VALUES ('e1','u','u','legacy','x','1','ok','{}','2026-01-01')",
        )
        connection.execute(
            "INSERT INTO hub_audit_events(event_id, actor_user_id, "
            "actor_username, action, resource_type, resource_id, "
            "outcome, detail_json, created_at) "
            "VALUES ('e2','u','u','legacy','x','2','ok','{}','2026-01-02')",
        )
    # re-initialize (idempotent) triggers the backfill
    initialize_hub_database(database)
    store = HubOperationsStore(database, tmp_path)
    result = store.verify_chain()
    assert result["valid"] is True
    assert result["checked"] == 2


def test_chain_head_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.chain_head() == {"rows": 0, "head_hash": None}
    _events(store, 2)
    head = store.chain_head()
    assert head["rows"] == 2
    assert head["head_hash"] == store.verify_chain()["head_hash"]
    assert head["head_event_id"]


def test_endpoints_admin_only_and_reporting(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        admin = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {admin}"}
        verify = client.get(
            "/api/hub/admin/audit/verify",
            headers=headers,
        )
        assert verify.status_code == 200
        assert verify.json()["valid"] is True
        head = client.get(
            "/api/hub/admin/audit/chain-head",
            headers=headers,
        )
        assert head.status_code == 200
        assert head.json()["rows"] >= 1
        client.post(
            "/api/hub/admin/users",
            json={"username": "member", "password": "pw-123456"},
            headers=headers,
        )
        member = client.app.state.auth_service.authenticate(
            "member",
            "pw-123456",
        )[1]
        denied = client.get(
            "/api/hub/admin/audit/verify",
            headers={"Authorization": f"Bearer {member}"},
        )
        assert denied.status_code == 403
