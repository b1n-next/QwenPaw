# -*- coding: utf-8 -*-
"""EP-2-21: A2A server + core client tests."""

from __future__ import annotations

# pylint: disable=protected-access

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwenpaw.a2a import client as a2a_client
from qwenpaw.a2a import server as a2a_server
from qwenpaw.app._app import app


@pytest.fixture(name="workspace")
def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(a2a_server, "_TEMPLATES_DIR", tmp_path / "tpl")
    templates = tmp_path / "tpl"
    templates.mkdir(parents=True)
    (templates / "research.json").write_text(
        json.dumps(
            {"id": "research", "name": "Research flow"},
        ),
        encoding="utf-8",
    )
    yield tmp_path


class _TaskRig:
    """Mock the inter-agent chat task API behind the A2A surface."""

    def __init__(self, *, delay: int = 0, text: str = "hello back") -> None:
        self.delay = delay
        self.text = text
        self.calls = 0
        self.submitted: list[dict] = []

    def client(self, *_args, **_kwargs):
        rig = self

        class _Response:
            def __init__(self, payload: dict) -> None:
                self._payload = payload
                self.status_code = 200

            def json(self) -> dict:
                return self._payload

            def raise_for_status(self) -> None:
                return None

        class _Client:
            def __init__(self, *_cargs, **_ckwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_cargs):
                return None

            def post(self, _url, **kwargs) -> _Response:
                rig.submitted.append(kwargs.get("json"))
                return _Response({"task_id": "task-42"})

            def get(self, _url, **_kwargs) -> _Response:
                rig.calls += 1
                if rig.calls <= rig.delay:
                    return _Response(
                        {"task_id": "task-42", "status": "running"},
                    )
                return _Response(
                    {
                        "task_id": "task-42",
                        "status": "completed",
                        "output": [{"type": "text", "text": rig.text}],
                    },
                )

        return _Client()


def _patch(rig: _TaskRig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "qwenpaw.agents.tools.agent_management.create_agent_api_client",
        rig.client,
    )


# --------------------------------------------------------------- card


def test_agent_card_lists_graph_templates(workspace) -> None:
    del workspace  # fixture wires _TEMPLATES_DIR
    with TestClient(app) as client:
        card = client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "qwenpaw-agent"
    assert card["capabilities"]["streaming"] is True
    skill_ids = {skill["id"] for skill in card["skills"]}
    assert {"chat", "research"} <= skill_ids


# -------------------------------------------------------- message/send


def test_send_completed_returns_agent_message(
    workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    rig = _TaskRig()
    _patch(rig, monkeypatch)
    with TestClient(app) as client:
        answer = client.post(
            "/api/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": "hi"}],
                    },
                },
            },
        ).json()
    result = answer["result"]
    assert result["kind"] == "message"
    assert result["role"] == "agent"
    assert result["parts"][0]["text"] == "hello back"
    assert rig.submitted[0]["input"][0]["content"][0]["text"] == "hi"


def test_send_slow_task_returns_working_task(
    workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    rig = _TaskRig(delay=5)
    _patch(rig, monkeypatch)
    with TestClient(app) as client:
        answer = client.post(
            "/api/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": "slow"}],
                    },
                },
            },
        ).json()
        # then tasks/get observes completion once the mock settles
        rig.delay = 0
        task = client.post(
            "/api/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tasks/get",
                "params": {"id": "task-42"},
            },
        ).json()
    assert answer["result"]["kind"] == "task"
    assert answer["result"]["status"]["state"] == "working"
    assert task["result"]["status"]["state"] == "completed"
    assert task["result"]["artifacts"][0]["parts"][0]["text"] == "hello back"


def test_send_empty_text_is_invalid_params(
    workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    _patch(_TaskRig(), monkeypatch)
    with TestClient(app) as client:
        answer = client.post(
            "/api/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "message/send",
                "params": {
                    "message": {"role": "user", "parts": []},
                },
            },
        ).json()
    assert answer["error"]["code"] == -32602


# ------------------------------------------------------ message/stream


def test_stream_emits_events_until_done(
    workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    rig = _TaskRig(delay=2)
    _patch(rig, monkeypatch)
    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/api/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "message/stream",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": "stream"}],
                    },
                },
            },
        ) as response:
            assert response.headers["content-type"].startswith(
                "text/event-stream",
            )
            events = []
            for line in response.iter_lines():
                if line.startswith("data: "):
                    events.append(line[len("data: ") :].strip())
    assert events[-1] == "[DONE]"
    payloads = [json.loads(item) for item in events[:-1]]
    assert payloads[0]["first"] is True
    assert payloads[0]["status"]["state"] == "working"
    assert payloads[-1]["kind"] == "message"
    assert payloads[-1]["parts"][0]["text"] == "hello back"


# ------------------------------------------------- core client (DoD E2E)


@pytest.fixture(name="live_server")
def _live_server():
    """Real uvicorn thread: DoD-grade network stack for the client."""
    import socket
    import threading
    import time

    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline and not server.started:
        time.sleep(0.1)
    assert server.started, "uvicorn failed to start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_core_client_discovers_and_calls(
    workspace,
    live_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DoD: a standard A2A client discovers the card and exchanges a
    message (our core client against a real HTTP server)."""
    del workspace
    _patch(_TaskRig(), monkeypatch)
    card = a2a_client.discover_agent_card(live_server)
    assert card["name"] == "qwenpaw-agent"
    assert card["url"].endswith("/api/a2a")

    target = a2a_client.A2AClient(live_server)
    answer = target.send_message("hi there")
    assert answer["role"] == "agent"
    assert answer["parts"][0]["text"] == "hello back"


def test_core_client_stream(
    workspace,
    live_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    _patch(_TaskRig(), monkeypatch)
    target = a2a_client.A2AClient(live_server)
    events = list(target.stream_message("stream me"))
    assert events[-1]["kind"] == "message"
    assert events[-1]["final"] is True
