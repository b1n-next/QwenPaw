# -*- coding: utf-8 -*-
"""EP-2-15: human_gate loop-mode gate tests.

The 8th built-in gate suspends the loop through the shared
ApprovalService — approve resumes, deny/timeout terminates — and
compiles into custom loop modes like any catalog gate.
"""

from __future__ import annotations

import asyncio

# pylint: disable=protected-access

import pytest

from qwenpaw.app.approvals import service as approval_service_module
from qwenpaw.security.tool_guard.approval import ApprovalDecision
from qwenpaw.app.approvals.service import ApprovalService
from qwenpaw.config.config import CustomLoopModeConfig, GateInstanceConfig
from qwenpaw.loop.catalog import get_gate_catalog
from qwenpaw.loop.compiler import compile_loop_mode
from qwenpaw.loop.gates.base import StopAction
from qwenpaw.loop.gates.human import HumanGate


class _Ctx:
    """Minimal stop-check context placeholder."""


@pytest.fixture(name="service")
def _service(monkeypatch: pytest.MonkeyPatch) -> ApprovalService:
    svc = ApprovalService()
    monkeypatch.setattr(
        approval_service_module,
        "get_approval_service",
        lambda: svc,
    )
    return svc


async def _resolve_when_pending(
    service: ApprovalService,
    decision: ApprovalDecision,
) -> None:
    """Wait for the gate's pending approval, then resolve it."""
    for _ in range(200):  # up to 10s
        async with service._lock:
            pending_ids = [
                rid
                for rid, p in service._pending.items()
                if p.status == "pending"
            ]
        if pending_ids:
            await service.resolve_request(pending_ids[0], decision)
            return
        await asyncio.sleep(0.05)
    raise AssertionError("no pending approval appeared")


async def _check_with_resolution(
    gate: HumanGate,
    service: ApprovalService,
    decision: ApprovalDecision,
):
    return await asyncio.gather(
        gate.check(_Ctx()),
        _resolve_when_pending(service, decision),
    )


# ------------------------------------------------------ decision paths


async def test_approved_round_resumes(service: ApprovalService) -> None:
    gate = HumanGate(at_rounds=[2], message="continue?")
    gate.activate()

    first = await gate.check(_Ctx())
    assert first.action == StopAction.BYPASS  # round 1 not gated

    (result, _) = await _check_with_resolution(
        gate,
        service,
        ApprovalDecision.APPROVED,
    )
    assert result.action == StopAction.BYPASS  # approved → continue

    third = await gate.check(_Ctx())
    assert third.action == StopAction.BYPASS  # round 3 not gated


async def test_denied_round_terminates(service: ApprovalService) -> None:
    gate = HumanGate(at_rounds=[1])
    gate.activate()

    (result, _) = await _check_with_resolution(
        gate,
        service,
        ApprovalDecision.DENIED,
    )
    assert result.action == StopAction.TERMINATE
    assert "round 1" in result.reason
    assert "denied" in result.reason


async def test_timeout_terminates(service: ApprovalService) -> None:
    gate = HumanGate(at_rounds=[1])
    gate.activate()

    (result, _) = await _check_with_resolution(
        gate,
        service,
        ApprovalDecision.TIMEOUT,
    )
    assert result.action == StopAction.TERMINATE
    assert "timeout" in result.reason


# ----------------------------------------------------------- cadence


async def test_every_n_rounds_repeats(service: ApprovalService) -> None:
    gate = HumanGate(every_n_rounds=2)
    gate.activate()

    assert (await gate.check(_Ctx())).action == StopAction.BYPASS  # 1

    (result, _) = await _check_with_resolution(
        gate,
        service,
        ApprovalDecision.APPROVED,
    )
    assert result.action == StopAction.BYPASS  # 2 approved

    assert (await gate.check(_Ctx())).action == StopAction.BYPASS  # 3

    (result, _) = await _check_with_resolution(
        gate,
        service,
        ApprovalDecision.DENIED,
    )
    assert result.action == StopAction.TERMINATE  # 4 denied


async def test_reset_turn_restarts_counters() -> None:
    gate = HumanGate(at_rounds=[1, 2])
    gate.activate()
    state = gate._state()
    state.iteration = 2
    state.asked.add(1)
    state.asked.add(2)
    gate.reset_turn()
    assert state.iteration == 0
    assert state.asked == set()


# ------------------------------------------------- catalog + compile


def test_catalog_registers_eighth_gate() -> None:
    types = [e["type"] for e in get_gate_catalog().describe()]
    assert "human_gate" in types
    entry = next(
        e for e in get_gate_catalog().describe() if e["type"] == "human_gate"
    )
    assert entry["category"] == "approval"
    assert "at_rounds" in entry["schema"]["properties"]


def test_compiles_into_custom_loop_mode() -> None:
    config = CustomLoopModeConfig(
        id="supervised-run",
        name="Supervised run",
        slash_command="supervised",
        enabled=True,
        gates=[
            GateInstanceConfig(
                id="iteration-cap",
                type="iteration",
                enabled=True,
                params={"max_iterations": 10},
            ),
            GateInstanceConfig(
                id="checkpoint",
                type="human_gate",
                enabled=True,
                params={"at_rounds": [5], "message": "Keep going?"},
            ),
        ],
    )
    handler = compile_loop_mode(config)
    gate_names = [g.name for g in handler._gates]
    assert "checkpoint" in gate_names
    assert "iteration-cap" in gate_names


def test_catalog_rejects_bad_params() -> None:
    with pytest.raises(Exception):
        get_gate_catalog().validate_params(
            "human_gate",
            {"timeout_seconds": 0},
        )
