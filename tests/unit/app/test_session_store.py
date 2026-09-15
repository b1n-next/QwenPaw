# -*- coding: utf-8 -*-
"""Phase 3 (G-P14): SQLite session store + factory tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from qwenpaw.app.chats.session import SafeJSONSession
from qwenpaw.app.chats.session_store import (
    SqliteSession,
    build_session_store,
)


class _Module:
    """Minimal state module standing in for an agent."""

    def __init__(self) -> None:
        self.data: dict = {}

    def state_dict(self) -> dict:
        return self.data

    def load_state_dict(self, payload: dict) -> None:
        self.data = payload


@pytest.fixture(name="store")
def _store(tmp_path: Path) -> SqliteSession:
    return SqliteSession(tmp_path / "sessions.db")


def test_save_load_roundtrip(store: SqliteSession) -> None:
    agent = _Module()
    agent.data = {"turns": 3, "memory": ["hello"]}

    asyncio.run(
        store.save_session_state("s1", user_id="u1", agent=agent),
    )

    revived = _Module()
    asyncio.run(
        store.load_session_state("s1", user_id="u1", agent=revived),
    )
    assert revived.data == agent.data


def test_channel_isolation(store: SqliteSession) -> None:
    first = _Module()
    first.data = {"v": 1}
    second = _Module()
    second.data = {"v": 2}
    asyncio.run(
        store.save_session_state("s", user_id="u", channel="web", agent=first),
    )
    asyncio.run(
        store.save_session_state(
            "s", user_id="u", channel="sip", agent=second
        ),
    )

    web = _Module()
    sip = _Module()
    asyncio.run(
        store.load_session_state("s", user_id="u", channel="web", agent=web),
    )
    asyncio.run(
        store.load_session_state("s", user_id="u", channel="sip", agent=sip),
    )
    assert web.data == {"v": 1}
    assert sip.data == {"v": 2}


def test_load_missing_behaves_like_json_store(
    tmp_path: Path,
    store: SqliteSession,
) -> None:
    json_store = SafeJSONSession(save_dir=str(tmp_path / "json"))

    module = _Module()
    asyncio.run(store.load_session_state("ghost", agent=module))
    assert module.data == {}

    module2 = _Module()
    asyncio.run(json_store.load_session_state("ghost", agent=module2))
    assert module2.data == {}


def test_update_and_get_nested(store: SqliteSession) -> None:
    asyncio.run(
        store.update_session_state(
            "s1",
            "mode.flags.verbose",
            True,
            user_id="u1",
        ),
    )
    states = asyncio.run(
        store.get_session_state_dict("s1", user_id="u1"),
    )
    assert states["mode"]["flags"]["verbose"] is True

    asyncio.run(
        store.update_session_state(
            "s1",
            ["mode", "flags", "level"],
            2,
            user_id="u1",
        ),
    )
    states = asyncio.run(
        store.get_session_state_dict("s1", user_id="u1"),
    )
    assert states["mode"]["flags"] == {"verbose": True, "level": 2}


def test_update_requires_existing_when_asked(store: SqliteSession) -> None:
    from qwenpaw.exceptions import AgentStateError

    with pytest.raises(AgentStateError):
        asyncio.run(
            store.update_session_state(
                "ghost",
                "a.b",
                1,
                user_id="u",
                create_if_not_exist=False,
            ),
        )


def test_second_connection_sees_writes(
    store: SqliteSession,
    tmp_path: Path,
) -> None:
    """Shared-volume semantics: a fresh handle observes prior writes."""
    agent = _Module()
    agent.data = {"shared": True}
    asyncio.run(
        store.save_session_state("s1", user_id="u", agent=agent),
    )

    other = SqliteSession(tmp_path / "sessions.db")
    revived = _Module()
    asyncio.run(
        other.load_session_state("s1", user_id="u", agent=revived),
    )
    assert revived.data == {"shared": True}
    assert other.list_sessions(user_id="u")[0]["session_id"] == "s1"


def test_delete_and_list(store: SqliteSession) -> None:
    for index in range(3):
        agent = _Module()
        agent.data = {"i": index}
        asyncio.run(
            store.save_session_state(f"s{index}", user_id="u", agent=agent),
        )
    assert len(store.list_sessions(user_id="u")) == 3

    assert store.delete_session_state("s1", user_id="u") is True
    assert store.delete_session_state("s1", user_id="u") is False
    remaining = {row["session_id"] for row in store.list_sessions(user_id="u")}
    assert remaining == {"s0", "s2"}


def test_factory_defaults_to_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QWENPAW_SESSION_STORE", raising=False)
    monkeypatch.chdir(tmp_path)
    store = build_session_store()
    assert isinstance(store, SafeJSONSession)


def test_factory_selects_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_SESSION_STORE", "sqlite")
    monkeypatch.setenv(
        "QWENPAW_SESSION_DB",
        str(tmp_path / "custom.db"),
    )
    store = build_session_store()
    assert isinstance(store, SqliteSession)

    agent = _Module()
    agent.data = {"x": 1}
    asyncio.run(store.save_session_state("s", agent=agent))
    assert (tmp_path / "custom.db").exists()
