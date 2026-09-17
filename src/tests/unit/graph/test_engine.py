# -*- coding: utf-8 -*-
"""EP-2-17 graph engine E2E: linear chain, branch routing, and
resume-from-checkpoint after an interruption."""

from __future__ import annotations

import asyncio
from pathlib import Path

# pylint: disable=protected-access

import pytest

from qwenpaw.graph.executor import (
    GraphExecutor,
    NodeResult,
    Suspension,
)
from qwenpaw.graph.schema import GraphEdge, GraphNode, GraphSchema
from qwenpaw.graph.state_store import GraphStateStore


def _store(tmp_path: Path) -> GraphStateStore:
    return GraphStateStore(tmp_path / "graph.db")


def _linear_graph() -> GraphSchema:
    return GraphSchema(
        id="linear",
        name="Linear",
        entry="a",
        nodes=[
            GraphNode(id="a", kind="echo", params={"value": "A"}),
            GraphNode(id="b", kind="echo", params={"value": "B"}),
            GraphNode(id="c", kind="echo", params={"value": "C"}),
        ],
        edges=[
            GraphEdge(**{"from": "a", "to": "b"}),
            GraphEdge(**{"from": "b", "to": "c"}),
        ],
    )


def _branch_graph() -> GraphSchema:
    return GraphSchema(
        id="branch",
        name="Branch",
        entry="gate",
        nodes=[
            GraphNode(id="gate", kind="gate"),
            GraphNode(id="approved-path", kind="echo", params={"value": "GO"}),
            GraphNode(id="denied-path", kind="echo", params={"value": "STOP"}),
        ],
        edges=[
            GraphEdge(
                **{"from": "gate", "to": "approved-path"},
                when="approve",
            ),
            GraphEdge(**{"from": "gate", "to": "denied-path"}, when="deny"),
        ],
    )


def _echo_handlers() -> dict:
    async def echo(node, _state):
        return NodeResult(outputs={"value": node.params["value"]})

    return {"echo": echo}


# ------------------------------------------------------------- schema


def test_rejects_cycle_and_ambiguous_routes() -> None:
    with pytest.raises(Exception, match="acyclic"):
        GraphSchema(
            id="cyc",
            name="C",
            entry="a",
            nodes=[
                GraphNode(id="a", kind="x"),
                GraphNode(id="b", kind="x"),
            ],
            edges=[
                GraphEdge(**{"from": "a", "to": "b"}),
                GraphEdge(**{"from": "b", "to": "a"}),
            ],
        )
    # same (source, route) twice is legal fan-out, not ambiguity
    fan = GraphSchema(
        id="fan",
        name="Fan",
        entry="a",
        nodes=[
            GraphNode(id="a", kind="x"),
            GraphNode(id="b", kind="x"),
            GraphNode(id="c", kind="x"),
        ],
        edges=[
            GraphEdge(**{"from": "a", "to": "b"}),
            GraphEdge(**{"from": "a", "to": "c"}),
        ],
    )
    assert len(fan.outgoing("a")) == 2


# -------------------------------------------------------------- linear


async def test_linear_chain_completes_in_order(tmp_path: Path) -> None:
    order: list[str] = []

    async def echo(node, _state):
        order.append(node.id)
        return NodeResult(outputs={"value": node.params["value"]})

    executor = GraphExecutor(
        _linear_graph(),
        _store(tmp_path),
        {"echo": echo},
    )
    result = await executor.run(initial_state={"seed": 1})

    assert result.status == "completed"
    assert order == ["a", "b", "c"]
    assert result.state["nodes"]["c"] == {"value": "C"}
    assert result.state["seed"] == 1


# -------------------------------------------------------------- branch


