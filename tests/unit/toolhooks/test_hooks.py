# -*- coding: utf-8 -*-
"""EP-2-23: tool-level hooks — registry semantics and the funnel."""

from __future__ import annotations

# pylint: disable=protected-access

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

import pytest

from qwenpaw.toolhooks import (
    AuditLogHook,
    DenyListHook,
    RedactSecretsHook,
    ToolCallContext,
    ToolHook,
    ToolHookDecision,
    get_registry,
)
from qwenpaw.toolhooks.adapt import (
    arguments_as_dict,
    tool_call_parts,
    write_tool_call_arguments,
)


@pytest.fixture(name="registry")
def _registry():
    registry = get_registry()
    registry.clear()
    yield registry
    registry.clear()


# ---------------------------------------------------------- base types


def test_context_build_coerces_and_carries_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "qwenpaw.toolhooks.base.resolve_trace_id",
        lambda: "trace-77",
    )
    context = ToolCallContext.build("web_search", '{"q": "hi"}')
    assert context.tool_name == "web_search"
    assert context.arguments == {"input": '{"q": "hi"}'}
    assert context.trace_id == "trace-77"


def test_decision_constructors() -> None:
    assert ToolHookDecision.allow().blocked is False
    blocked = ToolHookDecision.block("nope")
    assert blocked.blocked and blocked.reason == "nope"
    mutated = ToolHookDecision.mutate({"a": 1})
    assert mutated.arguments == {"a": 1}


# ----------------------------------------------------------- registry


def test_deny_list_blocks(registry) -> None:
    registry.register(
        DenyListHook({"execute_shell_command": "policy"}),
        pattern="execute_shell_command",
    )
    decision = registry.dispatch_pre(
        ToolCallContext.build("execute_shell_command", {"command": "ls"}),
    )
    assert decision.blocked
    assert "policy" in decision.reason


def test_redaction_mutates_arguments(registry) -> None:
    registry.register(RedactSecretsHook())
    decision = registry.dispatch_pre(
        ToolCallContext.build(
            "web_search",
            {"search_term": "x", "api_key": "sk-1", "nested": {"token": "t"}},
        ),
    )
    assert decision.arguments is not None
    assert decision.arguments["api_key"] == "***"
    assert decision.arguments["nested"]["token"] == "***"
    assert decision.arguments["search_term"] == "x"


def test_pattern_scoping_and_priority(registry) -> None:
    @dataclass
    class OrderHook(ToolHook):
        name: str = "order"
        seen: List[str] = None  # type: ignore[assignment]

        def pre_call(self, context: ToolCallContext) -> ToolHookDecision:
            self.seen.append(context.tool_name)
            return ToolHookDecision.allow()

    high = OrderHook(seen=[])
    low = OrderHook(seen=[])
    registry.register(low, pattern="web_.*", priority=0)
    registry.register(high, pattern="web_search", priority=10)

    registry.dispatch_pre(ToolCallContext.build("web_search", {}))
    assert high.seen == ["web_search"]
    assert low.seen == ["web_search"]

    registry.dispatch_pre(ToolCallContext.build("web_fetch", {}))
    assert high.seen == ["web_search"]
    assert low.seen == ["web_search", "web_fetch"]


def test_broken_hook_is_isolated(registry, caplog) -> None:
    class ExplodingHook(ToolHook):
        name = "exploding"

        def pre_call(self, context: ToolCallContext) -> ToolHookDecision:
            raise RuntimeError("boom")

    registry.register(ExplodingHook())
    with caplog.at_level(logging.WARNING):
        decision = registry.dispatch_pre(ToolCallContext.build("any", {}))
    assert decision.blocked is False  # tool still runs


def test_audit_post_and_failure(registry) -> None:
    lines: List[str] = []
    registry.register(AuditLogHook(sink=lines.append))

    context = ToolCallContext.build("web_search", {}, agent_id="a1")
    registry.timed_post(context, None, time.monotonic() - 0.01)
    registry.dispatch_failure(context, ValueError("kaput"))

    assert len(lines) == 2
    assert lines[0].startswith("tool_ok tool=web_search agent=a1")
    assert "duration_ms=" in lines[0]
    assert lines[1].startswith("tool_fail tool=web_search")
    assert "ValueError" in lines[1]


# -------------------------------------------------------------- adapt


class _Function:
    def __init__(self) -> None:
        self.name = "web_search"
        self.arguments = json.dumps({"q": "hi"})


class _OpenAIStyleToolCall:
    def __init__(self) -> None:
        self.function = _Function()


