# -*- coding: utf-8 -*-
"""EP-2-14: sub-agent principal demotion tests.

Spawned subagents keep the parent identity for governance/approval
routing, but token usage and tool audits attribute to a distinct
``<parent>:sub:<suffix>`` principal so the hub can split per-subagent
cost and activity.
"""

from __future__ import annotations

# pylint: disable=protected-access

from pathlib import Path

from qwenpaw.agents.tools.agent_management import (
    _build_spawn_request_context,
)
from qwenpaw.app.agent_context import (
    set_current_agent_id,
    set_subagent_principal,
)
from qwenpaw.governance.audit import AuditLog
from qwenpaw.governance.policy import (
    GovernanceAction,
    GovernanceDecision,
    ToolCallSpec,
)
from qwenpaw.token_usage.manager import _usage_agent_id


def _tc(agent_id: str = "coder") -> ToolCallSpec:
    return ToolCallSpec(
        tool_name="Read",
        target="/tmp/x",
        agent_id=agent_id,
        session_id="s1",
    )


# ------------------------------------------------------------ mint


def test_principal_minted_from_session() -> None:
    rc = _build_spawn_request_context("coder", "sub-abc12345")
    assert rc["subagent_principal"] == "coder:sub:abc12345"


def test_no_principal_without_session() -> None:
    rc = _build_spawn_request_context("coder")
    assert "subagent_principal" not in rc


# ------------------------------------------------------- attribution


def test_usage_prefers_principal() -> None:
    set_current_agent_id("coder")
    set_subagent_principal("coder:sub:1")
    try:
        assert _usage_agent_id() == "coder:sub:1"
        set_subagent_principal("")
        assert _usage_agent_id() == "coder"
    finally:
        set_subagent_principal("")
        set_current_agent_id("default")


def test_audit_attributes_to_principal(tmp_path: Path) -> None:
    existing = AuditLog._instance
    if existing is not None:
        existing.close()
    audit = AuditLog.get_instance(tmp_path)
    decision = GovernanceDecision(
        action=GovernanceAction.ALLOW,
        reason="ok",
        source="user_rules",
    )

    set_subagent_principal("coder:sub:9")
    try:
        audit.record(str(tmp_path), _tc("coder"), decision)
        set_subagent_principal("")
        audit.record(str(tmp_path), _tc("coder"), decision)
    finally:
        set_subagent_principal("")

    events, total = audit.query(limit=10)
    assert total == 2
    agents = {event.agent_id for event in events}
    assert agents == {"coder:sub:9", "coder"}
    principal_rows, principal_total = audit.query(
        agent_id="coder:sub:9",
        limit=10,
    )
    assert principal_total == 1  # principal filter isolates the subagent
    assert principal_rows[0].agent_id == "coder:sub:9"
    audit.close()
