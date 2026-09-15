# -*- coding: utf-8 -*-
"""MCP tool definitions over QwenPaw agents and graphs (EP-2-20).

Agent turns ride the existing inter-agent chat task API (submit +
poll, both sync HTTP calls against the local runtime); graph tools
run the EP-2-17 executor in-process against the shared state store.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Dict

from ..agents.tools.agent_management import (
    DEFAULT_AGENT_API_TIMEOUT,
    get_agent_chat_task_status,
    submit_agent_chat_task,
)
from ..constant import WORKING_DIR
from ..graph.executor import GraphExecutor
from ..graph.schema import GraphSchema
from ..graph.state_store import GraphStateStore
from .protocol import MCPServer

_TEMPLATES_DIR = Path(WORKING_DIR) / "graph_templates"

_store_singleton: GraphStateStore | None = None


def _state_store() -> GraphStateStore:
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = GraphStateStore(
            Path(WORKING_DIR) / "graph_runs.db",
        )
    return _store_singleton


# ------------------------------------------------------------ handlers


def _tool_call_text(text: str, is_error: bool = False) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def _agent_chat_submit(arguments: Dict[str, Any]) -> Dict[str, Any]:
    agent_id = str(arguments.get("agent_id") or "default").strip()
    message = str(arguments.get("message") or "").strip()
    if not message:
        return _tool_call_text("message is required", is_error=True)
    payload = {
        "session_id": f"mcp-{uuid.uuid4().hex[:10]}",
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": message}],
            },
        ],
    }
    try:
        result = submit_agent_chat_task(
            None,  # default base-url resolution (localhost runtime)
            payload,
            agent_id,
            int(DEFAULT_AGENT_API_TIMEOUT),
        )
    except Exception as exc:  # noqa: BLE001 - tool-level error, not protocol
        return _tool_call_text(
            f"agent runtime unreachable: {exc}",
            is_error=True,
        )
    if isinstance(result, dict) and result.get("error"):
        return _tool_call_text(
            str(result["error"]),
            is_error=True,
        )
    task_id = (
        result.get("task_id") if isinstance(result, dict) else None
    ) or ""
    return _tool_call_text(
        f"submitted task {task_id} to agent '{agent_id}' — poll with "
        "agent_task_status",
    )


def _agent_task_status(arguments: Dict[str, Any]) -> Dict[str, Any]:
    agent_id = str(arguments.get("agent_id") or "default").strip()
    task_id = str(arguments.get("task_id") or "").strip()
    if not task_id:
        return _tool_call_text("task_id is required", is_error=True)
    status = get_agent_chat_task_status(
        None,
        task_id,
        to_agent=agent_id or None,
        timeout=int(DEFAULT_AGENT_API_TIMEOUT),
    )
    if isinstance(status, dict) and status.get("error"):
        return _tool_call_text(str(status["error"]), is_error=True)
    return _tool_call_text(repr(status))


def _load_template(template_id: str) -> GraphSchema:
    path = _TEMPLATES_DIR / f"{template_id}.json"
    if not path.exists():
        raise ValueError(f"template '{template_id}' not found")
    return GraphSchema.model_validate_json(path.read_text(encoding="utf-8"))


def _graph_handlers() -> Dict[str, Any]:
    async def _agent(node: Any, _state: Dict[str, Any]):
        from ..graph.executor import NodeResult

        return NodeResult(
            outputs={
                "kind": "agent",
                "prompt": str(node.params.get("prompt") or ""),
            },
        )

    async def _gate(_node: Any, _state: Dict[str, Any]):
        from ..graph.executor import Suspension

        raise Suspension("human gate")

    return {"agent": _agent, "gate": _gate}


def _run_payload(result: Any) -> Dict[str, Any]:
    return _tool_call_text(
        repr(
            {
                "run_id": result.run_id,
                "status": result.status,
                "suspended_at": result.suspended_at,
                "error": result.error,
            },
        ),
    )


def _graph_run(arguments: Dict[str, Any]) -> Dict[str, Any]:
    template_id = str(arguments.get("template_id") or "").strip()
    if not template_id:
        return _tool_call_text("template_id is required", is_error=True)
    try:
        schema = _load_template(template_id)
    except (OSError, ValueError) as exc:
        return _tool_call_text(str(exc), is_error=True)
    executor = GraphExecutor(
        schema,
        _state_store(),
        _graph_handlers(),
    )
    run_id = f"mcp-{uuid.uuid4().hex[:12]}"
    result = asyncio.run(
        executor.run(
            run_id=run_id,
            initial_state={
                "inputs": dict(arguments.get("inputs") or {}),
                "template": template_id,
            },
        ),
    )
    return _run_payload(result)


def _graph_resume(arguments: Dict[str, Any]) -> Dict[str, Any]:
    run_id = str(arguments.get("run_id") or "").strip()
    route = str(arguments.get("route") or "").strip()
    if not run_id or not route:
        return _tool_call_text(
            "run_id and route are required",
            is_error=True,
        )
    run = _state_store().get_run(run_id)
    if run is None:
        return _tool_call_text(f"run '{run_id}' not found", is_error=True)
    try:
        schema = _load_template(str(run.get("state", {}).get("template", "")))
    except (OSError, ValueError) as exc:
        return _tool_call_text(str(exc), is_error=True)
    executor = GraphExecutor(
        schema,
        _state_store(),
        _graph_handlers(),
    )
    result = asyncio.run(executor.resume(run_id, route))
    return _run_payload(result)


# -------------------------------------------------------------- build


TOOL_DESCRIPTIONS = [
    {
        "name": "agent_chat_submit",
        "description": (
            "Submit one message to a QwenPaw agent as a background "
            "chat task; returns the task id for polling."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Target agent id (default: 'default')",
                },
                "message": {"type": "string"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "agent_task_status",
        "description": "Poll the status and latest output of a chat task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "graph_run",
        "description": (
            "Start a graph-agent run from a published template; a human "
            "gate suspends the run — resolve it with graph_resume."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "template_id": {"type": "string"},
                "inputs": {"type": "object"},
            },
            "required": ["template_id"],
        },
    },
    {
        "name": "graph_resume",
        "description": (
            "Resolve a suspended graph run (approve/deny a human gate)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "route": {"type": "string"},
            },
            "required": ["run_id", "route"],
        },
    },
]


def build_server() -> MCPServer:
    """Assemble the MCP server with all tools registered."""
    server = MCPServer(
        {
            "agent_chat_submit": _agent_chat_submit,
            "agent_task_status": _agent_task_status,
            "graph_run": _graph_run,
            "graph_resume": _graph_resume,
        },
    )
    server.describe_tools(TOOL_DESCRIPTIONS)
    return server


__all__ = ["TOOL_DESCRIPTIONS", "build_server"]
