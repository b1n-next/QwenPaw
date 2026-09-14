# -*- coding: utf-8 -*-
"""Durable approval persistence for the runtime plane (EP-2-12).

The in-memory ``ApprovalService._pending`` dict dies with the process:
a ``kill -9`` between "tool asks permission" and "user answers" used to
silently drop the request. This store mirrors the approval lifecycle
into a small SQLite database (create → resolve/cancel/timeout) and
feeds a startup recovery scan, so pending approvals survive runtime
restarts and remain answerable (or at least auditable) afterwards.

Write failures never break the approval flow: the store is best-effort
shadow state, mirroring the audit-write discipline.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_events (
    request_id      TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    root_session_id TEXT NOT NULL,
    owner_agent_id  TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    channel         TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    created_at      REAL NOT NULL,
    timeout_seconds REAL NOT NULL,
    status          TEXT NOT NULL,
    resolved_at     REAL,
    findings_count  INTEGER NOT NULL DEFAULT 0,
    severity        TEXT NOT NULL DEFAULT 'medium',
    result_summary  TEXT NOT NULL DEFAULT '',
    identity_policy TEXT NOT NULL DEFAULT 'AGENT',
    scope           TEXT,
    source          TEXT NOT NULL DEFAULT 'tool_guard',
    extra_json      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_approval_status_created
    ON approval_events(status, created_at);
"""

# Trim decided history once it passes this age (days).
_HISTORY_MAX_AGE_SECONDS = 14 * 24 * 3600


def _safe_extra(extra: dict[str, Any]) -> str:
    """JSON-serialize extra payload, dropping live objects.

    ``extra`` may carry non-serializable helpers (channel instances,
    lock handles). Those are runtime-only wiring and are represented
    as their type name; everything structured survives for recovery.
    """
    try:
        return json.dumps(
            {
                key: (
                    f"<{type(value).__name__}>"
                    if not _is_jsonable(value)
                    else value
                )
                for key, value in extra.items()
            },
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError):
        return "{}"


def _is_jsonable(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


class ApprovalStore:
    """SQLite-backed shadow of the approval lifecycle."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(database_path),
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------ writes

    def save_pending(
        self,
        pending: Any,
        *,
        source: str = "tool_guard",
    ) -> None:
        """Insert one pending approval row (best-effort)."""
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO approval_events(
                        request_id, session_id, root_session_id,
                        owner_agent_id, user_id, channel, agent_id,
                        tool_name, created_at, timeout_seconds, status,
                        findings_count, severity, result_summary,
                        identity_policy, source, extra_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending',
                              ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pending.request_id,
                        pending.session_id,
                        pending.root_session_id,
                        pending.owner_agent_id,
                        pending.user_id,
                        pending.channel,
                        pending.agent_id,
                        pending.tool_name,
                        pending.created_at,
                        pending.timeout_seconds,
                        pending.findings_count,
                        pending.severity,
                        pending.result_summary,
                        pending.identity_policy.value,
                        source,
                        _safe_extra(pending.extra or {}),
                    ),
                )
                self._conn.commit()
        except sqlite3.Error:
            logger.warning(
                "ApprovalStore.save_pending failed for %s",
                getattr(pending, "request_id", "?"),
                exc_info=True,
            )

    def mark_resolved(
        self,
        request_id: str,
        *,
        status: str,
        resolved_at: float,
        scope: str | None = None,
    ) -> None:
        """Update one row to a decided status (best-effort)."""
        try:
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE approval_events
                    SET status = ?, resolved_at = ?, scope = ?
                    WHERE request_id = ?
                    """,
                    (status, resolved_at, scope, request_id),
                )
                self._conn.commit()
        except sqlite3.Error:
            logger.warning(
                "ApprovalStore.mark_resolved failed for %s",
                request_id,
                exc_info=True,
            )

    # ------------------------------------------------------------- reads

    def load_unresolved(self) -> list[dict[str, Any]]:
        """Return persisted rows still marked pending."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM approval_events WHERE status = 'pending'
                ORDER BY created_at ASC
                """,
            ).fetchall()
        columns = [
            desc[0]
            for desc in self._conn.execute(
                "SELECT * FROM approval_events LIMIT 0",
            ).description
        ]
        return [dict(zip(columns, row)) for row in rows]

    # ---------------------------------------------------------- cleanup

    def purge_expired(self) -> int:
        """Delete decided history older than the retention window."""
        cutoff = time.time() - _HISTORY_MAX_AGE_SECONDS
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "DELETE FROM approval_events "
                    "WHERE status != 'pending' AND resolved_at < ?",
                    (cutoff,),
                )
                self._conn.commit()
                return cursor.rowcount
        except sqlite3.Error:
            logger.warning("ApprovalStore.purge_expired failed", exc_info=True)
            return 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
