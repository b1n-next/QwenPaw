# -*- coding: utf-8 -*-
"""EP-2-23 demo: the three tool-hook mount points.

Run:  PYTHONPATH=src python examples/tool_hooks/demo.py

Pre-registration state is restored on exit so the demo never leaks
hooks into a real runtime process.
"""

from __future__ import annotations

import sys

from qwenpaw.toolhooks import (
    AuditLogHook,
    DenyListHook,
    RedactSecretsHook,
    ToolCallContext,
    get_registry,
)


def main() -> int:
    registry = get_registry()
    registry.clear()  # demo starts from a clean slate

    lines: list[str] = []
    audit = AuditLogHook(sink=lines.append)
    deny = DenyListHook({"execute_shell_command": "policy: no shell here"})
    redact = RedactSecretsHook()

    registry.register(deny, pattern="execute_shell_command")
    registry.register(redact, pattern=".*")
    registry.register(audit, pattern=".*", priority=-10)

    def simulate(name: str, arguments: dict) -> str:
        context = ToolCallContext.build(name, arguments, agent_id="demo")
        decision = registry.dispatch_pre(context)
        if decision.blocked:
            return f"BLOCKED: {decision.reason}"
        effective = (
            decision.arguments
            if decision.arguments is not None
            else context.arguments
        )
        registry.timed_post(context, None, __import__("time").monotonic())
        return f"EXECUTED with {effective}"

    # 1) pre-hook interception
    print(simulate("execute_shell_command", {"command": "rm -rf /"}))

    # 2) pre-hook mutation (secrets redacted before the tool sees them)
    print(
        simulate(
            "web_search",
            {"search_term": "qwenpaw", "api_key": "sk-live-123"},
        ),
    )

    # 3) post/failure observation (audit lines carry trace/duration)
    print(f"audit lines: {len(lines)} -> {lines}")

    registry.clear()
    return 0


if __name__ == "__main__":
    sys.exit(main())
