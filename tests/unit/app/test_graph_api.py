# -*- coding: utf-8 -*-
"""EP-2-18: graph orchestration API tests (publish → run → gate → resume)."""

from __future__ import annotations

# pylint: disable=protected-access

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwenpaw.app.routers import graph as graph_api


@pytest.fixture(name="workspace")
def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(graph_api, "_TEMPLATES_DIR", tmp_path / "templates")
    monkeypatch.setattr(graph_api, "_store", None)
    monkeypatch.setattr(graph_api, "_executors", {})
    yield tmp_path


@pytest.fixture(name="client")
def _client(workspace: Path) -> TestClient:
    del workspace  # fixture isolates WORKING_DIR-based stores
    from qwenpaw.app._app import app

    with TestClient(app) as test_client:
        yield test_client


_GRAPH = {
    "id": "demo-flow",
    "name": "Demo flow",
    "entry": "gate",
    "nodes": [
        {"id": "gate", "kind": "gate", "params": {"message": "ok to run?"}},
        {
            "id": "go",
            "kind": "agent",
            "params": {"prompt": "do the thing"},
        },
        {
            "id": "halt",
            "kind": "agent",
            "params": {"prompt": "explain why not"},
        },
    ],
    "edges": [
        {"from": "gate", "to": "go", "when": "approve"},
        {"from": "gate", "to": "halt", "when": "deny"},
    ],
}


def _publish(client: TestClient) -> None:
    response = client.post("/api/graph/publish", json={"graph": _GRAPH})
    assert response.status_code == 200


def test_validate_rejects_cycles(client: TestClient) -> None:
    response = client.post(
        "/api/graph/validate",
        json={
            "graph": {
                "id": "cyc",
                "name": "C",
                "entry": "a",
                "nodes": [
                    {"id": "a", "kind": "agent"},
                    {"id": "b", "kind": "agent"},
                ],
                "edges": [
                    {"from": "a", "to": "b"},
                    {"from": "b", "to": "a"},
                ],
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_GRAPH"


def test_publish_then_templates_list(client: TestClient) -> None:
    _publish(client)
    templates = client.get("/api/graph/templates").json()["templates"]
    assert [t["id"] for t in templates] == ["demo-flow"]


def test_run_suspends_at_gate_and_resume_completes(
    client: TestClient,
) -> None:
    _publish(client)
    started = client.post(
        "/api/graph/runs",
        json={"template_id": "demo-flow", "inputs": {"seed": 7}},
    ).json()
    assert started["status"] == "suspended"
    assert started["suspended_at"] == "gate"

    resumed = client.post(
        f"/api/graph/runs/{started['run_id']}/resume",
        json={"route": "approve"},
    ).json()
    assert resumed["status"] == "completed"
    go = next(n for n in resumed["nodes"] if n["node_id"] == "go")
    assert go["outputs"]["prompt"] == "do the thing"
    assert all(n["node_id"] != "halt" for n in resumed["nodes"])
    assert resumed["state"]["inputs"]["seed"] == 7


def test_resume_deny_routes_alternative_branch(client: TestClient) -> None:
    _publish(client)
    started = client.post(
        "/api/graph/runs",
        json={"template_id": "demo-flow"},
    ).json()
    resumed = client.post(
        f"/api/graph/runs/{started['run_id']}/resume",
        json={"route": "deny"},
    ).json()
    assert resumed["status"] == "completed"
    halt = next(n for n in resumed["nodes"] if n["node_id"] == "halt")
    assert halt["outputs"]["prompt"] == "explain why not"


def test_missing_template_404(client: TestClient) -> None:
    response = client.post(
        "/api/graph/runs",
        json={"template_id": "nope"},
    )
    assert response.status_code == 404


def test_get_run_returns_checkpoints(client: TestClient) -> None:
    _publish(client)
    started = client.post(
        "/api/graph/runs",
        json={"template_id": "demo-flow"},
    ).json()
    fetched = client.get(f"/api/graph/runs/{started['run_id']}").json()
    assert fetched["run"]["status"] == "suspended"
    assert fetched["run"]["state"]["template"] == "demo-flow"
    assert any(n["status"] == "suspended" for n in fetched["nodes"])
