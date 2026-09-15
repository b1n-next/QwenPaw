# -*- coding: utf-8 -*-
"""Prompt asset library with an approval workflow (EP-2-24).

Assets carry versioned content; every edit lands as a pending
version that only takes effect after an admin approves it — the
"template changes go through approval" DoD. Members read the
current approved version.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_prompt_assets (
    asset_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    current_version INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS hub_prompt_versions (
    asset_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    proposed_by TEXT,
    reviewed_by TEXT,
    created_at TEXT,
    reviewed_at TEXT,
    PRIMARY KEY (asset_id, version)
);
"""


class PromptLibrary:
    """hub_prompt_assets + hub_prompt_versions on the control DB."""

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

    def propose(
        self,
        asset_id: str,
        name: str,
        content: str,
        *,
        proposed_by: str,
        category: str = "general",
    ) -> dict:
        """Create an asset or propose its next version (pending)."""
        if not asset_id.strip() or len(asset_id) > 64:
            raise ValueError("asset_id must be 1-64 characters")
        if not name.strip():
            raise ValueError("name is required")
        if not content.strip():
            raise ValueError("content is required")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            asset = connection.execute(
                "SELECT current_version FROM hub_prompt_assets "
                "WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            version = (asset["current_version"] or 0) + 1 if asset else 1
            if asset is None:
                connection.execute(
                    """
                    INSERT INTO hub_prompt_assets(
                        asset_id, name, category, current_version,
                        created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, 0, ?, ?, ?)
                    """,
                    (asset_id, name.strip(), category, proposed_by, now, now),
                )
            else:
                connection.execute(
                    "UPDATE hub_prompt_assets SET name = ?, category = ?, "
                    "updated_at = ? WHERE asset_id = ?",
                    (name.strip(), category, now, asset_id),
                )
            connection.execute(
                """
                INSERT INTO hub_prompt_versions(
                    asset_id, version, content, status, proposed_by,
                    created_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (asset_id, version, content, proposed_by, now),
            )
        return self.get_asset(asset_id) or {}

    def review(
        self,
        asset_id: str,
        version: int,
        decision: str,
        *,
        reviewed_by: str,
    ) -> Optional[dict]:
        """Approve or reject a pending version (approve publishes)."""
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved or rejected")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM hub_prompt_versions "
                "WHERE asset_id = ? AND version = ?",
                (asset_id, version),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return None
            connection.execute(
                "UPDATE hub_prompt_versions SET status = ?, "
                "reviewed_by = ?, reviewed_at = ? "
                "WHERE asset_id = ? AND version = ?",
                (decision, reviewed_by, now, asset_id, version),
            )
            if decision == "approved":
                connection.execute(
                    "UPDATE hub_prompt_assets SET current_version = ?, "
                    "updated_at = ? WHERE asset_id = ?",
                    (version, now, asset_id),
                )
        return self.get_asset(asset_id)

    # -------------------------------------------------------------- reads

    def get_asset(self, asset_id: str) -> Optional[dict]:
        """Asset with its current approved content (if any)."""
        with self._connect() as connection:
            asset = connection.execute(
                "SELECT * FROM hub_prompt_assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            if asset is None:
                return None
            versions = connection.execute(
                "SELECT version, status, proposed_by, reviewed_by, "
                "created_at, reviewed_at FROM hub_prompt_versions "
                "WHERE asset_id = ? ORDER BY version DESC",
                (asset_id,),
            ).fetchall()
        current = int(asset["current_version"])
        content_row = None
        if current > 0:
            with self._connect() as connection:
                content_row = connection.execute(
                    "SELECT content FROM hub_prompt_versions "
                    "WHERE asset_id = ? AND version = ?",
                    (asset_id, current),
                ).fetchone()
        return {
            "asset_id": asset["asset_id"],
            "name": asset["name"],
            "category": asset["category"],
            "current_version": current,
            "created_by": asset["created_by"],
            "updated_at": asset["updated_at"],
            "content": content_row["content"] if content_row else "",
            "versions": [dict(row) for row in versions],
        }

    def list_assets(
        self,
        *,
        include_pending: bool = False,
    ) -> list[dict]:
        """Approved assets (members) or everything with pending (admin)."""
        query = "SELECT * FROM hub_prompt_assets ORDER BY updated_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
            pending = {
                row["asset_id"]
                for row in connection.execute(
                    "SELECT DISTINCT asset_id FROM hub_prompt_versions "
                    "WHERE status = 'pending'",
                ).fetchall()
            }
        assets = []
        for row in rows:
            current = int(row["current_version"])
            has_pending = row["asset_id"] in pending
            if current == 0 and not include_pending:
                continue  # nothing approved yet — invisible to members
            if not include_pending and has_pending:
                # pending proposal exists but members still see approved
                pass
            assets.append(
                {
                    "asset_id": row["asset_id"],
                    "name": row["name"],
                    "category": row["category"],
                    "current_version": current,
                    "updated_at": row["updated_at"],
                    "has_pending": has_pending,
                },
            )
        return assets


__all__ = ["PromptLibrary"]
