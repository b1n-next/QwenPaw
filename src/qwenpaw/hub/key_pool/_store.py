# -*- coding: utf-8 -*-
"""Provider API key pool with round-robin leasing (EP-2-24)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_api_keys (
    key_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    key_value TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    created_by TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hub_api_keys_provider
    ON hub_api_keys(provider);
"""


class KeyPool:
    """Round-robin leases per provider, disabled keys skipped."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=30,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _init_tables(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    # ------------------------------------------------------------- writes

    def add_key(
        self,
        provider: str,
        key_value: str,
        *,
        created_by: str = "",
        key_id: str = "",
    ) -> dict:
        """Register one key under a provider."""
        provider = provider.strip()
        if not provider or len(provider) > 64:
            raise ValueError("provider must be 1-64 characters")
        if not key_value.strip():
            raise ValueError("key_value is required")
        with self._connect() as connection:
            key_id = key_id.strip() or (
                f"key-{provider}-{len(key_value)}"
                f"-{datetime.now(timezone.utc).strftime('%H%M%S%f')}"
            )
            existing = connection.execute(
                "SELECT key_id FROM hub_api_keys WHERE key_id = ?",
                (key_id,),
            ).fetchone()
            if existing:
                raise ValueError(f"key_id already exists: {key_id}")
            connection.execute(
                """
                INSERT INTO hub_api_keys(
                    key_id, provider, key_value, status, created_by,
                    created_at
                ) VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (
                    key_id,
                    provider,
                    key_value.strip(),
                    created_by,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return self.get_key(key_id) or {}

    def set_status(self, key_id: str, status: str) -> Optional[dict]:
        """Enable or disable one key."""
        if status not in ("active", "disabled"):
            raise ValueError("status must be active or disabled")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT key_id FROM hub_api_keys WHERE key_id = ?",
                (key_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE hub_api_keys SET status = ? WHERE key_id = ?",
                (status, key_id),
            )
        return self.get_key(key_id)

    def lease(self, provider: str) -> Optional[dict]:
        """Hand out the next active key (round-robin, counted)."""
        provider = provider.strip()
        if not provider:
            raise ValueError("provider is required")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT key_id, use_count FROM hub_api_keys "
                "WHERE provider = ? AND status = 'active' "
                "ORDER BY key_id",
                (provider,),
            ).fetchall()
            if not rows:
                return None
            counts = [int(row["use_count"]) for row in rows]
            lowest = min(counts)
            chosen = next(
                row for row in rows if int(row["use_count"]) == lowest
            )
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "UPDATE hub_api_keys SET use_count = use_count + 1, "
                "last_used_at = ? WHERE key_id = ?",
                (now, chosen["key_id"]),
            )
        key = self.get_key(chosen["key_id"]) or {}
        key["key_value"] = self._value_of(chosen["key_id"])
        return key

    # -------------------------------------------------------------- reads

    def _value_of(self, key_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT key_value FROM hub_api_keys WHERE key_id = ?",
                (key_id,),
            ).fetchone()
        return row["key_value"] if row else ""

    def get_key(self, key_id: str) -> Optional[dict]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT key_id, provider, status, use_count, "
                "last_used_at, created_by, created_at "
                "FROM hub_api_keys WHERE key_id = ?",
                (key_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_keys(
        self,
        *,
        provider: Optional[str] = None,
    ) -> list[dict]:
        """Metadata only — key values never leave through list()."""
        query = (
            "SELECT key_id, provider, status, use_count, last_used_at, "
            "created_by, created_at FROM hub_api_keys"
        )
        params: tuple = ()
        if provider:
            query += " WHERE provider = ?"
            params = (provider,)
        query += " ORDER BY provider, key_id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]


__all__ = ["KeyPool"]
