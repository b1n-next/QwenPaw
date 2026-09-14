# -*- coding: utf-8 -*-
"""SQLite storage for the organization policy baseline (EP-2-13)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

_BASELINE_ID = "default"

# Mirror of runtime GovernanceAction values the baseline may use.
_ALLOWED_ACTIONS = frozenset({"allow", "deny", "ask"})


def canonical_rule_digest(rules: list[dict[str, Any]]) -> str:
    """Hash the rule list deterministically (runtime verifies this)."""
    canonical = json.dumps(
        rules,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PolicyCatalogStore:
    """Single-row organization baseline with revision + digest."""

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
                CREATE TABLE IF NOT EXISTS hub_policy_baseline (
                    baseline_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL DEFAULT 0,
                    rules_json TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    updated_by TEXT,
                    updated_at TEXT
                )
                """,
            )

    # ----------------------------------------------------------- writes

    def update_baseline(
        self,
        rules: list[dict[str, Any]],
        *,
        updated_by: str,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Validate and store a new baseline revision."""
        normalized = self._validate_rules(rules)
        digest = canonical_rule_digest(normalized)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT revision FROM hub_policy_baseline "
                "WHERE baseline_id = ?",
                (_BASELINE_ID,),
            ).fetchone()
            revision = (row["revision"] + 1) if row else 1
            from datetime import datetime, timezone

            connection.execute(
                """
                INSERT INTO hub_policy_baseline(
                    baseline_id, revision, rules_json, sha256, enabled,
                    updated_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(baseline_id) DO UPDATE SET
                    revision = excluded.revision,
                    rules_json = excluded.rules_json,
                    sha256 = excluded.sha256,
                    enabled = excluded.enabled,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (
                    _BASELINE_ID,
                    revision,
                    json.dumps(
                        normalized,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    digest,
                    1 if enabled else 0,
                    updated_by,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return self.get_baseline() or {}

    @staticmethod
    def _validate_rules(rules: object) -> list[dict[str, Any]]:
        if not isinstance(rules, list):
            raise ValueError("baseline rules must be a list")
        if len(rules) > 500:
            raise ValueError("baseline rules exceed the 500-rule cap")
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(rules):
            if not isinstance(item, dict):
                raise ValueError(f"rule #{index} must be an object")
            match = item.get("match")
            if not isinstance(match, str) or not match.strip():
                raise ValueError(f"rule #{index} is missing 'match'")
            action = str(item.get("action", "")).lower()
            if action not in _ALLOWED_ACTIONS:
                allowed = ", ".join(sorted(_ALLOWED_ACTIONS))
                raise ValueError(
                    f"rule #{index} action must be one of {allowed}",
                )
            normalized.append(
                {
                    "match": match.strip(),
                    "action": action,
                    "reason": str(item.get("reason") or ""),
                    "grantee": str(item.get("grantee") or "*"),
                    "duration": str(item.get("duration") or ""),
                },
            )
        return normalized

    # ------------------------------------------------------------ reads

    def get_baseline(self) -> dict[str, Any] | None:
        """Return the current baseline row (rules parsed)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM hub_policy_baseline WHERE baseline_id = ?",
                (_BASELINE_ID,),
            ).fetchone()
        if row is None:
            return None
        return {
            "revision": int(row["revision"]),
            "enabled": bool(row["enabled"]),
            "sha256": str(row["sha256"]),
            "rules": json.loads(row["rules_json"]),
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
        }

    def baseline_payload(self) -> dict[str, Any] | None:
        """Provisioner-facing payload (None when disabled/absent)."""
        baseline = self.get_baseline()
        if not baseline or not baseline["enabled"]:
            return None
        return {
            "revision": baseline["revision"],
            "sha256": baseline["sha256"],
            "rules": baseline["rules"],
        }
