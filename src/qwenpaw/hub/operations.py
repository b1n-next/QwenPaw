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
    ) -> None:
        """Append one sanitized, hash-chained Hub management event."""
        event_id = uuid.uuid4().hex
        detail_json = json.dumps(
            detail or {},
            ensure_ascii=False,
            sort_keys=True,
        )
        created_at = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
                    detail_json, created_at, prev_hash, row_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                "SELECT rowid AS ordinal, * FROM hub_audit_events "
                "ORDER BY rowid",
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
        head = rows[-1] if rows else None
        return {
            "valid": True,
            "checked": checked,
            "head_hash": head["row_hash"] if head is not None else None,
        }

    def chain_head(self) -> dict[str, Any]:
        """Latest chain digest for external anchoring (H2)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT event_id, created_at, row_hash FROM "
                "hub_audit_events ORDER BY rowid DESC LIMIT 1",
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

    def host_metrics(self) -> dict[str, float]:
        """Collect portable host utilization percentages."""
        return {
            "cpu_percent": round(float(psutil.cpu_percent()), 1),
            "memory_percent": round(
                float(psutil.virtual_memory().percent),
                1,
            ),
            "disk_percent": round(
                float(psutil.disk_usage(str(self.data_root)).percent),
                1,
            ),
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
            "detail": json.loads(str(row["detail_json"])),
            "created_at": str(row["created_at"]),
        }
