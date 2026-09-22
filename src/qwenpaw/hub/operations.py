# -*- coding: utf-8 -*-
"""Lightweight operational telemetry for QwenPaw Hub."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import psutil

from .database import (
    audit_chain_hash,
    connect_hub_database,
    initialize_hub_database,
    utc_now,
)


def _rowid_expr(connection: Any) -> str:
    """Dialect row-ordinal column: rowid (SQLite) / ctid (PG)."""
    from .db_adapter import PgConnection

    return "ctid" if isinstance(connection, PgConnection) else "rowid"


class HubOperationsStore:
    """Persist audit events and collect inexpensive host metrics."""

    def __init__(self, database_path: Path, data_root: Path) -> None:
        self.database_path = database_path
        self.data_root = data_root
        initialize_hub_database(database_path)

    def _connect(self) -> sqlite3.Connection:
        return connect_hub_database(self.database_path)

    def record(
        self,
        *,
        actor_user_id: str,
        actor_username: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str = "success",
        detail: dict[str, Any] | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
        remote_address: str | None = None,
        quota_dimension: str | None = None,
        quota_used: int | None = None,
        quota_limit: int | None = None,
    ) -> None:
        """Append one sanitized, hash-chained Hub management event.

        The three ``quota_*`` fields (F6) lift the quota decision out
        of the detail JSON into queryable columns for quota-scoped
        audit filters; every other action leaves them NULL.
        """
        event_id = uuid.uuid4().hex
        detail_json = json.dumps(
            detail or {},
            ensure_ascii=False,
            sort_keys=True,
        )
        created_at = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            from .db_adapter import PgConnection

            if isinstance(connection, PgConnection):
                head = connection.execute(
                    "SELECT row_hash FROM hub_audit_events "
                    "ORDER BY ctid DESC LIMIT 1",
                ).fetchone()
            else:
                head = connection.execute(
                    "SELECT row_hash FROM hub_audit_events "
                    "ORDER BY rowid DESC LIMIT 1",
                ).fetchone()
            prev_hash = head["row_hash"] if head is not None else None
            row_hash = audit_chain_hash(
                prev_hash,
                {
                    "event_id": event_id,
                    "actor_user_id": actor_user_id,
                    "actor_username": actor_username,
                    "action": action,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "outcome": outcome,
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "trace_id": trace_id,
                    "remote_address": remote_address,
                    "quota_dimension": quota_dimension,
                    "quota_used": quota_used,
                    "quota_limit": quota_limit,
                    "detail_json": detail_json,
                    "created_at": created_at,
                },
            )
            connection.execute(
                """
                INSERT INTO hub_audit_events(
                    event_id, actor_user_id, actor_username, action,
                    resource_type, resource_id, outcome, request_id,
                    correlation_id, trace_id, remote_address,
                    quota_dimension, quota_used, quota_limit,
                    detail_json, created_at, prev_hash, row_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    actor_user_id,
                    actor_username,
                    action,
                    resource_type,
                    resource_id,
                    outcome,
                    request_id,
                    correlation_id,
                    trace_id,
                    remote_address,
                    quota_dimension,
                    quota_used,
                    quota_limit,
                    detail_json,
                    created_at,
                    prev_hash,
                    row_hash,
                ),
            )

    def verify_chain(self) -> dict[str, Any]:
        """Walk the whole audit chain and report integrity (H2).

        Recomputes every row hash and checks predecessor linkage;
        returns the first break when anything was tampered with,
        deleted, or reordered.
        """
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_rowid_expr(connection)} AS ordinal, "
                "* FROM hub_audit_events "
                f"ORDER BY {_rowid_expr(connection)}",
            ).fetchall()
        previous: str | None = None
        checked = 0
        for row in rows:
            if row["prev_hash"] != previous:
                return {
                    "valid": False,
                    "checked": checked,
                    "reason": "broken-link",
                    "at_event_id": row["event_id"],
                }
            expected = audit_chain_hash(previous, row)
            if expected != row["row_hash"]:
                return {
                    "valid": False,
                    "checked": checked,
                    "reason": "row-hash-mismatch",
                    "at_event_id": row["event_id"],
                }
            previous = row["row_hash"]
            checked += 1
        head_hash: str | None = None
        if rows:
            head_hash = str(rows[-1]["row_hash"])
        return {
            "valid": True,
            "checked": checked,
            "head_hash": head_hash,
        }

    def iter_events(
        self,
        *,
        before: str | None = None,
        after: str | None = None,
    ):
        """Yield audit rows (hash columns included) for export (H3)."""
        clauses: list[str] = []
        params: list[object] = []
        if before:
            clauses.append("created_at < ?")
            params.append(before)
        if after:
            clauses.append("created_at >= ?")
            params.append(after)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM hub_audit_events {where} "
                f"ORDER BY {_rowid_expr(connection)}",
                tuple(params),
            ).fetchall()
        for row in rows:
            yield dict(row)

    def prune_before(
        self,
        cutoff: str,
        *,
        archive_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Archive rows older than *cutoff* to JSONL, then delete them.

        Chain-aware (H2/H3 contract): the archived segment keeps its
        hashes so it can be verified offline; after deletion the
        remaining chain restarts from a fresh genesis and the pruned
        segment's head hash is recorded in audit_chain_archives as an
        anchoring point.
        """
        target_dir = archive_dir or self.data_root
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = utc_now().replace(":", "").replace("-", "")[:15]
        archive_path = target_dir / f"audit-archive-{stamp}.jsonl"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT {_rowid_expr(connection)} AS ordinal, "
                "* FROM hub_audit_events "
                f"WHERE created_at < ? "
                f"ORDER BY {_rowid_expr(connection)}",
                (cutoff,),
            ).fetchall()
            if not rows:
                return {"pruned": 0, "archive_path": None}
            with open(archive_path, "w", encoding="utf-8") as handle:
                for row in rows:
                    payload = {
                        key: row[key] for key in row.keys() if key != "ordinal"
                    }
                    handle.write(
                        json.dumps(payload, ensure_ascii=False) + "\n",
                    )
            first = rows[0]
            last = rows[-1]
            connection.execute(
                "DELETE FROM hub_audit_events WHERE created_at < ?",
                (cutoff,),
            )
            # remaining chain: fresh genesis (re-hash the new head —
            # its old digest chained to a predecessor now archived)
            survivor = connection.execute(
                f"SELECT {_rowid_expr(connection)} AS ordinal, "
                "* FROM hub_audit_events "
                f"ORDER BY {_rowid_expr(connection)} LIMIT 1",
            ).fetchone()
            if survivor is not None:
                genesis_hash = audit_chain_hash(None, survivor)
                connection.execute(
                    "UPDATE hub_audit_events SET prev_hash = NULL, "
                    f"row_hash = ? WHERE {_rowid_expr(connection)} = ?",
                    (genesis_hash, survivor["ordinal"]),
                )
            archive_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO audit_chain_archives(archive_id, "
                "first_event_id, last_event_id, row_count, head_hash, "
                "archive_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    archive_id,
                    first["event_id"],
                    last["event_id"],
                    len(rows),
                    last["row_hash"],
                    str(archive_path),
                    utc_now(),
                ),
            )
        return {
            "pruned": len(rows),
            "archive_path": str(archive_path),
            "archive_id": archive_id,
            "head_hash": last["row_hash"],
        }

    def list_archives(self) -> list[dict[str, Any]]:
        """Pruned chain segments (anchoring points, H3)."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT archive_id, first_event_id, last_event_id, "
                "row_count, head_hash, archive_path, created_at "
                "FROM audit_chain_archives ORDER BY created_at",
            ).fetchall()
        return [dict(row) for row in rows]

    def chain_head(self) -> dict[str, Any]:
        """Latest chain digest for external anchoring (H2)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT event_id, created_at, row_hash FROM "
                f"hub_audit_events ORDER BY "
                f"{_rowid_expr(connection)} DESC LIMIT 1",
            ).fetchone()
        if row is None:
            return {"rows": 0, "head_hash": None}
        return {
            "rows": connection.execute(  # type: ignore[union-attr]
                "SELECT COUNT(*) FROM hub_audit_events",
            ).fetchone()[0],
            "head_hash": row["row_hash"],
            "head_event_id": row["event_id"],
            "head_created_at": row["created_at"],
        }

    def list_events(
        self,
        *,
        page: int,
        page_size: int,
        query: str | None = None,
        action: str | None = None,
        outcome: str | None = None,
        trace_id: str | None = None,
        quota_dimension: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return one filtered audit page without secret data."""
        clauses: list[str] = []
        parameters: list[object] = []
        if query:
            clauses.append(
                "(actor_username LIKE ? OR resource_id LIKE ?)",
            )
            pattern = f"%{query}%"
            parameters.extend([pattern, pattern])
        if action:
            clauses.append("action = ?")
            parameters.append(action)
        if outcome:
            clauses.append("outcome = ?")
            parameters.append(outcome)
        if trace_id:
            clauses.append("trace_id = ?")
            parameters.append(trace_id)
        if quota_dimension:
            clauses.append("quota_dimension = ?")
            parameters.append(quota_dimension)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            total_row = connection.execute(
                "SELECT COUNT(*) AS count FROM hub_audit_events" f"{where}",
                parameters,
            ).fetchone()
            rows = connection.execute(
                "SELECT * FROM hub_audit_events"
                f"{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*parameters, page_size, (page - 1) * page_size),
            ).fetchall()
        return [self._event_from_row(row) for row in rows], int(
            total_row["count"],
        )

    def host_metrics(self) -> dict[str, float | int | str]:
        """Collect capacity for the filesystem containing Hub user data."""
        memory = psutil.virtual_memory()
        data_path = self.data_root.resolve()
        disk = psutil.disk_usage(str(data_path))
        return {
            "cpu_percent": round(float(psutil.cpu_percent()), 1),
            "memory_percent": round(float(memory.percent), 1),
            "memory_used": memory.used,
            "memory_total": memory.total,
            "memory_available": memory.available,
            "disk_percent": round(float(disk.percent), 1),
            "disk_used": disk.used,
            "disk_total": disk.total,
            "disk_free": disk.free,
            "disk_path": str(data_path),
        }

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": str(row["event_id"]),
            "actor_user_id": str(row["actor_user_id"]),
            "actor_username": str(row["actor_username"]),
            "action": str(row["action"]),
            "resource_type": str(row["resource_type"]),
            "resource_id": str(row["resource_id"]),
            "outcome": str(row["outcome"]),
            "request_id": row["request_id"],
            "correlation_id": row["correlation_id"],
            "trace_id": row["trace_id"],
            "remote_address": row["remote_address"],
            "quota_dimension": row["quota_dimension"],
            "quota_used": row["quota_used"],
            "quota_limit": row["quota_limit"],
            "detail": json.loads(str(row["detail_json"])),
            "created_at": str(row["created_at"]),
        }
