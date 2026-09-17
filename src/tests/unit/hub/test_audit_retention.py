# -*- coding: utf-8 -*-
"""H3: audit retention — export, archive-then-prune, chain survival."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.operations import HubOperationsStore


def _store(tmp_path: Path) -> HubOperationsStore:
    return HubOperationsStore(tmp_path / "control.db", tmp_path)


def _events(store: HubOperationsStore, stamps: list[str]) -> None:
    for action, stamp in enumerate(stamps):
        store.record(
            actor_user_id="u1",
            actor_username="u1",
            action=f"act-{action}",
            resource_type="x",
            resource_id=str(action),
            detail={"stamp": stamp},
        )


def test_export_contains_hash_columns(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _events(store, ["a", "b"])
    rows = list(store.iter_events())
    assert len(rows) == 2
    for row in rows:
        assert row["row_hash"]
        assert "prev_hash" in row


def test_prune_archives_then_deletes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _events(store, ["a", "b", "c"])
    result = store.prune_before(
        "2030-01-01",
        archive_dir=tmp_path / "archives",
    )
    assert result["pruned"] == 3
    archive = Path(result["archive_path"])
    assert archive.exists()
    lines = archive.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    archived = [json.loads(line) for line in lines]
    # offline verification material is complete
    assert all(entry["row_hash"] for entry in archived)
    assert archived[2]["row_hash"] == result["head_hash"]
    # empty live chain still verifies
    assert store.verify_chain()["valid"] is True
    archives = store.list_archives()
    assert archives[0]["row_count"] == 3
    assert archives[0]["head_hash"] == result["head_hash"]


def test_prune_keeps_recent_and_rechains(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _events(store, ["a", "b", "c"])
    # record() stamps utc_now(); force a controllable timeline by
    # rewriting created_at and re-chaining the three rows
    import sqlite3

    from qwenpaw.hub.database import audit_chain_hash

    stamps = [
        "2026-01-01T00:00:00",
        "2026-06-01T00:00:00",
        "2026-12-01T00:00:00",
    ]
    database = tmp_path / "control.db"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT rowid AS ordinal, * FROM hub_audit_events "
            "ORDER BY rowid",
        ).fetchall()
        previous = None
        for row, stamp in zip(rows, stamps):
            fields = dict(row)
            fields["created_at"] = stamp
            digest = audit_chain_hash(previous, fields)
            connection.execute(
                "UPDATE hub_audit_events SET created_at = ?, "
                "prev_hash = ?, row_hash = ? WHERE rowid = ?",
                (stamp, previous, digest, row["ordinal"]),
            )
            previous = digest
    result = store.prune_before(
        "2026-07-01T00:00:00",
        archive_dir=tmp_path / "archives",
    )
    assert result["pruned"] == 2
    remaining = list(store.iter_events())
    assert len(remaining) == 1
    assert remaining[0]["prev_hash"] is None  # fresh genesis
    assert store.verify_chain()["valid"] is True
    # new events chain onto the fresh genesis
    store.record(
        actor_user_id="u1",
        actor_username="u1",
        action="post-prune",
        resource_type="x",
        resource_id="9",
        detail={},
    )
    verified = store.verify_chain()
    assert verified["valid"] is True and verified["checked"] == 2


def test_prune_nothing_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _events(store, ["a"])
    result = store.prune_before("2000-01-01")
    assert result == {"pruned": 0, "archive_path": None}


def _admin(client: TestClient) -> str:
    response = client.post(
        "/api/auth/register",
        json={"username": "owner", "password": "pw-123456"},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def test_endpoints_export_prune_archives(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = _admin(client)
        headers = {"Authorization": f"Bearer {token}"}
        export = client.get(
            "/api/hub/admin/audit/export",
            headers=headers,
        )
        assert export.status_code == 200
        assert "x-ndjson" in export.headers["content-type"]
        first = json.loads(export.text.strip().splitlines()[0])
        assert first["action"] == "auth.register"
        assert first["row_hash"]
        bad = client.post(
            "/api/hub/admin/audit/prune",
            json={"before": "not-a-date"},
            headers=headers,
        )
        assert bad.status_code == 422
        prune = client.post(
            "/api/hub/admin/audit/prune",
            json={"before": "2030-01-01"},
            headers=headers,
        )
        assert prune.status_code == 200
        body = prune.json()
        before_rows = (
            len(
                list(client.app.state.operations.iter_events()),
            )
            + body["pruned"]
        )
        assert body["pruned"] >= 1
        assert Path(body["archive_path"]).exists()
        assert len(list(client.app.state.operations.iter_events())) == (
            before_rows - body["pruned"]
        )
        archives = client.get(
            "/api/hub/admin/audit/archives",
            headers=headers,
        )
        assert archives.status_code == 200
        assert archives.json()["archives"][0]["head_hash"]
        # verify still green after the prune + the prune audit row
        verify = client.get(
            "/api/hub/admin/audit/verify",
            headers=headers,
        )
        assert verify.json()["valid"] is True
