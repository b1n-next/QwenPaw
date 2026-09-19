# -*- coding: utf-8 -*-
"""Hub-side LLM usage accounting (EP-1-4).

Pull-based collector: instead of instrumenting every runtime with a
reporting hook, the hub periodically polls each running personal
runtime's existing ``GET /api/token-usage/details`` endpoint (already
user-plane allowed) with the per-runtime internal token, and upserts
the returned per-(date, provider, model, agent) counters into the
``usage_counters`` table. The runtime JSON stays the source of truth,
which makes re-polls idempotent and needs zero runtime-side patches.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class UsageTotals:
    """Aggregated counters for one grouping row."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    call_count: int = 0
    rows: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class UsageStore:
    """Append-mostly counter table for per-runtime usage snapshots."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
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
                CREATE TABLE IF NOT EXISTS usage_counters (
                  tenant_id TEXT NOT NULL,
                  usage_date TEXT NOT NULL,
                  provider_id TEXT NOT NULL DEFAULT '',
                  model TEXT NOT NULL,
                  agent_id TEXT,
                  prompt_tokens INTEGER NOT NULL DEFAULT 0,
                  completion_tokens INTEGER NOT NULL DEFAULT 0,
                  call_count INTEGER NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (
                    tenant_id, usage_date, provider_id, model, agent_id
                  )
                );
                CREATE INDEX IF NOT EXISTS usage_counters_date_idx
                  ON usage_counters (usage_date);
                """,
            )

    def detail_rows(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Raw aggregated rows (tenant×agent×provider×model) for export.

        E10 billing export: finer than `summary` group-bys — one row
        per PK tuple in the range, newest last.
        """
        end_d = date.fromisoformat(end_date) if end_date else date.today()
        start_d = (
            date.fromisoformat(start_date)
            if start_date
            else end_d - timedelta(days=29)
        )
        if start_d > end_d:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT tenant_id, usage_date, provider_id, model, agent_id,
                       prompt_tokens, completion_tokens, call_count
                FROM usage_counters
                WHERE usage_date BETWEEN ? AND ?
                ORDER BY usage_date, tenant_id, agent_id, model
                """,
                (start_d.isoformat(), end_d.isoformat()),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_rows(
        self,
        tenant_id: str,
        rows: list[dict[str, Any]],
    ) -> int:
        """Upsert last-seen counters for *tenant_id*; returns row count."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO usage_counters (
                      tenant_id, usage_date, provider_id, model, agent_id,
                      prompt_tokens, completion_tokens, call_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (
                      tenant_id, usage_date, provider_id, model, agent_id
                    ) DO UPDATE SET
                      prompt_tokens = excluded.prompt_tokens,
                      completion_tokens = excluded.completion_tokens,
                      call_count = excluded.call_count,
                      updated_at = excluded.updated_at
                    """,
                    (
                        tenant_id,
                        str(row.get("date") or ""),
                        str(row.get("provider_id") or ""),
                        str(row.get("model") or ""),
                        # NULL never matches in a SQLite PK conflict
                        # target; store "" for the un-attributed case.
                        str(row.get("agent_id") or ""),
                        int(row.get("prompt_tokens") or 0),
                        int(row.get("completion_tokens") or 0),
                        int(row.get("call_count") or 0),
                        now,
                    ),
                )
        return len(rows)

    def summary(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        tenant_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate counters by tenant (user), model and date."""
        end_d = date.fromisoformat(end_date) if end_date else date.today()
        start_d = (
            date.fromisoformat(start_date)
            if start_date
            else end_d - timedelta(days=29)
        )
        if start_d > end_d:
            start_d, end_d = end_d, start_d
        clauses = ["usage_date >= ?", "usage_date <= ?"]
        params: list[Any] = [start_d.isoformat(), end_d.isoformat()]
        if tenant_id:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if model:
            clauses.append("model = ?")
            params.append(model)
        where = " AND ".join(clauses)
        with self._connect() as connection:
            by_tenant = {
                (
                    row["tenant_id"] if row["tenant_id"] else "(unknown)"
                ): UsageTotals(
                    prompt_tokens=row["prompt_tokens"],
                    completion_tokens=row["completion_tokens"],
                    call_count=row["call_count"],
                    rows=row["rows"],
                )
                for row in connection.execute(
                    f"""
                    SELECT tenant_id,
                           SUM(prompt_tokens) AS prompt_tokens,
                           SUM(completion_tokens) AS completion_tokens,
                           SUM(call_count) AS call_count,
                           COUNT(*) AS rows
                    FROM usage_counters WHERE {where}
                    GROUP BY tenant_id
                    """,
                    params,
                )
            }
            by_agent_rows = connection.execute(
                f"""
                SELECT agent_id,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(call_count) AS call_count,
                       COUNT(*) AS rows
                FROM usage_counters WHERE {where}
                GROUP BY agent_id
                ORDER BY SUM(call_count) DESC
                """,
                params,
            ).fetchall()
            by_model_rows = connection.execute(
                f"""
                SELECT model,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(call_count) AS call_count,
                       COUNT(*) AS rows
                FROM usage_counters WHERE {where}
                GROUP BY model ORDER BY SUM(call_count) DESC
                """,
                params,
            ).fetchall()
            by_date_rows = connection.execute(
                f"""
                SELECT usage_date,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(call_count) AS call_count,
                       COUNT(*) AS rows
                FROM usage_counters WHERE {where}
                GROUP BY usage_date ORDER BY usage_date
                """,
                params,
            ).fetchall()
        total = UsageTotals()
        for entry in by_tenant.values():
            total.prompt_tokens += entry.prompt_tokens
            total.completion_tokens += entry.completion_tokens
            total.call_count += entry.call_count
            total.rows += entry.rows
        return {
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
            "total": {
                "prompt_tokens": total.prompt_tokens,
                "completion_tokens": total.completion_tokens,
                "call_count": total.call_count,
            },
            "by_user": {
                tenant: {
                    "prompt_tokens": entry.prompt_tokens,
                    "completion_tokens": entry.completion_tokens,
                    "call_count": entry.call_count,
                }
                for tenant, entry in sorted(by_tenant.items())
            },
            "by_agent": [
                {
                    "agent_id": (
                        row["agent_id"] if row["agent_id"] else "(default)"
                    ),
                    "prompt_tokens": row["prompt_tokens"],
                    "completion_tokens": row["completion_tokens"],
                    "call_count": row["call_count"],
                }
                for row in by_agent_rows
            ],
            "by_model": [
                {
                    "model": row["model"],
                    "prompt_tokens": row["prompt_tokens"],
                    "completion_tokens": row["completion_tokens"],
                    "call_count": row["call_count"],
                }
                for row in by_model_rows
            ],
            "by_date": [
                {
                    "date": row["usage_date"],
                    "prompt_tokens": row["prompt_tokens"],
                    "completion_tokens": row["completion_tokens"],
                    "call_count": row["call_count"],
                }
                for row in by_date_rows
            ],
        }

    def known_dates(self) -> list[str]:
        """Distinct usage dates present (oldest first)."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT usage_date FROM usage_counters "
                "ORDER BY usage_date",
            ).fetchall()
        return [row["usage_date"] for row in rows]
