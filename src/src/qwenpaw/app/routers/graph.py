# -*- coding: utf-8 -*-
"""Graph orchestration API (EP-2-18).

Canvas-facing endpoints over the EP-2-17 engine:
    POST /api/graph/validate    — schema validation before saving
    POST /api/graph/publish     — persist a template
    GET  /api/graph/templates   — published templates (the hall list)
    POST /api/graph/runs        — start a run from a template
    GET  /api/graph/runs/{id}   — run + node checkpoints
    POST /api/graph/runs/{id}/resume — resolve a suspended gate

Node semantics in this ticket: ``agent`` nodes echo their prompt
specification (LLM wiring lands with EP-2-19 template instantiation);
``gate`` nodes suspend exactly like loop human gates.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...constant import WORKING_DIR
from ...graph.executor import (
    GraphExecutor,
    NodeResult,
    Suspension,
)
from ...graph.schema import GraphSchema
from ...graph.state_store import GraphStateStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/graph", tags=["graph"])

_TEMPLATES_DIR = Path(WORKING_DIR) / "graph_templates"

_store_lock = threading.Lock()
_store: GraphStateStore | None = None
_executors: Dict[str, GraphExecutor] = {}
_executors_lock = threading.Lock()


def _state_store() -> GraphStateStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = GraphStateStore(
                Path(WORKING_DIR) / "graph_runs.db",
            )
        return _store


# ------------------------------------------------------------- handlers


async def _agent_handler(node: Any, state: Dict[str, Any]) -> NodeResult:
    """Echo the prompt spec; EP-2-19 swaps in real model turns."""
    prompt = str(node.params.get("prompt") or "")
    return NodeResult(
        outputs={
            "kind": "agent",
            "prompt": prompt,
            "referenced": bool(state.get("nodes")),
        },
        route=str(node.params.get("route") or "default"),
    )


async def _gate_handler(node: Any, state: Dict[str, Any]) -> NodeResult:
    """Suspend like the loop human gate; resume supplies the route."""
    raise Suspension(str(node.params.get("message") or "Human gate"))


def _build_executor(schema: GraphSchema) -> GraphExecutor:
    return GraphExecutor(
        schema,
        _state_store(),
        {"agent": _agent_handler, "gate": _gate_handler},
    )


# ------------------------------------------------------------- models


class ValidateRequest(BaseModel):
    graph: Dict[str, Any]


class PublishRequest(BaseModel):
    graph: Dict[str, Any]


class StartRunRequest(BaseModel):
    template_id: str = Field(min_length=1, max_length=64)
    inputs: Dict[str, Any] = Field(default_factory=dict)


class ResumeRequest(BaseModel):
    route: str = Field(min_length=1, max_length=32)
    outputs: Dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------ endpoints


def _parse_schema(graph: Dict[str, Any]) -> GraphSchema:
    try:
        return GraphSchema.model_validate(graph)
    except Exception as exc:  # noqa: BLE001 - surface pydantic detail
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_GRAPH", "message": str(exc)},
        ) from None


@router.post("/validate")
async def validate_graph(request: ValidateRequest) -> Dict[str, Any]:
    schema = _parse_schema(request.graph)
    return {"valid": True, "node_count": len(schema.nodes)}


@router.post("/publish")
async def publish_template(request: PublishRequest) -> Dict[str, Any]:
    schema = _parse_schema(request.graph)
    _TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    path = _TEMPLATES_DIR / f"{schema.id}.json"
    path.write_text(
        json.dumps(schema.model_dump(by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )
    return {"template": schema.model_dump(by_alias=True)}


@router.get("/templates")
async def list_templates() -> Dict[str, Any]:
    if not _TEMPLATES_DIR.exists():
        return {"templates": []}
    templates = []
    for path in sorted(_TEMPLATES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        templates.append(data)
    return {"templates": templates}


def _load_template(template_id: str) -> GraphSchema:
    path = _TEMPLATES_DIR / f"{template_id}.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail={
                "code": "TEMPLATE_NOT_FOUND",
                "message": f"Graph template '{template_id}' not found",
            },
        )
    return _parse_schema(json.loads(path.read_text(encoding="utf-8")))


@router.post("/runs")
async def start_run(request: StartRunRequest) -> Dict[str, Any]:
    schema = _load_template(request.template_id)
    run_id = f"graph-{uuid.uuid4().hex[:12]}"
    executor = _build_executor(schema)
    with _executors_lock:
        _executors[run_id] = executor
    result = await executor.run(
        run_id=run_id,
        initial_state={"inputs": request.inputs, "template": schema.id},
    )
    return _run_payload(result)


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> Dict[str, Any]:
    run = _state_store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "run": run,
        "nodes": _state_store().list_node_runs(run_id),
    }


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, request: ResumeRequest) -> Dict[str, Any]:
    with _executors_lock:
        executor = _executors.get(run_id)
    if executor is None:
        # EP-2-18 follow-up: rebuild executors for runs suspended before
        # a process restart (run state lives in graph_runs.db; the
        # executor itself is stateless beyond schema + handlers).
        executor = _rebuild_executor(run_id)
        if executor is not None:
            with _executors_lock:
                _executors[run_id] = executor
    if executor is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RUN_NOT_RESUMABLE",
                "message": "Run executor is no longer in this process",
            },
        )
    try:
        result = await executor.resume(
            run_id,
            request.route,
            outputs=request.outputs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _run_payload(result)


def _rebuild_executor(run_id: str) -> GraphExecutor | None:
    """Recreate an executor for a persisted suspended run.

    Returns None when the run is unknown, already terminal, or its
    template was deleted after the run suspended (410 semantics).
    """
    run = _state_store().get_run(run_id)
    if run is None or run.get("status") != "suspended":
        return None
    try:
        schema = _load_template(str(run.get("graph_id") or ""))
    except HTTPException:
        logger.warning(
            "graph run %s is suspended but its template '%s' is gone; "
            "resume stays unavailable until the template is re-published",
            run_id,
            run.get("graph_id"),
        )
        return None
    logger.info(
        "graph run %s executor rebuilt from template '%s' after restart",
        run_id,
        schema.id,
    )
    return _build_executor(schema)


def _run_payload(result: Any) -> Dict[str, Any]:
    return {
        "run_id": result.run_id,
        "status": result.status,
        "suspended_at": result.suspended_at,
        "error": result.error,
        "state": result.state,
        "nodes": _state_store().list_node_runs(result.run_id),
    }


__all__ = ["router"]
