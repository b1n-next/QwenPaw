# -*- coding: utf-8 -*-
"""HumanGate — loop-mode gate that suspends for human approval (EP-2-15).

The 8th built-in gate: at configured loop rounds the loop suspends by
creating a pending approval through the shared ApprovalService — the
same queue, persistence (EP-2-12) and console approval cards as tool
approvals. Approve resumes the loop; deny (or timeout) terminates it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .base import StopAction, StopHandlerResult
from .loop_gate import LoopGate

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 600.0


@dataclass
class _HumanState:
    rounds: set[int]
    timeout_seconds: float
    message: str
    iteration: int = 0
    asked: set[int] = field(default_factory=set)


class HumanGate(LoopGate):
    """Suspend the loop for a human decision at specific rounds."""

    def __init__(
        self,
        *,
        at_rounds: Optional[list[int]] = None,
        every_n_rounds: int = 0,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        message: str = "",
    ) -> None:
        super().__init__()
        rounds = {int(r) for r in (at_rounds or []) if int(r) > 0}
        if every_n_rounds > 0:
            rounds.update(every_n_rounds * k for k in range(1, 1000))
        self._default_rounds = sorted(rounds)
        self._default_timeout = float(timeout_seconds)
        self._default_message = message

    @property
    def name(self) -> str:
        return "human_gate"

    @property
    def priority(self) -> int:
        return 20  # after the hard iteration cap (10)

    def activate(  # pylint: disable=arguments-renamed
        self,
        rounds: Optional[set[int]] = None,
        timeout_seconds: Optional[float] = None,
        message: str = "",
    ) -> None:
        effective = rounds if rounds is not None else set(self._default_rounds)
        super().activate(
            _HumanState(
                rounds=effective,
                timeout_seconds=(timeout_seconds or self._default_timeout),
                message=message or self._default_message,
            ),
        )

    async def check(
        self,
        ctx: Any,  # pylint: disable=unused-argument
    ) -> StopHandlerResult:
        state: Optional[_HumanState] = self._state()
        if state is None:
            return StopHandlerResult(action=StopAction.BYPASS)

        state.iteration += 1
        if state.iteration not in state.rounds or state.iteration in (
            state.asked
        ):
            return StopHandlerResult(action=StopAction.BYPASS)
        state.asked.add(state.iteration)

        decision = await self._request_decision(state)
        if decision == "approved":
            logger.info(
                "HumanGate: round %d approved — loop continues",
                state.iteration,
            )
            return StopHandlerResult(action=StopAction.BYPASS)
        logger.info(
            "HumanGate: round %d %s — loop terminates",
            state.iteration,
            decision,
        )
        self.deactivate()
        return StopHandlerResult(
            action=StopAction.TERMINATE,
            reason=(
                f"Human gate rejected the loop at round {state.iteration} "
                f"({decision})"
            ),
        )

    async def _request_decision(self, state: _HumanState) -> str:
        """Create a pending approval and await the human decision."""
        from ...app.agent_context import (
            get_current_agent_id,
            get_current_channel,
            get_current_root_session_id,
            get_current_session_id,
            get_current_user_id,
        )
        from ...app.approvals.models import ApprovalRequestSummary
        from ...app.approvals.service import get_approval_service

        session_id = get_current_session_id() or "default"
        agent_id = get_current_agent_id() or "default"
        try:
            service = get_approval_service()
            pending = await service.create_pending_summary(
                session_id=session_id,
                root_session_id=(get_current_root_session_id() or session_id),
                owner_agent_id=agent_id,
                user_id=get_current_user_id() or "",
                channel=get_current_channel() or "",
                agent_id=agent_id,
                summary=ApprovalRequestSummary(
                    source_type="loop_human_gate",
                    name="human_gate",
                    severity="medium",
                    findings_count=0,
                    result_summary=state.message
                    or "Loop reached a human gate round",
                    payload={
                        "gate": "human_gate",
                        "round": state.iteration,
                        "message": state.message,
                    },
                ),
                timeout_seconds=state.timeout_seconds,
                extra={"_loop_human_gate": True},
            )
        except Exception:  # pragma: no cover - defensive guard
            logger.exception("HumanGate: failed to create approval")
            return "error"

        decision = await service.wait_for_approval(
            pending.request_id,
            state.timeout_seconds,
        )
        return decision.value  # "approved" | "denied" | "timeout"

    def reset_turn(self) -> None:
        """Fresh counters for a new top-level turn."""
        state = self._state()
        if state is not None:
            state.iteration = 0
            state.asked.clear()


__all__ = ["HumanGate"]
