# -*- coding: utf-8 -*-
"""A2A 1.0 server surface (EP-2-21).

- GET /.well-known/agent-card.json — standard discovery document;
  skills come from published graph templates plus the built-in chat
  skill
- POST /api/a2a — JSON-RPC 2.0 profile: message/send (sync reply or
  working task), message/stream (SSE), tasks/get
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..agents.tools.agent_management import (
    DEFAULT_AGENT_API_TIMEOUT,
    get_agent_chat_task_status,
    submit_agent_chat_task,
)
from ..constant import WORKING_DIR

well_known_router = APIRouter(tags=["a2a"])
api_router = APIRouter(prefix="/a2a", tags=["a2a"])

_TEMPLATES_DIR = Path(WORKING_DIR) / "graph_templates"

_DONE_STATES = frozenset({"completed", "succeeded", "success", "done"})
_FAILED_STATES = frozenset({"failed", "error", "cancelled", "canceled"})


# ------------------------------------------------------------ helpers


class _A2AError(Exception):
    """Protocol-level error carrying a JSON-RPC code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _iter_templates() -> list[dict[str, str]]:
    templates: list[dict[str, str]] = []
    if not _TEMPLATES_DIR.exists():
        return templates
    for path in sorted(_TEMPLATES_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        templates.append(
            {
                "id": str(payload.get("id") or path.stem),
                "name": str(payload.get("name") or path.stem),
                "description": str(payload.get("name") or path.stem),
            },
        )
    return templates


def agent_card(base_url: str = "") -> dict[str, Any]:
    """Assemble the discovery document (skills from graph templates)."""
    skills = [
        {
            "id": "chat",
            "name": "Default chat",
            "description": "Conversation with the default QwenPaw agent",
        },
    ]
    for template in _iter_templates():
        skills.append(
            {
                "id": template["id"],
                "name": template["name"],
                "description": template["description"],
            },
        )
    return {
        "name": "qwenpaw-agent",
        "description": "QwenPaw agents and graph flows over A2A",
        "url": f"{base_url}/api/a2a".rstrip("/"),
        "version": "1.0.0",
        "capabilities": {"streaming": True, "pushNotifications": False},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": skills,
    }


def _message_text(message: Dict[str, Any]) -> str:
    parts = message.get("parts") or []
    chunks = [
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and part.get("kind") == "text"
    ]
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def _submit(message: Dict[str, Any]) -> Dict[str, Any]:
    """Submit one A2A user message as an agent chat task."""
    text = _message_text(message)
    if not text:
        raise _A2AError(-32602, "message.text is required")
    payload = {
        "session_id": f"a2a-{uuid.uuid4().hex[:10]}",
        "input": [
            {"role": "user", "content": [{"type": "text", "text": text}]},
        ],
    }
    agent_id = str(message.get("agent_id") or "default") or "default"
    try:
        result = submit_agent_chat_task(
            None,
            payload,
            agent_id,
            int(DEFAULT_AGENT_API_TIMEOUT),
        )
    except Exception as exc:  # noqa: BLE001 - protocol boundary
        raise _A2AError(-32000, f"agent runtime unreachable: {exc}") from exc
    if isinstance(result, dict) and result.get("error"):
        raise _A2AError(-32000, str(result["error"]))
    task_id = (result or {}).get("task_id") if isinstance(result, dict) else ""
    if not task_id:
        raise _A2AError(-32000, "task submission returned no task_id")
    return {"task_id": str(task_id), "agent_id": agent_id}


def _task_state(status: Dict[str, Any]) -> str:
    raw = str(
        status.get("status") or status.get("state") or "",
    ).lower()
    if raw in _DONE_STATES:
        return "completed"
    if raw in _FAILED_STATES:
        return "failed"
    return "working"


def _extract_text(status: Dict[str, Any]) -> str:
    for key in ("text", "output", "content", "result"):
        value = status.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            chunks = [
                str(item.get("text") or "")
                for item in value
                if isinstance(item, dict)
            ]
            joined = "\n".join(chunk for chunk in chunks if chunk)
            if joined:
                return joined
    return ""


def _poll(task_id: str, agent_id: str) -> Dict[str, Any]:
    """Fetch current task status (raises nothing; raw dict back)."""
    return get_agent_chat_task_status(
        None,
        task_id,
        to_agent=agent_id or None,
        timeout=int(DEFAULT_AGENT_API_TIMEOUT),
    )


def _task_payload(
    task_id: str,
    status: Dict[str, Any],
) -> Dict[str, Any]:
    state = _task_state(status)
    payload: Dict[str, Any] = {
        "kind": "task",
        "taskId": task_id,
        "contextId": task_id,
        "status": {"state": state},
    }
    if state == "completed":
        text = _extract_text(status)
        if text:
            payload["artifacts"] = [
                {"parts": [{"kind": "text", "text": text}]},
            ]
    error = status.get("error")
    if isinstance(error, str) and error:
        payload["status"]["message"] = error
    return payload


def _agent_message(text: str, task_id: str) -> Dict[str, Any]:
    return {
        "kind": "message",
        "role": "agent",
        "messageId": f"msg-{uuid.uuid4().hex[:10]}",
        "taskId": task_id,
        "contextId": task_id,
        "parts": [{"kind": "text", "text": text}],
    }


# ------------------------------------------------------------ routes


@well_known_router.get("/.well-known/agent-card.json")
async def well_known_card() -> JSONResponse:
    """Standard A2A discovery document."""
    return JSONResponse(agent_card())


@api_router.post("")
async def a2a_endpoint(request: Request):
    """JSON-RPC 2.0 entry for A2A clients."""
    payload: Any = await request.json()
    if not isinstance(payload, dict):
        return _rpc_response(
            None,
            error={"code": -32600, "message": "Invalid Request"},
        )
    request_id = payload.get("id")
    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    try:
        result = await _dispatch(method, params)
    except _A2AError as exc:
        return _rpc_response(
            request_id,
            error={"code": exc.code, "message": exc.message},
        )
    except Exception as exc:  # noqa: BLE001 - protocol boundary
        return _rpc_response(
            request_id,
            error={"code": -32603, "message": f"Internal error: {exc}"},
        )
    if "id" not in payload:
        return JSONResponse(status_code=202, content=None)
    if isinstance(result, StreamingResponse):
        # message/stream answers with SSE frames, not a JSON envelope
        return result
    return _rpc_response(request_id, result=result)


def _rpc_response(request_id: Any, result: Any = None, error: Any = None):
    body: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    return JSONResponse(body)


async def _dispatch(method: str, params: Dict[str, Any]) -> Any:
    if method == "message/send":
        message = params.get("message") or {}
        if not isinstance(message, dict):
            raise _A2AError(-32602, "params.message must be an object")
        submitted = _submit(message)
        task_id = submitted["task_id"]
        status = await asyncio.to_thread(
            _poll,
            task_id,
            submitted["agent_id"],
        )
        if _task_state(status) == "completed":
            return _agent_message(_extract_text(status), task_id)
        return _task_payload(task_id, status)
    if method == "message/stream":
        message = params.get("message") or {}
        if not isinstance(message, dict):
            raise _A2AError(-32602, "params.message must be an object")
        submitted = _submit(message)
        return _sse_response(submitted["task_id"], submitted["agent_id"])
    if method == "tasks/get":
        task_id = str(
            (params.get("params") or params).get("id")
            or params.get("taskId")
            or "",
        ).strip()
        if not task_id:
            raise _A2AError(-32602, "task id is required")
        status = await asyncio.to_thread(_poll, task_id, "")
        return _task_payload(task_id, status)
    raise _A2AError(-32601, f"Method not found: {method}")


def _sse_response(task_id: str, agent_id: str) -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        first = True
        while True:
            status = await asyncio.to_thread(_poll, task_id, agent_id)
            state = _task_state(status)
            event = (
                _task_payload(task_id, status)
                if state != "completed"
                else _agent_message(_extract_text(status), task_id)
            )
            event["final"] = state in ("completed", "failed")
            if first:
                event["first"] = True
                first = False
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event["final"]:
                yield "data: [DONE]\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
    )


__all__ = ["agent_card", "api_router", "well_known_router"]
