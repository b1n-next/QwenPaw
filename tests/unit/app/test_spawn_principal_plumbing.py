# -*- coding: utf-8 -*-
"""EP-2-14: spawn plumbing carries the sub-principal end to end."""

from __future__ import annotations

import asyncio

# pylint: disable=protected-access

from qwenpaw.agents.tools import agent_management as am


def test_spawn_context_carries_principal() -> None:
    """_build_subagent_request_context mints the sub-principal.

    ContextVars default empty, and a missing/synthetic agent config is
    tolerated by the builder, so no patching is required.
    """
    rc = asyncio.run(
        am._build_subagent_request_context(
            "coder",
            subagent_session_id="sub-deadbeef",
        ),
    )
    assert rc["_spawn_subagent"] is True
    assert rc["subagent_principal"] == "coder:sub:deadbeef"


def test_spawn_context_without_session_has_no_principal() -> None:
    rc = asyncio.run(am._build_subagent_request_context("coder"))
    assert "subagent_principal" not in rc
