# -*- coding: utf-8 -*-
"""Redis-backed session state store (Phase 3, G-P14 enhancement).

Drop-in alternative to :class:`SafeJSONSession` / :class:`SqliteSession`
with the same async surface. State payloads live in Redis as one JSON
string per (channel, user_id, session_id) key, with a per-session-id
index set for listings. Designed for multi-node runtime fleets where
even a shared volume is not an option.

The ``redis`` client is an optional dependency: import it lazily and
fail with a pointed ConfigurationException so deployments that never
opt in see zero new requirements. Enable via:

    QWENPAW_SESSION_STORE=redis
    QWENPAW_SESSION_REDIS_URL=redis://[:password@]host:port/db
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Sequence, Union

from ...exceptions import AgentStateError, ConfigurationException

_PREFIX = "qwenpaw:session"
_KEY_SEPARATOR = "\x1f"


def _missing_client_error(url: str) -> ConfigurationException:
    return ConfigurationException(
        config_key="QWENPAW_SESSION_REDIS_URL",
        message=(
            "QWENPAW_SESSION_STORE=redis requires the optional "
            "'redis' package (pip install 'qwenpaw[sessions]' or "
            "pip install redis>=5). Configured URL: "
            f"{url or '(default redis://localhost:6379/0)'}"
        ),
    )


class RedisSession:
    """Redis session store mirroring SafeJSONSession's surface."""

    def __init__(self, redis_url: str = "", client: Any = None) -> None:
        # ``client`` injection keeps tests dependency-free.
        if client is not None:
            self._client = client
        else:
            try:
                # pylint: disable=import-outside-toplevel
                from redis import asyncio as aioredis
            except ImportError as exc:  # pragma: no cover - env-specific
                raise _missing_client_error(redis_url) from exc
            self._client = aioredis.from_url(
                redis_url or "redis://localhost:6379/0",
                decode_responses=True,
            )

    # ------------------------------------------------------ keying

    @staticmethod
    def _data_key(session_id: str, user_id: str, channel: str) -> str:
        return (
            f"{_PREFIX}:data:"
            f"{channel}{_KEY_SEPARATOR}{user_id}{_KEY_SEPARATOR}"
            f"{session_id}"
        )

    @staticmethod
    def _meta(session_id: str, user_id: str, channel: str) -> dict:
        return {
            "session_id": session_id,
            "user_id": user_id,
            "channel": channel,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _index_key(session_id: str) -> str:
        return f"{_PREFIX}:index:{session_id}"

    # --------------------------------------------------- internals

    async def _read(self, session_id: str, user_id: str, channel: str) -> dict:
        raw = await self._client.get(
            self._data_key(session_id, user_id, channel),
        )
        if raw is None:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    async def _write(
        self,
        session_id: str,
        user_id: str,
        channel: str,
        states: dict,
    ) -> None:
        meta = self._meta(session_id, user_id, channel)
        pipe = self._client.pipeline()
        pipe.set(
            self._data_key(session_id, user_id, channel),
            json.dumps(states, ensure_ascii=False),
        )
        pipe.hset(
            self._index_key(session_id),
            meta["updated_at"],
            json.dumps(meta),
        )
        pipe.sadd(f"{_PREFIX}:sessions", session_id)
        await pipe.execute()

    # ------------------------------------------------ async surface

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
        await self._write(session_id, user_id, channel, states)

    async def load_session_state(
        self,
        session_id: str,
        user_id: str = "",
        channel: str = "",
        allow_not_exist: bool = True,
        **state_modules_mapping,
    ) -> None:
        """Hydrate state modules from the stored payload."""
        states = await self._read(session_id, user_id, channel)
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
        states = await self._read(session_id, user_id, channel)
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
        await self._write(session_id, user_id, channel, states)

    async def get_session_state_dict(
        self,
        session_id: str,
        user_id: str = "",
        channel: str = "",
        allow_not_exist: bool = True,
    ) -> dict:
        """Return the raw stored state dict ({} when absent)."""
        states = await self._read(session_id, user_id, channel)
        if states or allow_not_exist:
            return states
        raise AgentStateError(
            session_id=session_id,
            message=(
                f"No stored session state for {session_id!r} "
                f"(user={user_id!r}, channel={channel!r})"
            ),
        )

    # ------------------------------------------------ sync helpers

    async def delete_session_state(
        self,
        session_id: str,
        user_id: str = "",
        channel: str = "",
    ) -> bool:
        """Remove one stored payload; True when something was deleted."""
        pipe = self._client.pipeline()
        data_key = self._data_key(session_id, user_id, channel)
        pipe.delete(data_key)
        pipe.delete(self._index_key(session_id))
        pipe.srem(f"{_PREFIX}:sessions", session_id)
        results = await pipe.execute()
        return bool(results and results[0])

    async def list_sessions(
        self,
        *,
        user_id: str = "",
        channel: str = "",
    ) -> list[dict]:
        """Stored session metadata rows (newest first)."""
        session_ids = await self._client.smembers(f"{_PREFIX}:sessions")
        metas: list[dict] = []
        pipe = self._client.pipeline()
        index_keys = []
        for session_id in session_ids:
            index_key = self._index_key(session_id)
            index_keys.append(session_id)
            pipe.hgetall(index_key)
        rows = await pipe.execute()
        for session_id, entries in zip(index_keys, rows):
            for _stamp, raw in entries.items():
                try:
                    meta = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if user_id and meta.get("user_id") != user_id:
                    continue
                if channel and meta.get("channel") != channel:
                    continue
                metas.append(meta)
        metas.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return metas


__all__ = ["RedisSession"]
