# -*- coding: utf-8 -*-
"""Tenant-scoped runtime log retention (F7, 02 matrix).

Pull-based, mirrors EP-1-4 UsageCollector: the hub polls each running
runtime's ``/api/debug/backend-logs`` tail with its internal token and
keeps a bounded rolling window of snapshots in the hub database, so an
operator can read one tenant's recent runtime logs from the hub even
after the runtime pod is gone.

Deliberate scope (hub is not a log pipeline — Loki/ELK stay external
per 07 §7):
- retention is a **tail window**: latest N lines per pass, M snapshots
  per runtime, oldest rows pruned;
- unchanged logs (same mtime+size) do not grow the table;
- search is per-runtime tail browsing, not full-text indexing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 300.0
DEFAULT_TAIL_LINES = 500
DEFAULT_KEEP_SNAPSHOTS = 48  # 4h of 5-minute snapshots per runtime

_RUNTIME_TOKEN_HEADER = "X-QwenPaw-Runtime-Token"


class RuntimeLogStore:
    """SQLite persistence for runtime log tail snapshots."""

    def __init__(self, database_path: Any) -> None:
        self._database_path = database_path
        self._init_tables()

    def _connect(self):
        import sqlite3

        connection = sqlite3.connect(
            self._database_path,
            timeout=30,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _init_tables(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_log_snapshots (
                  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  runtime_id TEXT NOT NULL,
                  tenant_id TEXT NOT NULL,
                  captured_at TEXT NOT NULL,
                  log_mtime REAL,
                  log_size INTEGER,
                  content_sha256 TEXT NOT NULL,
                  tail_lines INTEGER NOT NULL,
                  content TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_logs_rt_captured
                ON runtime_log_snapshots(runtime_id, captured_at DESC);
                """,
            )

    def insert_snapshot(
        self,
        *,
        runtime_id: str,
        tenant_id: str,
        log_mtime: float | None,
        log_size: int | None,
        tail_lines: int,
        content: str,
    ) -> int:
        """Store one snapshot and prune the rolling window."""
        captured_at = datetime.now(timezone.utc).isoformat()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_log_snapshots(
                    runtime_id, tenant_id, captured_at, log_mtime,
                    log_size, content_sha256, tail_lines, content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    runtime_id,
                    tenant_id,
                    captured_at,
                    log_mtime,
                    log_size,
                    digest,
                    tail_lines,
                    content,
                ),
            )
            connection.execute(
                """
                DELETE FROM runtime_log_snapshots
                WHERE runtime_id = ? AND snapshot_id NOT IN (
                    SELECT snapshot_id FROM runtime_log_snapshots
                    WHERE runtime_id = ?
                    ORDER BY snapshot_id DESC
                    LIMIT ?
                )
                """,
                (runtime_id, runtime_id, self._keep),
            )
        return len(content)

    _keep = DEFAULT_KEEP_SNAPSHOTS

    def set_keep(self, keep: int) -> None:
        """Adjust the rolling window (tests / ops)."""
        self._keep = max(1, int(keep))

    def latest(
        self,
        runtime_id: str,
        *,
        snapshots: int = 1,
    ) -> list[dict[str, Any]]:
        """Most recent snapshots for one runtime (newest first)."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT snapshot_id, captured_at, log_mtime, log_size,
                       tail_lines, content
                FROM runtime_log_snapshots
                WHERE runtime_id = ?
                ORDER BY snapshot_id DESC
                LIMIT ?
                """,
                (runtime_id, max(1, int(snapshots))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_runtime_ids(self) -> list[str]:
        """Distinct runtimes with retained logs (ops diagnostics)."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT runtime_id FROM runtime_log_snapshots",
            ).fetchall()
        return [str(row["runtime_id"]) for row in rows]


class RuntimeLogCollector:
    """Background poller for runtime log tails (F7)."""

    def __init__(
        self,
        *,
        runtime_service: Any,
        credential_vault: Any,
        store: RuntimeLogStore,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        tail_lines: int = DEFAULT_TAIL_LINES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._runtime_service = runtime_service
        self._credential_vault = credential_vault
        self._store = store
        self._interval = interval_seconds
        self._tail_lines = tail_lines
        self._transport = transport
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_pass_at: str | None = None
        self.last_error: str | None = None

    @property
    def store(self) -> RuntimeLogStore:
        """Backing store (tests / diagnostics)."""
        return self._store

    async def collect_once(self) -> int:
        """One pass; returns bytes stored (0 when nothing changed)."""
        records = await asyncio.to_thread(self._runtime_service.list)
        running = [
            record
            for record in records
            if getattr(record, "state", None) == "running"
        ]
        stored = 0
        had_error = False
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=10.0,
        ) as client:
            for record in running:
                target = (
                    f"http://{record.host}:{record.port}"
                    f"/api/debug/backend-logs"
                )
                try:
                    token = await asyncio.to_thread(
                        self._credential_vault.get_runtime_secret,
                        tenant_id=record.tenant_id,
                        runtime_id=record.runtime_id,
                        name="QWENPAW_RUNTIME_INTERNAL_TOKEN",
                    )
                except Exception:  # noqa: BLE001
                    token = None
                if not token:
                    continue
                try:
                    response = await client.get(
                        target,
                        params={"lines": self._tail_lines},
                        headers={_RUNTIME_TOKEN_HEADER: token},
                    )
                    response.raise_for_status()
                    payload = response.json()
                except Exception as exc:  # noqa: BLE001
                    had_error = True
                    self.last_error = f"{record.runtime_id}: {exc}"
                    logger.debug(
                        "log collection failed for %s: %s",
                        record.runtime_id,
                        exc,
                    )
                    continue
                content = str(payload.get("content") or "")
                if not content:
                    continue
                stored += await asyncio.to_thread(
                    self._store.insert_snapshot,
                    runtime_id=record.runtime_id,
                    tenant_id=record.tenant_id,
                    log_mtime=payload.get("updated_at"),
                    log_size=int(payload.get("size") or 0),
                    tail_lines=int(payload.get("lines") or 0),
                    content=content,
                )
        stamp = datetime.now(timezone.utc)
        self.last_pass_at = stamp.isoformat()
        if not had_error:
            self.last_error = None
        return stored

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.collect_once()
            except Exception:  # noqa: BLE001
                logger.exception("runtime log collector pass crashed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(
                self._run(),
                name="qwenpaw-hub-runtime-log-collector",
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
