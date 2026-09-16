# -*- coding: utf-8 -*-
"""Phase 3 (G-P14): Redis session backend tests (injected fake client)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from qwenpaw.app.chats.session_store_redis import RedisSession
from qwenpaw.app.chats.session_store import build_session_store
from qwenpaw.exceptions import AgentStateError


class _FakePipeline:
    def __init__(self, store: "_FakeRedis") -> None:
        self._store = store
        self._ops: list[tuple[str, list[Any]]] = []

    def set(self, key: str, value: str) -> "_FakePipeline":
        self._ops.append(("set", [key, value]))
        return self

    def hset(self, key: str, field: str, value: str) -> "_FakePipeline":
        self._ops.append(("hset", [key, field, value]))
        return self

    def sadd(self, key: str, member: str) -> "_FakePipeline":
        self._ops.append(("sadd", [key, member]))
        return self

    def delete(self, key: str) -> "_FakePipeline":
        self._ops.append(("del", [key]))
        return self

    def srem(self, key: str, member: str) -> "_FakePipeline":
        self._ops.append(("srem", [key, member]))
        return self

    def hgetall(self, key: str) -> "_FakePipeline":
        self._ops.append(("hgetall", [key]))
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for op, args in self._ops:
            if op == "set":
                self._store.strings[args[0]] = args[1]
                results.append(True)
            elif op == "hset":
                self._store.hashes.setdefault(args[0], {})[args[1]] = args[2]
                results.append(1)
            elif op == "sadd":
                self._store.sets.setdefault(args[0], set()).add(args[1])
                results.append(1)
            elif op == "del":
                removed = 1 if self._store.strings.pop(args[0], None) else 0
                results.append(removed)
            elif op == "srem":
                members = self._store.sets.get(args[0], set())
                existed = args[1] in members
                members.discard(args[1])
                results.append(1 if existed else 0)
            elif op == "hgetall":
                results.append(dict(self._store.hashes.get(args[0], {})))
        self._ops = []
        return results


class _FakeRedis:
    """Dict-backed async surface used by RedisSession."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set] = {}

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def get(self, key: str) -> Any:
        return self.strings.get(key)

    async def smembers(self, key: str) -> set:
        return set(self.sets.get(key, set()))


class _Module:
    def __init__(self) -> None:
        self._state: dict = {}

    def state_dict(self) -> dict:
        return dict(self._state)

    def load_state_dict(self, state: dict) -> None:
        self._state = dict(state)


@pytest.fixture(name="store")
def _store() -> RedisSession:
    return RedisSession(client=_FakeRedis())


# Stand-in modules read their own state dict via the same private
# attribute the real state modules expose.
# pylint: disable=protected-access


def _store_data(store: RedisSession) -> _FakeRedis:
    return store._client  # pylint: disable=protected-access


def test_save_load_roundtrip(store: RedisSession) -> None:
    module = _Module()
    module._state = {"messages": ["hi"], "count": 1}
    asyncio.run(
        store.save_session_state(
            "s1",
            user_id="u",
            channel="web",
            agent=module,
        ),
    )

    hydrated = _Module()
    asyncio.run(
        store.load_session_state(
            "s1",
            user_id="u",
            channel="web",
            agent=hydrated,
        ),
    )
    assert hydrated._state == {"messages": ["hi"], "count": 1}


def test_channel_isolation(store: RedisSession) -> None:
    module = _Module()
    module._state = {"v": "web"}
    asyncio.run(
        store.save_session_state(
            "s1",
            user_id="u",
            channel="web",
            agent=module,
        ),
    )
    other = _Module()
    asyncio.run(
        store.load_session_state(
            "s1",
            user_id="u",
            channel="sip",
            agent=other,
        ),
    )
    assert not other._state


def test_load_missing_raises_when_required(store: RedisSession) -> None:
    module = _Module()
    with pytest.raises(AgentStateError):
        asyncio.run(
            store.load_session_state(
                "ghost",
                user_id="u",
                channel="web",
                allow_not_exist=False,
                agent=module,
            ),
        )


