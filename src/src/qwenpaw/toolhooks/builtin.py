# -*- coding: utf-8 -*-
"""Built-in tool hooks demonstrating the three mount points (EP-2-23)."""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import ToolCallContext, ToolHook, ToolHookDecision

logger = logging.getLogger(__name__)

_SECRET_KEYS = frozenset(
    {"password", "token", "api_key", "secret", "authorization"},
)
_REDACTED = "***"


class DenyListHook(ToolHook):
    """pre_call interception: block deny-listed tools."""

    name = "deny_list"

    def __init__(self, denied: Dict[str, str] | None = None) -> None:
        self._denied = dict(denied or {})

    def deny(self, tool_name: str, reason: str) -> None:
        """Add or replace one deny entry."""
        self._denied[tool_name] = reason

    def pre_call(self, context: ToolCallContext) -> ToolHookDecision:
        reason = self._denied.get(context.tool_name)
        if reason:
            return ToolHookDecision.block(
                f"tool '{context.tool_name}' denied: {reason}",
            )
        return ToolHookDecision.allow()


class RedactSecretsHook(ToolHook):
    """pre_call mutation: redact secret-looking argument values."""

    name = "redact_secrets"

    def pre_call(self, context: ToolCallContext) -> ToolHookDecision:
        redacted = self._redact(context.arguments)
        if redacted != context.arguments:
            return ToolHookDecision.mutate(redacted)
        return ToolHookDecision.allow()

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    _REDACTED
                    if str(key).lower() in _SECRET_KEYS and str(item) != ""
                    else cls._redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        return value


class AuditLogHook(ToolHook):
    """post_call + on_failure: structured usage audit line."""

    name = "audit_log"

    def __init__(self, sink: Any = None) -> None:
        self._sink = sink  # callable(line: str) for tests

    def _emit(self, line: str) -> None:
        if self._sink is not None:
            self._sink(line)
        else:
            logger.info(line)

    def post_call(
        self,
        context: ToolCallContext,
        result: Any,
        duration_ms: float,
    ) -> None:
        self._emit(
            f"tool_ok tool={context.tool_name} agent={context.agent_id} "
            f"trace={context.trace_id} duration_ms={duration_ms:.1f}",
        )

    def on_failure(
        self,
        context: ToolCallContext,
        error: BaseException,
    ) -> None:
        self._emit(
            f"tool_fail tool={context.tool_name} agent={context.agent_id} "
            f"trace={context.trace_id} error={type(error).__name__}",
        )


__all__ = ["AuditLogHook", "DenyListHook", "RedactSecretsHook"]
