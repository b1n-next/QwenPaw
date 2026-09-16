# -*- coding: utf-8 -*-
"""Tool hook base types ."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


def resolve_trace_id() -> Optional[str]:
    """Best-effort trace-id from the app request context.

    Import stays lazy so agents-side code never hard-depends on the
    app package (and vice versa); outside a request scope the
    answer is simply None.
    """
    try:
        # The trace module is optional at this layer: outside the
        # request-capable app it may be absent entirely.
        from ..app.trace_context import (  # pylint: disable=no-name-in-module
            current_trace_id,
        )

        return current_trace_id()
    except Exception:  # noqa: BLE001 - absent module or no request scope
        return None


@dataclass(frozen=True)
class ToolCallContext:
    """Everything a hook needs about one tool invocation."""

    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    agent_id: str = ""
    trace_id: Optional[str] = None

    @classmethod
    def build(
        cls,
        tool_name: str,
        arguments: Any,
        *,
        agent_id: str = "",
    ) -> "ToolCallContext":
        """Coerce heterogeneous argument payloads into a dict."""
        if isinstance(arguments, dict):
            normalized = dict(arguments)
        else:
            normalized = {"input": arguments}
        return cls(
            tool_name=tool_name,
            arguments=normalized,
            agent_id=agent_id,
            trace_id=resolve_trace_id(),
        )


@dataclass(frozen=True)
class ToolHookDecision:
    """Outcome of the pre_call phase.

    - blocked=True aborts the invocation (reason reaches the agent
      log; the tool never runs)
    - arguments, when not None, replaces the invocation arguments
      (mutation use case: secret redaction)
    """

    blocked: bool = False
    reason: str = ""
    arguments: Optional[Dict[str, Any]] = None

    @classmethod
    def allow(cls) -> "ToolHookDecision":
        return cls()

    @classmethod
    def block(cls, reason: str) -> "ToolHookDecision":
        return cls(blocked=True, reason=reason)

    @classmethod
    def mutate(
        cls,
        arguments: Dict[str, Any],
    ) -> "ToolHookDecision":
        return cls(arguments=dict(arguments))


class ToolHook:
    """Base class: override what you need, no-op by default."""

    name = "tool_hook"

    def pre_call(self, context: ToolCallContext) -> ToolHookDecision:
        """Run before the tool executes; may block or mutate."""
        del context  # default: no opinion
        return ToolHookDecision.allow()

    def post_call(
        self,
        context: ToolCallContext,
        result: Any,
        duration_ms: float,
    ) -> None:
        """Run after a successful execution (observation only)."""
        del context, result, duration_ms  # default: nothing

    def on_failure(
        self,
        context: ToolCallContext,
        error: BaseException,
    ) -> None:
        """Run when the invocation raises (fallback / telemetry)."""
        del context, error  # default: nothing


__all__ = [
    "ToolCallContext",
    "ToolHook",
    "ToolHookDecision",
    "resolve_trace_id",
]
