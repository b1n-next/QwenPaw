# -*- coding: utf-8 -*-
"""Agent template marketplace storage (EP-2-19).

Templates package a graph (EP-2-17/18 schema), a conversation prompt
and a skills list. Admins publish or offline them; members see only
published entries in the hall.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ALLOWED_STATUS = frozenset(
    {"draft", "pending_review", "published", "offline"},
)
_MANIFEST_KEYS = ("name", "description", "prompt", "graph", "skills")


class TemplateStore:
    """hub_agent_templates table on the shared control database."""

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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS hub_agent_templates (
                    template_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    manifest_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """,
            )

    # ------------------------------------------------------------- writes

    def upsert_template(
        self,
        template_id: str,
        manifest: dict[str, Any],
        *,
        created_by: str,
        status: str = "draft",
    ) -> dict[str, Any]:
        """Create or update one template (revision bumps on update)."""
        if not isinstance(template_id, str) or not template_id.strip():
            raise ValueError("template_id must be a non-empty string")
        if len(template_id) > 64:
            raise ValueError("template_id exceeds 64 characters")
        normalized = self._validate_manifest(manifest)
        if status not in _ALLOWED_STATUS:
            raise ValueError(
                f"status must be one of {sorted(_ALLOWED_STATUS)}",
            )
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT revision, created_by, created_at FROM "
                "hub_agent_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            revision = (row["revision"] + 1) if row else 1
            connection.execute(
                """
                INSERT INTO hub_agent_templates(
                    template_id, name, description, manifest_json,
                    status, revision, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(template_id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    manifest_json = excluded.manifest_json,
                    status = excluded.status,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (
                    template_id,
                    normalized["name"],
                    str(normalized.get("description") or ""),
                    json.dumps(normalized, ensure_ascii=False),
                    status,
                    revision,
                    created_by,
                    (row["created_at"] if row else now),
                    now,
                ),
            )
        return self.get_template(template_id) or {}

    def set_status(
        self,
        template_id: str,
        status: str,
    ) -> Optional[dict[str, Any]]:
        """Publish or offline one template."""
        if status not in _ALLOWED_STATUS:
            raise ValueError(
                f"status must be one of {sorted(_ALLOWED_STATUS)}",
            )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT template_id FROM hub_agent_templates "
                "WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE hub_agent_templates SET status = ?, "
                "updated_at = ? WHERE template_id = ?",
                (
                    status,
                    datetime.now(timezone.utc).isoformat(),
                    template_id,
                ),
            )
        return self.get_template(template_id)

    @staticmethod
    def _validate_manifest(manifest: object) -> dict[str, Any]:
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
        name = manifest.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("manifest.name must be a non-empty string")
        graph = manifest.get("graph")
        if graph is not None and not isinstance(graph, dict):
            raise ValueError("manifest.graph must be an object when present")
        skills = manifest.get("skills", [])
        if not isinstance(skills, list) or any(
            not isinstance(item, str) for item in skills
        ):
            raise ValueError("manifest.skills must be a list of strings")
        prompt = manifest.get("prompt", "")
        if not isinstance(prompt, str):
            raise ValueError("manifest.prompt must be a string")
        normalized = dict(manifest)
        normalized["name"] = name.strip()
        normalized.setdefault("description", "")
        normalized.setdefault("prompt", prompt)
        normalized.setdefault("skills", skills)
        return {
            key: normalized.get(key)
            for key in _MANIFEST_KEYS
            if normalized.get(key) is not None
        }

    # -------------------------------------------------------------- reads

    def get_template(self, template_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM hub_agent_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def list_templates(
        self,
        *,
        published_only: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM hub_agent_templates"
        if published_only:
            query += " WHERE status = 'published'"
        query += " ORDER BY updated_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> dict[str, Any]:
        manifest = json.loads(row["manifest_json"] or "{}")
        graph = manifest.get("graph") or {}
        return {
            "template_id": row["template_id"],
            "name": row["name"],
            "description": row["description"],
            "status": row["status"],
            "revision": int(row["revision"]),
            "created_by": row["created_by"],
            "updated_at": row["updated_at"],
            "prompt": manifest.get("prompt", ""),
            "skills": manifest.get("skills", []),
            "graph_node_count": len(graph.get("nodes", [])),
            "manifest": manifest,
        }


__all__ = ["TemplateStore"]
