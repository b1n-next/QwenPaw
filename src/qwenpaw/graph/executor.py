# -*- coding: utf-8 -*-
"""Async topological graph executor (EP-2-17).

Execution model:
    - nodes become ready when their inbound edges are all *activated*;
      an edge activates when its source completed with a route equal
      to the edge's ``when`` (default route matches ``when``-less edges)
    - ready nodes run concurrently (asyncio.gather); a pure chain is
      therefore executed in order
    - a handler may suspend (human gate): the run checkpoints as
      ``suspended`` and ``resume()`` continues from that node with an
      externally supplied route (e.g. approve/deny)
    - ``run()`` on an existing run id reloads completed checkpoints
      first, so interrupted runs continue from finished nodes

Handlers are registered per node kind:
    executor.register("agent", handler)
    async def handler(node, state) -> NodeResult
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .schema import GraphEdge, GraphSchema
from .state_store import GraphStateStore

logger = logging.getLogger(__name__)

Handler = Callable[[Any, Dict[str, Any]], Awaitable["NodeResult"]]

DEFAULT_ROUTE = "default"


class Suspension(Exception):
    """Raised by a handler to checkpoint and await a human decision."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason or "node suspended")
        self.reason = reason


@dataclass
class NodeResult:
    """One node's outcome."""

    outputs: Dict[str, Any] = field(default_factory=dict)
    route: str = DEFAULT_ROUTE


@dataclass
class GraphRunResult:
    """Terminal snapshot of one run (or suspension point)."""

    run_id: str
    status: str  # completed | suspended | failed
    state: Dict[str, Any] = field(default_factory=dict)
    suspended_at: Optional[str] = None
    error: Optional[str] = None