def test_update_nested_then_read(store: RedisSession) -> None:
    asyncio.run(
        store.save_session_state("s2", user_id="u", channel="web"),
    )
    asyncio.run(
        store.update_session_state(
            "s2",
            "memory.facts.latest",
            {"k": "v"},
            user_id="u",
            channel="web",
        ),
    )
    state = asyncio.run(
        store.get_session_state_dict("s2", user_id="u", channel="web"),
    )
    assert state["memory"]["facts"]["latest"] == {"k": "v"}


def test_update_requires_existing_when_asked(store: RedisSession) -> None:
    with pytest.raises(AgentStateError):
        asyncio.run(
            store.update_session_state(
                "ghost",
                "k",
                1,
                user_id="u",
                channel="web",
                create_if_not_exist=False,
            ),
        )


def test_second_store_sees_writes() -> None:
    shared = _FakeRedis()
    first = RedisSession(client=shared)
    second = RedisSession(client=shared)
    asyncio.run(
        first.save_session_state(
            "s3",
            user_id="u",
            channel="web",
            agent=_Module(),
        ),
    )
    listing = asyncio.run(second.list_sessions(user_id="u"))
    assert [row["session_id"] for row in listing] == ["s3"]


def test_delete_and_list(store: RedisSession) -> None:
    asyncio.run(
        store.save_session_state("s4", user_id="u1", channel="web"),
    )
    asyncio.run(
        store.save_session_state("s5", user_id="u2", channel="web"),
    )
    rows = asyncio.run(store.list_sessions())
    assert {row["session_id"] for row in rows} == {"s4", "s5"}
    assert asyncio.run(
        store.delete_session_state("s4", user_id="u1", channel="web"),
    )
    rows = asyncio.run(store.list_sessions())
    assert [row["session_id"] for row in rows] == ["s5"]
    assert not asyncio.run(
        store.delete_session_state("s4", user_id="u1", channel="web"),
    )


def test_list_filters_by_user_and_channel(store: RedisSession) -> None:
    asyncio.run(store.save_session_state("a", user_id="u1", channel="web"))
    asyncio.run(store.save_session_state("b", user_id="u2", channel="web"))
    asyncio.run(store.save_session_state("c", user_id="u1", channel="sip"))
    assert [
        row["session_id"]
        for row in asyncio.run(store.list_sessions(user_id="u1"))
    ] and all(
        row["user_id"] == "u1"
        for row in asyncio.run(store.list_sessions(user_id="u1"))
    )
    only_web = asyncio.run(store.list_sessions(channel="web"))
    assert {row["session_id"] for row in only_web} == {"a", "b"}


def test_state_is_json_serialized(store: RedisSession) -> None:
    asyncio.run(store.save_session_state("s6", user_id="u", channel="web"))
    keys = [k for k in _store_data(store).strings if k.endswith("s6")]
    assert keys, "data key written"
    payload = json.loads(_store_data(store).strings[keys[0]])
    assert payload == {}


def test_factory_selects_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import types

    fake_module = types.ModuleType("redis")
    fake_async = types.ModuleType("redis.asyncio")
    fake_async.from_url = lambda _url, **_kw: _FakeRedis()
    fake_module.asyncio = fake_async
    monkeypatch.setitem(sys.modules, "redis", fake_module)
    monkeypatch.setitem(sys.modules, "redis.asyncio", fake_async)
    monkeypatch.setenv("QWENPAW_SESSION_STORE", "redis")
    monkeypatch.setenv("QWENPAW_SESSION_REDIS_URL", "redis://r:6379/2")
    built = build_session_store()
    assert isinstance(built, RedisSession)


def test_factory_default_stays_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QWENPAW_SESSION_STORE", raising=False)
    from qwenpaw.app.chats.session import SafeJSONSession

    assert isinstance(build_session_store(), SafeJSONSession)
