# -*- coding: utf-8 -*-
"""SQLite-backed session state store (Phase 3, G-P14).

Drop-in alternative to :class:`SafeJSONSession` with the same async
surface (save / load / update / get). State rows live in one SQLite
file under WAL, so several processes on a shared volume — runtime
replicas, hub provisioners, repair tooling — see the same sessions
without NFS-safe JSON dances.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence, Union

from ...exceptions import AgentStateError, ConfigurationException

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_states (
    session_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    state_json TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_states_session
    ON session_states(session_id);
"""

_ENV_BACKEND = "QWENPAW_SESSION_STORE"
_ENV_DB_PATH = "QWENPAW_SESSION_DB"
_ENV_REDIS_URL = "QWENPAW_SESSION_REDIS_URL"
_DEFAULT_DB_NAME = "sessions.db"


def _db_path() -> Path:
    from ...constant import WORKING_DIR

    raw = os.environ.get(_ENV_DB_PATH, "").strip()
    if raw:
        return Path(raw)
    return Path(WORKING_DIR) / _DEFAULT_DB_NAME


def build_session_store():
    """Pick the configured session store.

    ``QWENPAW_SESSION_STORE=sqlite`` selects the SQLite backend
    (``QWENPAW_SESSION_DB`` overrides the database path);
    ``QWENPAW_SESSION_STORE=redis`` selects the Redis backend
    (``QWENPAW_SESSION_REDIS_URL`` overrides the connection URL,
    optional 'redis' dependency required); anything else keeps the
    stock JSON-file store — zero behavior change by default.
    """
    backend = os.environ.get(_ENV_BACKEND, "").strip().lower()
    if backend == "sqlite":
        return SqliteSession(database_path=_db_path())
    if backend == "redis":
        from .session_store_redis import RedisSession

        return RedisSession(
            redis_url=os.environ.get(_ENV_REDIS_URL, "").strip(),
        )
    from .session import SafeJSONSession
    from ...constant import WORKING_DIR

    return SafeJSONSession(save_dir=str(WORKING_DIR))


class SqliteSession:
    """SQLite session store mirroring SafeJSONSession's surface."""

    def __init__(self, database_path: Path | str) -> None:
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

    @staticmethod
    def _key(session_id: str, user_id: str, channel: str) -> str:
        return f"{channel}\x1f{user_id}\x1f{session_id}"

    def _read(self, session_id: str, user_id: str, channel: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM session_states WHERE session_key = ?",
                (self._key(session_id, user_id, channel),),
            ).fetchone()
        if row is None:
            return {}
        try:
            payload = json.loads(row["state_json"])
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write(
        self,
        session_id: str,
        user_id: str,
        channel: str,
        states: dict,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO session_states(
                    session_key, session_id, user_id, channel,
                    state_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (
                    self._key(session_id, user_id, channel),
                    session_id,
                    user_id,
                    channel,
                    json.dumps(states, ensure_ascii=False),
                    now,
                ),
            )

    # -------------------------------------------------- async surface

    async def save_session_state(
        self,
        session_id: str,
        user_id: str = "",
        channel: str = "",
        **state_modules_mapping,
    ) -> None:
        """Persist ``state_module.state_dict()`` payloads."""
        states = {
            name: state_module.state_dict()
            for name, state_module in state_modules_mapping.items()
        }
        self._write(session_id, user_id, channel, states)

    async def load_session_state(
        self,
        session_id: str,
        user_id: str = "",
        channel: str = "",
        allow_not_exist: bool = True,
        **state_modules_mapping,
    ) -> None:
        """Hydrate state modules from the stored row."""
        states = self._read(session_id, user_id, channel)
        if states:
            for name, state_module in state_modules_mapping.items():
                if name in states:
                    state_module.load_state_dict(states[name])
            return
        if not allow_not_exist:
            raise AgentStateError(
                session_id=session_id,
                message=(
                    f"No stored session state for {session_id!r} "
                    f"(user={user_id!r}, channel={channel!r})"
                ),
            )

    async def update_session_state(
        self,
        session_id: str,
        key: Union[str, Sequence[str]],
        value,
        user_id: str = "",
        channel: str = "",
        create_if_not_exist: bool = True,
    ) -> None:
        """Deep-merge one dotted key path into the stored state."""
        path = key.split(".") if isinstance(key, str) else list(key)
        if not path:
            raise ConfigurationException(
                config_key="session.key",
                message="key path is empty",
            )
        states = self._read(session_id, user_id, channel)
        if not states and not create_if_not_exist:
            raise AgentStateError(
                session_id=session_id,
                message=(
                    f"No stored session state for {session_id!r} "
                    f"(user={user_id!r}, channel={channel!r})"
                ),
            )
        cursor = states
        for step in path[:-1]:
            if step not in cursor or not isinstance(cursor[step], dict):
                cursor[step] = {}
            cursor = cursor[step]
        cursor[path[-1]] = value
        self._write(session_id, user_id, channel, states)

    async def get_session_state_dict(
        self,
        session_id: str,
        user_id: str = "",
        channel: str = "",
        allow_not_exist: bool = True,
    ) -> dict:
        """Return the raw stored state dict ({} when absent)."""
        states = self._read(session_id, user_id, channel)
        if states or allow_not_exist:
            return states
        raise AgentStateError(
            session_id=session_id,
            message=(
                f"No stored session state for {session_id!r} "
                f"(user={user_id!r}, channel={channel!r})"
            ),
        )

    # -------------------------------------------------- sync helpers

    def delete_session_state(
        self,
        session_id: str,
        user_id: str = "",
        channel: str = "",
    ) -> bool:
        """Remove one stored session row."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM session_states WHERE session_key = ?",
                (self._key(session_id, user_id, channel),),
            )
        return cursor.rowcount > 0

    def list_sessions(
        self,
        *,
        user_id: str = "",
        channel: str = "",
    ) -> list[dict]:
        """Stored session metadata rows."""
        query = (
            "SELECT session_id, user_id, channel, updated_at "
            "FROM session_states"
        )
        clauses: list[str] = []
        params: list[str] = []
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]


__all__ = ["SqliteSession", "build_session_store"]
