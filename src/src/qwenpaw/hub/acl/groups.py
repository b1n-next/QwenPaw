# -*- coding: utf-8 -*-
"""Group membership and policy storage for the ACL engine (05 §2).

Backs the EP-2-1 upgrade: subjects (user / group / role) carry
explicit allow/deny policies over resources; the engine evaluates
them ahead of the static rule table, with user > group > role
specificity and the fail-closed defaults still as the floor.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Tuple

from ..database import connect_hub_database, utc_now

VALID_SUBJECT_KINDS = ("user", "group", "role")
VALID_EFFECTS = ("allow", "deny")


@dataclass(frozen=True)
class Policy:
    """One subject→resource→effect row."""

    policy_id: str
    subject: str  # "user:<id>" | "group:<name>" | "role:<role>"
    resource: str  # "apigroup:<name>" | "menu:<name>" | ...
    effect: str  # "allow" | "deny"

    @property
    def subject_kind(self) -> str:
        return self.subject.split(":", 1)[0]

    @property
    def subject_value(self) -> str:
        return self.subject.split(":", 1)[1] if ":" in self.subject else ""


def _subject(kind: str, value: str) -> str:
    if kind not in VALID_SUBJECT_KINDS:
        raise ValueError(f"Invalid subject kind: {kind}")
    if not value or ":" in value:
        raise ValueError(f"Invalid subject value: {value!r}")
    return f"{kind}:{value}"


def _validate_resource(resource: str) -> None:
    if not resource or ":" not in resource or len(resource) > 256:
        raise ValueError(f"Invalid resource: {resource!r}")


class GroupPolicyStore:
    """CRUD over groups / group_members / policies."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        return connect_hub_database(self._database_path)

    # -------------------------------------------------- groups

    def create_group(self, name: str, *, source: str = "local") -> str:
        """Create a group; returns its generated id."""
        if not name or len(name) > 128 or ":" in name:
            raise ValueError(f"Invalid group name: {name!r}")
        group_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO groups(group_id, name, source, created_at) "
                "VALUES (?, ?, ?, ?)",
                (group_id, name, source, utc_now()),
            )
        return group_id

    def list_groups(self) -> List[dict]:
        """All groups with member counts."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT g.group_id, g.name, g.source, g.created_at,
                       COUNT(m.user_id) AS member_count
                FROM groups g
                LEFT JOIN group_members m ON m.group_id = g.group_id
                GROUP BY g.group_id
                ORDER BY g.name
                """,
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_group(self, group_id: str) -> bool:
        """Drop a group and its memberships/policies by name."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT name FROM groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "DELETE FROM group_members WHERE group_id = ?",
                (group_id,),
            )
            connection.execute(
                "DELETE FROM policies WHERE subject = ?",
                (f"group:{row['name']}",),
            )
            connection.execute(
                "DELETE FROM groups WHERE group_id = ?",
                (group_id,),
            )
        return True

    def add_member(self, group_id: str, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO group_members"
                "(group_id, user_id, added_at) VALUES (?, ?, ?)",
                (group_id, user_id, utc_now()),
            )

    def remove_member(self, group_id: str, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM group_members WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )

    def group_names_for(self, user_id: str) -> Tuple[str, ...]:
        """Ordered group names a user belongs to (empty for none)."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT g.name FROM groups g
                JOIN group_members m ON m.group_id = g.group_id
                WHERE m.user_id = ?
                ORDER BY g.name
                """,
                (user_id,),
            ).fetchall()
        return tuple(row["name"] for row in rows)

    # -------------------------------------------------- policies

    def create_policy(
        self,
        *,
        subject_kind: str,
        subject_value: str,
        resource: str,
        effect: str,
    ) -> str:
        """Insert one policy row; returns its generated id."""
        if effect not in VALID_EFFECTS:
            raise ValueError(f"Invalid effect: {effect}")
        _validate_resource(resource)
        subject = _subject(subject_kind, subject_value)
        policy_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO policies(policy_id, subject, resource, effect, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (policy_id, subject, resource, effect, utc_now()),
            )
        return policy_id

    def delete_policy(self, policy_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM policies WHERE policy_id = ?",
                (policy_id,),
            )
        return cursor.rowcount > 0

    def list_policies(self) -> List[Policy]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT policy_id, subject, resource, effect "
                "FROM policies ORDER BY subject, resource",
            ).fetchall()
        return [
            Policy(
                policy_id=row["policy_id"],
                subject=row["subject"],
                resource=row["resource"],
                effect=row["effect"],
            )
            for row in rows
        ]

    def policies_for(
        self,
        *,
        user_id: str,
        groups: Iterable[str],
        role: str,
    ) -> Tuple[Policy, ...]:
        """Policies matching one subject (user > group > role order)."""
        subjects = [_subject("user", user_id)]
        subjects.extend(_subject("group", name) for name in groups)
        subjects.append(_subject("role", role))
        placeholders = ",".join("?" for _ in subjects)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT policy_id, subject, resource, effect
                FROM policies WHERE subject IN ({placeholders})
                """,
                tuple(subjects),
            ).fetchall()
        order = {subject: rank for rank, subject in enumerate(subjects)}
        found = {
            row["policy_id"]: Policy(
                policy_id=row["policy_id"],
                subject=row["subject"],
                resource=row["resource"],
                effect=row["effect"],
            )
            for row in rows
        }
        return tuple(
            sorted(
                found.values(),
                key=lambda policy: (
                    order[policy.subject],
                    policy.resource,
                ),
            ),
        )


__all__ = ["GroupPolicyStore", "Policy"]