class GraphExecutor:
    """Executes a validated schema against pluggable kind handlers."""

    def __init__(
        self,
        schema: GraphSchema,
        state_store: GraphStateStore,
        handlers: Optional[Dict[str, Handler]] = None,
    ) -> None:
        self._schema = schema
        self._store = state_store
        self._handlers: Dict[str, Handler] = dict(handlers or {})

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    # ------------------------------------------------------------ run

    async def run(
        self,
        run_id: Optional[str] = None,
        initial_state: Optional[Dict[str, Any]] = None,
    ) -> GraphRunResult:
        """Execute the graph; resumes from checkpoints when run exists."""
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        state: Dict[str, Any] = dict(initial_state or {})

        existing = self._store.get_run(run_id)
        if existing is None:
            self._store.create_run(run_id, self._schema.id)
        else:
            if existing["graph_id"] != self._schema.id:
                raise ValueError(
                    f"Run {run_id} belongs to graph "
                    f"'{existing['graph_id']}', not '{self._schema.id}'",
                )
            for node_id, (outputs, _route) in self._store.completed_nodes(
                run_id,
            ).items():
                state.setdefault("nodes", {}).setdefault(node_id, outputs)
                logger.info(
                    "GraphExecutor: resuming run %s — node %s already "
                    "completed, skipped",
                    run_id,
                    node_id,
                )

        state.setdefault("nodes", {})
        activated: set[tuple[str, str]] = set()  # (from_node, when)
        completed: Dict[str, str] = {}  # node_id -> route

        for node_id, (_outputs, route) in self._store.completed_nodes(
            run_id,
        ).items():
            completed[node_id] = route
            for edge in self._schema.outgoing(node_id):
                if edge.when == route:
                    activated.add((edge.from_, edge.when))

        try:
            return await self._execute_frontier(
                run_id,
                state,
                activated,
                completed,
            )
        except Suspension as suspension:
            return await self._suspend(
                run_id,
                state,
                suspension,
            )
        except Exception as exc:  # noqa: BLE001 - checkpoint any failure
            logger.exception(
                "GraphExecutor: run %s failed: %s",
                run_id,
                exc,
            )
            self._store.set_run_status(
                run_id,
                "failed",
                state=state,
                error=str(exc),
            )
            return GraphRunResult(
                run_id=run_id,
                status="failed",
                state=state,
                error=str(exc),
            )

    # -------------------------------------------------------- internals

    async def _execute_frontier(
        self,
        run_id: str,
        state: Dict[str, Any],
        activated: set[tuple[str, str]],
        completed: Dict[str, str],
    ) -> GraphRunResult:
        while True:
            ready = self._ready_nodes(activated, completed)
            if not ready:
                break
            results = await asyncio.gather(
                *(self._run_one(run_id, node_id, state) for node_id in ready),
            )
            progressed = False
            for node_id, result in zip(ready, results):
                completed[node_id] = result.route
                progressed = True
                for edge in self._schema.outgoing(node_id):
                    if edge.when == result.route:
                        activated.add((edge.from_, edge.when))
            if not progressed:
                break

        self._store.set_run_status(
            run_id,
            "completed",
            state=state,
        )
        return GraphRunResult(run_id=run_id, status="completed", state=state)

    def _ready_nodes(
        self,
        activated: set[tuple[str, str]],
        completed: Dict[str, str],
    ) -> List[str]:
        """Nodes whose every inbound edge is activated and not yet run."""
        ready: List[str] = []
        incoming: Dict[str, List[GraphEdge]] = {}
        for edge in self._schema.edges:
            incoming.setdefault(edge.to, []).append(edge)
        for node in self._schema.nodes:
            if node.id in completed:
                continue
            edges = incoming.get(node.id, [])
            if node.id == self._schema.entry and not edges:
                ready.append(node.id)
                continue
            if edges and all(
                (edge.from_, edge.when) in activated for edge in edges
            ):
                ready.append(node.id)
        return ready

    async def _run_one(
        self,
        run_id: str,
        node_id: str,
        state: Dict[str, Any],
    ) -> NodeResult:
        node = self._schema.node(node_id)
        handler = self._handlers.get(node.kind)
        if handler is None:
            raise ValueError(
                f"No handler registered for node kind '{node.kind}' "
                f"(node '{node_id}')",
            )
        self._store.mark_node_started(run_id, node_id)
        try:
            result = await handler(node, state)
        except Suspension:
            # Tag the exact gated node before the exception unwinds the
            # gather, so concurrent siblings cannot be mistaken for it.
            self._store.mark_node_suspended(run_id, node_id)
            raise
        state.setdefault("nodes", {})[node_id] = result.outputs
        self._store.mark_node_completed(
            run_id,
            node_id,
            result.outputs,
            result.route,
        )
        logger.info(
            "GraphExecutor: node %s completed (route=%s)",
            node_id,
            result.route,
        )
        return result

    async def _suspend(
        self,
        run_id: str,
        state: Dict[str, Any],
        suspension: Suspension,
    ) -> GraphRunResult:
        # Tagged by _run_one before the exception unwound the gather.
        suspended_node = next(
            (
                entry["node_id"]
                for entry in reversed(self._store.list_node_runs(run_id))
                if entry["status"] == "suspended"
            ),
            None,
        )
        if suspended_node is None:  # pragma: no cover - defensive
            suspended_node = ""
        self._store.mark_node_suspended(run_id, suspended_node)
        self._store.set_run_status(run_id, "suspended", state=state)
        logger.info(
            "GraphExecutor: run %s suspended at node %s (%s)",
            run_id,
            suspended_node,
            suspension.reason,
        )
        return GraphRunResult(
            run_id=run_id,
            status="suspended",
            state=state,
            suspended_at=suspended_node,
        )

    # ------------------------------------------------------------ resume

    async def resume(
        self,
        run_id: str,
        route: str,
        outputs: Optional[Dict[str, Any]] = None,
    ) -> GraphRunResult:
        """Continue a suspended run by resolving the gated node."""
        run = self._store.get_run(run_id)
        if run is None or run["status"] != "suspended":
            raise ValueError(f"Run {run_id} is not suspended")

        suspended_node = next(
            (
                entry["node_id"]
                for entry in reversed(self._store.list_node_runs(run_id))
                if entry["status"] == "suspended"
            ),
            None,
        )
        if suspended_node is None:  # pragma: no cover - defensive
            raise ValueError(f"Run {run_id} has no suspended node")

        state = run["state"]
        effective_outputs = dict(outputs or {})
        state.setdefault("nodes", {})[suspended_node] = effective_outputs
        self._store.mark_node_completed(
            run_id,
            suspended_node,
            effective_outputs,
            route,
        )
        self._store.set_run_status(run_id, "running", state=state)
        return await self.run(run_id=run_id, initial_state=state)


__all__ = [
    "DEFAULT_ROUTE",
    "GraphExecutor",
    "GraphRunResult",
    "Handler",
    "NodeResult",
    "Suspension",
]
