# -*- coding: utf-8 -*-
"""Tool hook registry with pattern matching (EP-2-23)."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .base import ToolCallContext, ToolHook, ToolHookDecision

logger = logging.getLogger(__name__)

_MAX_LOG_ARGUMENTS = 512


class ToolHookRegistry:
    """Priority-ordered hooks, each bound to a tool-name pattern."""

    def __init__(self) -> None:
        self._entries: List[Tuple[int, re.Pattern[str], ToolHook]] = []

    def register(
        self,
        hook: ToolHook,
        *,
        pattern: str = ".*",
        priority: int = 0,
    ) -> None:
        """Register ``hook`` for tool names matching ``pattern``.

        Higher priority runs first in pre_call; post/failure run in
        reverse (undo-style ordering).
        """
        self._entries.append((priority, re.compile(pattern), hook))
        self._entries.sort(key=lambda item: item[0], reverse=True)

    def clear(self) -> None:
        """Drop every registration (test hygiene)."""
        self._entries.clear()

    def _matching(self, tool_name: str) -> List[ToolHook]:
        return [
            hook
            for _priority, compiled, hook in self._entries
            if compiled.match(tool_name)
        ]

    # ------------------------------------------------------- dispatch

    def dispatch_pre(
        self,
        context: ToolCallContext,
    ) -> ToolHookDecision:
        """Run pre_call hooks; first block wins, mutations chain."""
        arguments: Optional[Dict[str, Any]] = None
        for hook in self._matching(context.tool_name):
            try:
                decision = hook.pre_call(context)
            except Exception:  # noqa: BLE001 - hooks must not break tools
                logger.exception(
                    "tool hook %s failed in pre_call; ignored",
                    hook.name,
                )
                continue
            if decision.blocked:
                logger.warning(
                    "tool %s blocked by hook %s: %s (trace=%s)",
                    context.tool_name,
                    hook.name,
                    decision.reason,
                    context.trace_id,
                )
                return decision
            if decision.arguments is not None:
                arguments = decision.arguments
                context = ToolCallContext(
                    tool_name=context.tool_name,
                    arguments=dict(arguments),
                    agent_id=context.agent_id,
                    trace_id=context.trace_id,
                )
        if arguments is None:
            return ToolHookDecision.allow()
        return ToolHookDecision.mutate(arguments)

    def dispatch_post(
        self,
        context: ToolCallContext,
        result: Any,
        duration_ms: float,
    ) -> None:
        """Run post_call hooks for the tool (reverse priority)."""
        for hook in reversed(self._matching(context.tool_name)):
            try:
                hook.post_call(context, result, duration_ms)
            except Exception:  # noqa: BLE001 - hooks must not break tools
                logger.exception(
                    "tool hook %s failed in post_call; ignored",
                    hook.name,
                )

    def dispatch_failure(
        self,
        context: ToolCallContext,
        error: BaseException,
    ) -> None:
        """Run on_failure hooks for the tool (fallback tier)."""
        for hook in reversed(self._matching(context.tool_name)):
            try:
                hook.on_failure(context, error)
            except Exception:  # noqa: BLE001 - hooks must not break tools
                logger.exception(
                    "tool hook %s failed in on_failure; ignored",
                    hook.name,
                )

    # ----------------------------------------------------- convenience

    def timed_post(
        self,
        context: ToolCallContext,
        result: Any,
        started_at: float,
    ) -> None:
        """dispatch_post with wall-clock duration from time.monotonic."""
        self.dispatch_post(
            context,
            result,
            max(0.0, (time.monotonic() - started_at) * 1000.0),
        )

    @staticmethod
    def preview_arguments(context: ToolCallContext) -> str:
        """Small argument preview for logs (no secret dumps)."""
        rendered = repr(context.arguments)
        if len(rendered) > _MAX_LOG_ARGUMENTS:
            rendered = rendered[:_MAX_LOG_ARGUMENTS] + "…"
        return rendered


_registry: Optional[ToolHookRegistry] = None


def get_registry() -> ToolHookRegistry:
    """Process-wide default registry (created on first use)."""
    global _registry
    if _registry is None:
        _registry = ToolHookRegistry()
    return _registry


def register(
    hook: ToolHook,
    *,
    pattern: str = ".*",
    priority: int = 0,
) -> None:
    """Register into the default registry."""
    get_registry().register(hook, pattern=pattern, priority=priority)


__all__ = ["ToolHookRegistry", "get_registry", "register"]
