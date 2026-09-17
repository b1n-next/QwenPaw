# -*- coding: utf-8 -*-
"""Tool-level hooks (EP-2-23).

pre / post / failure hook points around every agent tool call,
trace-id aware. The upstream ``runtime/hooks`` phases wrap a whole
Runtime.run(); these wrap the single tool-call funnel in
``ReactAgent._execute_tool_call`` — the one choke point both the
sequential and concurrent execution paths traverse.
"""

from .base import ToolCallContext, ToolHook, ToolHookDecision
from .builtin import (
    AuditLogHook,
    DenyListHook,
    RedactSecretsHook,
)
from .registry import get_registry, register

__all__ = [
    "ToolCallContext",
    "ToolHook",
    "ToolHookDecision",
    "AuditLogHook",
    "DenyListHook",
    "RedactSecretsHook",
    "get_registry",
    "register",
]
