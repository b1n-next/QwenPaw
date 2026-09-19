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
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

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
    expires_at: Optional[str] = None  # C8 delegation window

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


_MAX_GROUP_DEPTH = 4


def _policy_expired(expires_at: Optional[str]) -> bool:
    """True when a C8 delegation window has passed (fail-closed)."""
    if not expires_at:
        return False
    try:
        moment = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return True  # unparseable expiry never grants
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment <= datetime.now(timezone.utc)


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

    def create_group(
        self,
        name: str,
        *,
        source: str = "local",
        parent_group_id: Optional[str] = None,
    ) -> str:
        """Create a group; returns its generated id.

        ``parent_group_id`` (C6) nests it under one parent — up to
        four levels (tenant→department→team→subteam); cycles and
        self-parenting are rejected.
        """
        if not name or len(name) > 128 or ":" in name:
            raise ValueError(f"Invalid group name: {name!r}")
        with self._connect() as connection:
            if parent_group_id:
                if parent_group_id == name:
                    raise ValueError("group cannot parent itself")
                depth = self._lineage_depth(
                    connection,
                    parent_group_id,
                )
                if depth is None:
                    raise ValueError("parent group not found")
                if depth + 1 >= _MAX_GROUP_DEPTH:
                    raise ValueError(
                        f"group nesting deeper than {_MAX_GROUP_DEPTH}",
                    )
            group_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO groups(group_id, name, source, parent_id, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (group_id, name, source, parent_group_id, utc_now()),
            )
        return group_id

    @staticmethod
    def _lineage_depth(
        connection: sqlite3.Connection,
        group_id: str,
    ) -> Optional[int]:
        """Depth of one node in its chain (0 = root); None if absent
        or if the chain is corrupt/cyclic."""
        seen = set()
        depth = 0
        current = group_id
        while current is not None:
            if current in seen:
                return None
            seen.add(current)
            row = connection.execute(
                "SELECT parent_id FROM groups WHERE group_id = ?",
                (current,),
            ).fetchone()
            if row is None:
                return None
            current = row["parent_id"]
            depth += 1
            if depth > _MAX_GROUP_DEPTH * 2:
                return None
        return depth - 1

    def ancestor_group_ids(self, group_id: str) -> List[str]:
        """Chain of ancestor ids (nearest first), cycle-safe."""
        with self._connect() as connection:
            chain: List[str] = []
            seen = {group_id}
            current_row = connection.execute(
                "SELECT parent_id FROM groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            while current_row is not None:
                parent = current_row["parent_id"]
                if not parent or parent in seen:
                    break
                seen.add(parent)
                chain.append(parent)
                current_row = connection.execute(
                    "SELECT parent_id FROM groups WHERE group_id = ?",
                    (parent,),
                ).fetchone()
        return chain

    def descendant_member_ids(self, group_name: str) -> List[str]:
        """Members of one group AND all its descendants (C6 quotas,
        department totals include sub-teams)."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT group_id, name FROM groups",
            ).fetchall()
        by_id = {str(row["group_id"]): str(row["name"]) for row in rows}
        root_id = next(
            (gid for gid, name in by_id.items() if name == group_name),
            None,
        )
        if root_id is None:
            return []
        children: dict[str, List[str]] = {}
        with self._connect() as connection:
            for row in connection.execute(
                "SELECT group_id, parent_id FROM groups "
                "WHERE parent_id IS NOT NULL",
            ).fetchall():
                children.setdefault(str(row["parent_id"]), []).append(
                    str(row["group_id"]),
                )
        subtree = [root_id]
        frontier = [root_id]
        seen = {root_id}
        while frontier:
            nxt: List[str] = []
            for gid in frontier:
                for child in children.get(gid, []):
                    if child not in seen:
                        seen.add(child)
                        subtree.append(child)
                        nxt.append(child)
            frontier = nxt
        members: List[str] = []
        query_members = "SELECT user_id FROM group_members WHERE group_id = ?"
        with self._connect() as connection:
            for gid in subtree:
                rows2 = connection.execute(
                    query_members,
                    (gid,),
                ).fetchall()
                members.extend(str(r["user_id"]) for r in rows2)
        return sorted(set(members))

    def list_groups(self) -> List[dict]:
        """All groups with member counts."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT g.group_id, g.name, g.source, g.parent_id,
                       g.created_at,
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

    def member_ids(self, group_name: str) -> List[str]:
        """User ids belonging to one group by name (G3 quota sums)."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.user_id FROM groups g
                JOIN group_members m ON m.group_id = g.group_id
                WHERE g.name = ?
                ORDER BY m.user_id
                """,
                (group_name,),
            ).fetchall()
        return [str(row["user_id"]) for row in rows]

    # -------------------------------------------------- policies

    def create_policy(
        self,
        *,
        subject_kind: str,
        subject_value: str,
        resource: str,
        effect: str,
        expires_at: Optional[str] = None,
    ) -> str:
        """Insert one policy row; returns its generated id.

        ``expires_at`` (C8, ISO-8601 with offset) scopes the grant to
        a delegation window; once passed the policy stops matching
        entirely (fail-closed: expired rows are invisible everywhere).
        """
        if effect not in VALID_EFFECTS:
            raise ValueError(f"Invalid effect: {effect}")
        _validate_resource(resource)
        subject = _subject(subject_kind, subject_value)
        policy_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO policies(policy_id, subject, resource, "
                "effect, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    policy_id,
                    subject,
                    resource,
                    effect,
                    expires_at,
                    utc_now(),
                ),
            )
        return policy_id

    def purge_expired_policies(self) -> int:
        """Delete already-expired policy rows (housekeeping)."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM policies WHERE expires_at IS NOT NULL "
                "AND expires_at <= ?",
                (utc_now(),),
            )
        return cursor.rowcount

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
                "SELECT policy_id, subject, resource, effect, expires_at "
                "FROM policies ORDER BY subject, resource",
            ).fetchall()
        return [
            Policy(
                policy_id=row["policy_id"],
                subject=row["subject"],
                resource=row["resource"],
                effect=row["effect"],
                expires_at=row["expires_at"],
            )
            for row in rows
            if not _policy_expired(row["expires_at"])
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
        for name in groups:
            subjects.append(_subject("group", name))
            # C6: membership in a nested team also inherits the
            # department/tenant policies up the chain.
            row = None
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT group_id FROM groups WHERE name = ?",
                    (name,),
                ).fetchone()
            if row is not None:
                for ancestor_id in self.ancestor_group_ids(
                    str(row["group_id"]),
                ):
                    anc = None
                    with self._connect() as connection:
                        anc = connection.execute(
                            "SELECT name FROM groups WHERE group_id = ?",
                            (ancestor_id,),
                        ).fetchone()
                    if anc is not None:
                        subjects.append(
                            _subject("group", str(anc["name"])),
                        )
        subjects.append(_subject("role", role))
        placeholders = ",".join("?" for _ in subjects)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT policy_id, subject, resource, effect, expires_at
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
                expires_at=row["expires_at"],
            )
            for row in rows
            if not _policy_expired(row["expires_at"])
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
