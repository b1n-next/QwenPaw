# -*- coding: utf-8 -*-
"""Adapters between heterogeneous tool-call shapes and hooks .

Providers differ (OpenAI function calls, agentscope internal calls),
so extraction and write-back are kept tolerant and centralised here.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Tuple


def tool_call_parts(tool_call: Any) -> Tuple[str, Any]:
    """Extract (name, raw-arguments) from any tool-call shape."""
    function = getattr(tool_call, "function", None)
    if function is not None:
        name = getattr(function, "name", "") or ""
        raw = getattr(function, "arguments", None)
        return str(name), _parse(raw)
    name = getattr(tool_call, "name", None)
    if isinstance(name, str) and name:
        return name, _parse(getattr(tool_call, "input", None))
    if isinstance(tool_call, dict):
        inner = tool_call.get("function") or {}
        if isinstance(inner, dict) and inner.get("name"):
            return str(inner["name"]), _parse(inner.get("arguments"))
        if tool_call.get("name"):
            return str(tool_call["name"]), _parse(tool_call.get("input"))
    return "", {}


def write_tool_call_arguments(
    tool_call: Any,
    arguments: Dict[str, Any],
) -> None:
    """Write mutated arguments back onto the tool call (best effort)."""
    encoded = json.dumps(arguments, ensure_ascii=False)
    function = getattr(tool_call, "function", None)
    if function is not None and hasattr(function, "arguments"):
        function.arguments = encoded
        return
    if hasattr(tool_call, "input"):
        tool_call.input = arguments
        return
    if isinstance(tool_call, dict):
        inner = tool_call.get("function")
        if isinstance(inner, dict):
            inner["arguments"] = encoded
            return
        tool_call["input"] = arguments


def _parse(raw: Any) -> Any:
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {"input": raw}
    return {"input": raw}


def arguments_as_dict(raw: Any) -> Dict[str, Any]:
    """Coerce parsed arguments to the dict hooks work with."""
    if isinstance(raw, dict):
        return raw
    return {"input": raw}


__all__ = [
    "arguments_as_dict",
    "tool_call_parts",
    "write_tool_call_arguments",
]
