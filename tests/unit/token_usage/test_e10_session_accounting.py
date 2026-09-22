# -*- coding: utf-8 -*-
"""E10 C1-C3: conversation-level token accounting side-car."""

from __future__ import annotations

from qwenpaw.token_usage.buffer import _UsageEvent, _apply_event
from qwenpaw.token_usage.manager import TokenUsageManager


def _event(session: str = "", agent: str = "a1", **over) -> _UsageEvent:
    base = {
        "provider_id": "prov",
        "model_name": "m1",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "date_str": "2026-09-22",
        "now_iso": "2026-09-22T00:00:00+00:00",
        "agent_id": agent,
        "session_id": session,
    }
    base.update(over)
    return _UsageEvent(**base)


def test_side_car_accumulates_per_session() -> None:
    cache: dict = {}
    _apply_event(cache, _event(session="s1"))
    _apply_event(cache, _event(session="s1", prompt_tokens=7))
    _apply_event(cache, _event(session="s2"))
    _apply_event(cache, _event())  # no session: side-car untouched

    sessions = cache["sessions"]
    s1 = sessions["s1"]["2026-09-22"]["a1\x1fprov\x1fm1"]
    assert s1["prompt_tokens"] == 17
    assert s1["completion_tokens"] == 10
    assert s1["call_count"] == 2
    assert "s2" in sessions
    assert len(sessions) == 2  # anonymous event not tracked

    # daily aggregate unchanged shape
    day = cache["2026-09-22"]["a1\x1fprov\x1fm1"]
    assert day["prompt_tokens"] == 37  # 10+17 session s1 + 10 anon
    assert day["call_count"] == 4


def test_side_car_separates_agents_and_days() -> None:
    cache: dict = {}
    _apply_event(cache, _event(session="s1", agent="a1"))
    _apply_event(cache, _event(session="s1", agent="a2"))
    _apply_event(
        cache,
        _event(session="s1", agent="a1", date_str="2026-09-21"),
    )
    day = cache["sessions"]["s1"]["2026-09-22"]
    assert len(day) == 2  # a1 + a2 rows
    assert "2026-09-21" in cache["sessions"]["s1"]


def test_usage_for_session_drilldown(tmp_path) -> None:
    import asyncio

    from qwenpaw.token_usage.buffer import TokenUsageBuffer

    manager = TokenUsageManager()
    manager._buffer = TokenUsageBuffer(  # pylint: disable=protected-access
        tmp_path / "usage.json",
    )

    async def _run() -> None:
        await manager.record("prov", "m1", 100, 50)
        buffer = manager._buffer  # pylint: disable=protected-access
        buffer.enqueue(_event(session="chat-9", prompt_tokens=200))
        flush_once = getattr(buffer, "_flush_once")
        await flush_once(force=True)
        records = await manager.usage_for_session("chat-9")
        assert len(records) == 1
        row = records[0]
        assert row.session_id == "chat-9"
        assert row.prompt_tokens == 200
        assert row.model == "m1"
        assert row.agent_id == "a1"
        empty = await manager.usage_for_session("missing")
        assert empty == []

    asyncio.run(_run())
