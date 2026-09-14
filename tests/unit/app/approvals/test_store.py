# -*- coding: utf-8 -*-
"""EP-2-12: durable approval persistence tests.

Covers the ApprovalStore shadow, the service lifecycle mirroring, and
the crash-recovery scan (kill -9 semantics: a fresh service instance
attached to the same database must list previously pending approvals).
"""

from __future__ import annotations

# pylint: disable=protected-access

import time

import pytest

from qwenpaw.app.approvals.models import ApprovalRequestSummary
from qwenpaw.app.approvals.service import (
    ApprovalIdentityPolicy,
    ApprovalService,
    PendingApproval,
)
from qwenpaw.app.approvals.store import ApprovalStore, _safe_extra
from qwenpaw.security.tool_guard.approval import ApprovalDecision


# ------------------------------------------------------------- helpers


def _make_pending(
    request_id: str = "req-1",
    *,
    created_at: float | None = None,
    extra: dict | None = None,
) -> PendingApproval:
    return PendingApproval(
        request_id=request_id,
        session_id="s1",
        root_session_id="s1",
        owner_agent_id="agent-A",
        user_id="u1",
        channel="console",
        agent_id="agent-A",
        tool_name="Bash",
        created_at=created_at if created_at is not None else time.time(),
        future=None,  # type: ignore[arg-type]
        timeout_seconds=300.0,
        result_summary="2 findings",
        findings_count=2,
        severity="high",
        extra=extra or {},
    )


# ---------------------------------------------------------------- store


def test_store_roundtrip(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "approvals.db")
    pending = _make_pending("req-a")
    store.save_pending(pending)

    rows = store.load_unresolved()
    assert len(rows) == 1
    assert rows[0]["request_id"] == "req-a"
    assert rows[0]["status"] == "pending"
    assert rows[0]["tool_name"] == "Bash"

    store.mark_resolved(
        "req-a",
        status="approved",
        resolved_at=time.time(),
        scope="similar",
    )
    assert store.load_unresolved() == []

    store.close()


def test_store_extra_sanitizes_live_objects(tmp_path) -> None:
    class _LiveChannel:  # pylint: disable=too-few-public-methods
        pass

    store = ApprovalStore(tmp_path / "approvals.db")
    store.save_pending(
        _make_pending(
            "req-b",
            extra={
                "_channel_instance": _LiveChannel(),
                "tool_call": {"id": "tc-1", "name": "Bash"},
            },
        ),
    )
    rows = store.load_unresolved()
    import json

    extra = json.loads(rows[0]["extra_json"])
    assert extra["tool_call"] == {"id": "tc-1", "name": "Bash"}
    assert extra["_channel_instance"] == "<_LiveChannel>"

    store.close()


def test_safe_extra_plain_types_survive() -> None:
    import json

    parsed = json.loads(_safe_extra({"a": 1, "b": "x", "c": None}))
    assert parsed == {"a": 1, "b": "x", "c": None}


def test_store_purge_removes_only_decided(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "approvals.db")
    store.save_pending(_make_pending("keep"))
    store.save_pending(
        _make_pending("gone", created_at=time.time() - 60 * 86400),
    )
    store.mark_resolved(
        "gone",
        status="approved",
        resolved_at=time.time() - 60 * 86400,
    )

    store.purge_expired()

    rows = store.load_unresolved()
    assert [r["request_id"] for r in rows] == ["keep"]
    store.close()


# -------------------------------------------------------------- service


@pytest.mark.asyncio
async def test_service_lifecycle_mirrored_to_store(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "approvals.db")
    service = ApprovalService()
    service.attach_store(store)

    pending = await service.create_pending_summary(
        session_id="s1",
        root_session_id="s1",
        owner_agent_id="agent-A",
        user_id="u1",
        channel="console",
        agent_id="agent-A",
        summary=ApprovalRequestSummary(
            name="Shell",
            source_type="tool_guard",
            result_summary="dangerous command",
            findings_count=1,
            severity="high",
        ),
    )
    rows = store.load_unresolved()
    assert [r["request_id"] for r in rows] == [pending.request_id]

    resolved = await service.resolve_request(
        pending.request_id,
        ApprovalDecision.APPROVED,
    )
    assert resolved is not None
    assert store.load_unresolved() == []

    store.close()


@pytest.mark.asyncio
async def test_restore_recovers_pending_after_crash(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "approvals.db")
    first = ApprovalService()
    first.attach_store(store)
    pending = await first.create_pending_summary(
        session_id="s1",
        root_session_id="s1",
        owner_agent_id="agent-A",
        user_id="u1",
        channel="console",
        agent_id="agent-A",
        summary=ApprovalRequestSummary(
            name="Shell",
            source_type="tool_guard",
            result_summary="x",
            findings_count=0,
            severity="medium",
        ),
    )
    # simulate kill -9: pending still durable, service gone
    assert store.load_unresolved()

    second = ApprovalService()
    second.attach_store(store)
    restored = await second.restore_from_store()
    assert restored == 1

    recovered = await second.get_request(pending.request_id)
    assert recovered is not None
    assert recovered.tool_name == "Shell"
    assert recovered.session_id == "s1"
    assert recovered.identity_policy == ApprovalIdentityPolicy.AGENT

    # the recovered request stays answerable
    resolved = await second.resolve_request(
        pending.request_id,
        ApprovalDecision.DENIED,
    )
    assert resolved is not None
    assert store.load_unresolved() == []

    store.close()


@pytest.mark.asyncio
async def test_restore_times_out_expired_rows(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "approvals.db")
    stale = _make_pending(
        "req-stale",
        created_at=time.time() - 3600,
    )
    stale.timeout_seconds = 60.0
    store.save_pending(stale)

    service = ApprovalService()
    service.attach_store(store)
    restored = await service.restore_from_store()
    assert restored == 0  # expired, not re-hydrated

    assert await service.get_request("req-stale") is None
    assert store.load_unresolved() == []  # marked timeout in the store
    store.close()


@pytest.mark.asyncio
async def test_memory_only_service_without_store() -> None:
    service = ApprovalService()
    assert await service.restore_from_store() == 0