def test_adapt_openai_shape() -> None:
    call = _OpenAIStyleToolCall()
    name, raw = tool_call_parts(call)
    assert name == "web_search"
    assert raw == {"q": "hi"}

    write_tool_call_arguments(call, {"q": "redacted"})
    assert json.loads(call.function.arguments) == {"q": "redacted"}


def test_adapt_agentscope_shape() -> None:
    @dataclass
    class _Call:
        name: str = "shell"
        input: Any = None

    call = _Call(input={"command": "ls"})
    name, raw = tool_call_parts(call)
    assert (name, raw) == ("shell", {"command": "ls"})

    write_tool_call_arguments(call, {"command": "echo"})
    assert call.input == {"command": "echo"}


def test_adapt_unknown_shape() -> None:
    assert tool_call_parts(object()) == ("", {})
    assert tool_call_parts({"name": "x", "input": "raw"}) == (
        "x",
        {"input": "raw"},
    )


# ------------------------------------------------------------- funnel


class _RecordingAgent:
    """Minimal stand-in exposing the wired funnel logic."""

    name = "test-agent"

    def __init__(self) -> None:
        self.executed: List[str] = []
        self.arguments: List[Any] = []

    async def run_funnel(self, tool_call):
        # mirrors the funnel body injected into
        # ReactAgent._execute_tool_call (kept in sync there)
        await _funnel_for_test(self, tool_call, None)


async def _funnel_for_test(agent, tool_call, kept_rules):
    del kept_rules
    registry = get_registry()
    hook_name, hook_raw = tool_call_parts(tool_call)
    hook_context = ToolCallContext.build(
        hook_name,
        arguments_as_dict(hook_raw),
        agent_id=agent.name,
    )
    decision = registry.dispatch_pre(hook_context)
    if decision.blocked:
        return
    if decision.arguments is not None:
        write_tool_call_arguments(tool_call, decision.arguments)
    started_at = time.monotonic()
    failure = None
    try:
        agent.executed.append(tool_call.function.name)
        agent.arguments.append(json.loads(tool_call.function.arguments))
    except BaseException as exc:  # noqa: BLE001
        failure = exc
        raise
    finally:
        if failure is not None:
            registry.dispatch_failure(hook_context, failure)
        else:
            registry.timed_post(hook_context, None, started_at)


def test_funnel_block_skips_execution(registry) -> None:
    registry.register(
        DenyListHook({"web_search": "denied for test"}),
        pattern="web_search",
    )
    lines: List[str] = []
    registry.register(AuditLogHook(sink=lines.append))

    agent = _RecordingAgent()
    asyncio.run(agent.run_funnel(_OpenAIStyleToolCall()))
    assert not agent.executed  # pre-hook intercepted
    assert not lines  # no post/failure for a blocked call


def test_funnel_redacts_then_executes(registry) -> None:
    registry.register(RedactSecretsHook())
    lines: List[str] = []
    registry.register(AuditLogHook(sink=lines.append))

    call = _OpenAIStyleToolCall()
    call.function.arguments = json.dumps(
        {"q": "hi", "api_key": "sk-live"},
    )
    agent = _RecordingAgent()
    asyncio.run(agent.run_funnel(call))

    assert agent.executed == ["web_search"]
    assert agent.arguments[0]["api_key"] == "***"
    assert agent.arguments[0]["q"] == "hi"
    assert lines and lines[0].startswith("tool_ok")


def test_funnel_failure_tier(registry) -> None:
    lines: List[str] = []
    registry.register(AuditLogHook(sink=lines.append))

    call = _OpenAIStyleToolCall()
    call.function.arguments = "{not-json"  # execution blows up
    agent = _RecordingAgent()
    with pytest.raises(ValueError):
        asyncio.run(agent.run_funnel(call))
    assert any(line.startswith("tool_fail") for line in lines)


# ----------------------------------------------------------- example


def test_example_demo_runs() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "tool_hooks_demo",
        Path(__file__).parents[3] / "examples" / "tool_hooks" / "demo.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main() == 0


def test_empty_registry_is_transparent(registry) -> None:
    decision = registry.dispatch_pre(ToolCallContext.build("any", {}))
    assert decision.blocked is False
    assert decision.arguments is None
    registry.timed_post(ToolCallContext.build("any", {}), None, 0.0)
    registry.dispatch_failure(
        ToolCallContext.build("any", {}),
        RuntimeError("x"),
    )


def test_react_agent_funnel_is_wired() -> None:
    """Source contract: the real funnel dispatches all three tiers."""
    import inspect

    from qwenpaw.agents import react_agent as ra

    source = inspect.getsource(
        ra.QwenPawAgent._execute_tool_call,
    )
    assert "dispatch_pre" in source
    assert "dispatch_failure" in source
    assert "timed_post" in source
    assert "write_tool_call_arguments" in source