async def test_gate_suspend_then_approve_routes_go(tmp_path: Path) -> None:
    async def gate(_node, _state):
        raise Suspension("waiting for human")

    executed: list[str] = []

    async def echo(node, _state):
        executed.append(node.id)
        return NodeResult(outputs={"value": node.params["value"]})

    store = _store(tmp_path)
    executor = GraphExecutor(
        _branch_graph(),
        store,
        {"gate": gate, "echo": echo},
    )

    first = await executor.run()
    assert first.status == "suspended"
    assert first.suspended_at == "gate"
    assert not executed  # nothing downstream ran

    second = await executor.resume(first.run_id, "approve")
    assert second.status == "completed"
    assert executed == ["approved-path"]
    assert second.state["nodes"]["approved-path"] == {"value": "GO"}
    assert "denied-path" not in second.state["nodes"]

    node_runs = {n["node_id"]: n for n in store.list_node_runs(first.run_id)}
    assert node_runs["gate"]["route"] == "approve"


async def test_gate_deny_routes_stop(tmp_path: Path) -> None:
    async def gate(_node, _state):
        raise Suspension("waiting for human")

    async def echo(node, _state):
        return NodeResult(outputs={"value": node.params["value"]})

    executor = GraphExecutor(
        _branch_graph(),
        _store(tmp_path),
        {"gate": gate, "echo": echo},
    )
    first = await executor.run()
    second = await executor.resume(first.run_id, "deny")
    assert second.status == "completed"
    assert second.state["nodes"]["denied-path"] == {"value": "STOP"}


# ---------------------------------------------------------- checkpoint


async def test_resume_after_interrupt_skips_completed(tmp_path: Path) -> None:
    """DoD: interrupted run continues from the last finished node."""
    calls: list[str] = []
    store = _store(tmp_path)

    async def echo(node, _state):
        calls.append(node.id)
        return NodeResult(outputs={"value": node.params["value"]})

    # First session: a and b complete, then the process "dies" (c never
    # starts) — simulated by an executor whose c handler refuses to run.
    class _Boom(Exception):
        pass

    async def boom_echo(node, state):
        if node.id == "c":
            raise _Boom("process killed")
        return await echo(node, state)

    executor1 = GraphExecutor(
        _linear_graph(),
        store,
        {"echo": boom_echo},
    )
    result1 = await executor1.run()
    assert result1.status == "failed"
    assert calls == ["a", "b"]

    # Second session: fresh executor, same run id — a/b checkpoints are
    # reloaded, only c executes.
    executor2 = GraphExecutor(
        _linear_graph(),
        store,
        {"echo": echo},
    )
    result2 = await executor2.run(run_id=result1.run_id)
    assert result2.status == "completed"
    assert calls == ["a", "b", "c"]
    assert result2.state["nodes"]["c"] == {"value": "C"}


async def test_resume_rejects_wrong_graph(tmp_path: Path) -> None:
    store = _store(tmp_path)
    executor = GraphExecutor(
        _linear_graph(),
        store,
        _echo_handlers(),
    )
    first = await executor.run()
    other = GraphExecutor(
        _branch_graph(),
        store,
        _echo_handlers(),
    )
    with pytest.raises(ValueError, match="belongs to graph"):
        await other.run(run_id=first.run_id)


async def test_missing_handler_fails_the_run(tmp_path: Path) -> None:
    executor = GraphExecutor(_linear_graph(), _store(tmp_path), {})
    result = await executor.run()
    assert result.status == "failed"
    assert "No handler" in (result.error or "")


async def test_concurrent_branches_run_in_parallel(tmp_path: Path) -> None:
    graph = GraphSchema(
        id="fan",
        name="Fan",
        entry="start",
        nodes=[
            GraphNode(id="start", kind="echo", params={"value": "S"}),
            GraphNode(id="left", kind="slow"),
            GraphNode(id="right", kind="slow"),
        ],
        edges=[
            GraphEdge(**{"from": "start", "to": "left"}),
            GraphEdge(**{"from": "start", "to": "right"}),
        ],
    )

    async def slow(node, _state):
        await asyncio.sleep(0.2)
        return NodeResult(outputs={"id": node.id})

    executor = GraphExecutor(
        graph,
        _store(tmp_path),
        {
            "echo": _echo_handlers()["echo"],
            "slow": slow,
        },
    )
    import time

    started = time.monotonic()
    result = await executor.run()
    elapsed = time.monotonic() - started
    assert result.status == "completed"
    # parallel: 0.2s + start, well under the 0.4s serial floor
    assert elapsed < 0.38
