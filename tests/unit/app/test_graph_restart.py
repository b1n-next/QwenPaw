# -*- coding: utf-8 -*-
"""EP-2-18 follow-up: executor rebuild across process restarts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from qwenpaw.app import _app as app_module
from qwenpaw.app.routers import graph as graph_router


def _publish_gate_graph(client: TestClient) -> str:
    """Two-node graph: agent -> gate (suspends)."""
    graph = {
        "id": "restart-e2e",
        "name": "restart-e2e",
        "entry": "n1",
        "nodes": [
            {
                "id": "n1",
                "kind": "agent",
                "params": {"prompt": "hello"},
            },
            {"id": "n2", "kind": "gate", "params": {"message": "decide"}},
        ],
        "edges": [
            {"from": "n1", "to": "n2", "when": "default"},
        ],
    }
    response = client.post("/api/graph/publish", json={"graph": graph})
    assert response.status_code == 200, response.text
    return "restart-e2e"


def _start_and_suspend(
    client: TestClient,
    template_id: str,
) -> str:
    response = client.post(
        "/api/graph/runs",
        json={"template_id": template_id, "inputs": {"x": 1}},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "suspended"
    return payload["run_id"]


def _simulate_restart() -> None:
    """Drop all in-process executors, keep the SQLite state."""
    # pylint: disable=protected-access
    with graph_router._executors_lock:
        graph_router._executors.clear()


@pytest.fixture(name="client")
def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> TestClient:
    monkeypatch.setattr(app_module, "WORKING_DIR", str(tmp_path))
    monkeypatch.setattr(graph_router, "WORKING_DIR", str(tmp_path))
    monkeypatch.setattr(graph_router, "_TEMPLATES_DIR", tmp_path / "gt")
    monkeypatch.setattr(graph_router, "_store", None)
    with TestClient(app_module.app, raise_server_exceptions=False) as c:
        yield c


def test_resume_after_restart_rebuilds_executor(
    client: TestClient,
) -> None:
    template_id = _publish_gate_graph(client)
    run_id = _start_and_suspend(client, template_id)

    _simulate_restart()
    # pylint: disable=protected-access
    assert not graph_router._executors

    response = client.post(
        f"/api/graph/runs/{run_id}/resume",
        json={"route": "default", "outputs": {"approved": True}},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] in ("completed", "suspended")
    # pylint: disable=protected-access
    assert run_id in graph_router._executors


def test_restart_then_get_run_still_readable(
    client: TestClient,
) -> None:
    template_id = _publish_gate_graph(client)
    run_id = _start_and_suspend(client, template_id)

    _simulate_restart()
    response = client.get(f"/api/graph/runs/{run_id}")
    assert response.status_code == 200
    assert response.json()["run"]["status"] == "suspended"


def test_resume_unknown_run_still_409(client: TestClient) -> None:
    response = client.post(
        "/api/graph/runs/ghost/resume",
        json={"route": "default"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RUN_NOT_RESUMABLE"


def test_resume_after_template_deleted_stays_409(
    client: TestClient,
    tmp_path: Path,
) -> None:
    template_id = _publish_gate_graph(client)
    run_id = _start_and_suspend(client, template_id)

    _simulate_restart()
    (tmp_path / "gt" / f"{template_id}.json").unlink()

    response = client.post(
        f"/api/graph/runs/{run_id}/resume",
        json={"route": "default"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RUN_NOT_RESUMABLE"


def test_terminal_run_not_rebuilt(client: TestClient) -> None:
    template_id = _publish_gate_graph(client)
    run_id = _start_and_suspend(client, template_id)
    # finish the run first
    client.post(
        f"/api/graph/runs/{run_id}/resume",
        json={"route": "default"},
    )
    _simulate_restart()
    # a succeeded/failed run must not come back to life
    # pylint: disable=protected-access
    rebuilt = graph_router._rebuild_executor(run_id)
    assert rebuilt is None


def test_two_gate_chain_survives_restart(
    client: TestClient,
    tmp_path: Path,
) -> None:
    del tmp_path  # template paths come from the patched router dirs
    graph = {
        "id": "chain-restart",
        "name": "chain-restart",
        "entry": "a",
        "nodes": [
            {"id": "a", "kind": "agent", "params": {"prompt": "1"}},
            {"id": "g1", "kind": "gate", "params": {"message": "one"}},
            {"id": "g2", "kind": "gate", "params": {"message": "two"}},
        ],
        "edges": [
            {"from": "a", "to": "g1", "when": "default"},
            {"from": "g1", "to": "g2", "when": "default"},
        ],
    }
    response = client.post("/api/graph/publish", json={"graph": graph})
    assert response.status_code == 200
    run_id = _start_and_suspend(client, "chain-restart")

    _simulate_restart()
    first = client.post(
        f"/api/graph/runs/{run_id}/resume",
        json={"route": "default"},
    ).json()
    assert first["status"] == "suspended", first

    _simulate_restart()
    second = client.post(
        f"/api/graph/runs/{run_id}/resume",
        json={"route": "default"},
    ).json()
    assert second["status"] in ("completed", "suspended")


def _persisted_run_state(client: TestClient, run_id: str) -> Dict[str, Any]:
    response = client.get(f"/api/graph/runs/{run_id}")
    return response.json()["run"]


def test_state_persists_across_restart(
    client: TestClient,
    tmp_path: Path,
) -> None:
    del tmp_path  # template paths come from the patched router dirs
    template_id = _publish_gate_graph(client)
    run_id = _start_and_suspend(client, template_id)
    before = _persisted_run_state(client, run_id)

    _simulate_restart()
    after = _persisted_run_state(client, run_id)
    assert before["state"] == after["state"]
    assert json.dumps(before["state"], sort_keys=True)
