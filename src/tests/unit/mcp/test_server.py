# -*- coding: utf-8 -*-
"""EP-2-20: MCP server — protocol, tools, HTTP endpoint, stdio entry."""

from __future__ import annotations

# pylint: disable=protected-access

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from qwenpaw.mcp_server import build_server
from qwenpaw.mcp_server import tools as mcp_tools


@pytest.fixture(name="workspace")
def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mcp_tools, "_TEMPLATES_DIR", tmp_path / "templates")
    monkeypatch.setattr(mcp_tools, "_store_singleton", None)
    templates = tmp_path / "templates"
    templates.mkdir(parents=True)
    (templates / "mcp-demo.json").write_text(
        json.dumps(
            {
                "id": "mcp-demo",
                "name": "MCP demo",
                "entry": "gate",
                "nodes": [
                    {"id": "gate", "kind": "gate", "params": {}},
                    {
                        "id": "go",
                        "kind": "agent",
                        "params": {"prompt": "run"},
                    },
                ],
                "edges": [
                    {"from": "gate", "to": "go", "when": "approve"},
                ],
            },
        ),
        encoding="utf-8",
    )
    yield tmp_path


# ------------------------------------------------------------ protocol


def test_initialize_handshake(workspace) -> None:
    del workspace  # fixture keeps store isolation
    server = build_server()
    answer = json.loads(
        server.handle_raw(
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            ),
        ),
    )
    result = answer["result"]
    assert result["serverInfo"]["name"] == "qwenpaw"
    assert "tools" in result["capabilities"]


def test_tools_list_carries_schemas(workspace) -> None:
    del workspace  # fixture keeps store isolation
    server = build_server()
    answer = json.loads(
        server.handle_raw(
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ),
    )
    tools = answer["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {
        "agent_chat_submit",
        "agent_task_status",
        "graph_run",
        "graph_resume",
    }
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"


def test_notification_has_no_response(workspace) -> None:
    del workspace  # fixture keeps store isolation
    server = build_server()
    assert (
        server.handle_raw(
            json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            ),
        )
        is None
    )


def test_unknown_method_and_tool_error_codes(workspace) -> None:
    del workspace  # fixture keeps store isolation
    server = build_server()
    answer = json.loads(
        server.handle_raw(
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "nope"}),
        ),
    )
    assert answer["error"]["code"] == -32601
    answer = json.loads(
        server.handle_raw(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "missing", "arguments": {}},
                },
            ),
        ),
    )
    assert answer["error"]["code"] == -32602


# ------------------------------------------------------- graph tools


def test_graph_run_suspends_and_resume_completes(workspace) -> None:
    del workspace  # fixture keeps store isolation
    server = build_server()

    started = json.loads(
        server.handle_raw(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "graph_run",
                        "arguments": {"template_id": "mcp-demo"},
                    },
                },
            ),
        ),
    )
    text = started["result"]["content"][0]["text"]
    assert "'status': 'suspended'" in text
    run_id = text.split("'run_id': '")[1].split("'")[0]

    resumed = json.loads(
        server.handle_raw(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "graph_resume",
                        "arguments": {"run_id": run_id, "route": "approve"},
                    },
                },
            ),
        ),
    )
    assert "'status': 'completed'" in (resumed["result"]["content"][0]["text"])


def test_graph_run_missing_template_is_tool_error(workspace) -> None:
    del workspace  # fixture keeps store isolation
    server = build_server()
    answer = json.loads(
        server.handle_raw(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "graph_run",
                        "arguments": {"template_id": "ghost"},
                    },
                },
            ),
        ),
    )
    assert answer["result"]["isError"] is True


# ------------------------------------------------------------ agent


def test_agent_chat_submit_surfaces_runtime_error(workspace) -> None:
    """No running runtime → the tool returns isError content, not a
    protocol crash (MCP hosts must see tool errors as results)."""
    del workspace  # fixture keeps store isolation
    server = build_server()
    answer = json.loads(
        server.handle_raw(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {
                        "name": "agent_chat_submit",
                        "arguments": {"message": "hi"},
                    },
                },
            ),
        ),
    )
    # connect refused surfaces either as isError text or a submit error
    # string — both are valid tool-level outcomes
    result = answer["result"]
    assert result["content"][0]["type"] == "text"


def test_agent_chat_submit_mocked_task(workspace) -> None:
    del workspace  # fixture keeps store isolation

    class _Response:
        status_code = 200

        def json(self) -> dict:
            return {"task_id": "task-1"}

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs) -> _Response:
            return _Response()

    with patch(
        "qwenpaw.agents.tools.agent_management.create_agent_api_client",
        _Client,
    ):
        server = build_server()
        answer = json.loads(
            server.handle_raw(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {
                            "name": "agent_chat_submit",
                            "arguments": {"message": "hi"},
                        },
                    },
                ),
            ),
        )
    assert "task-1" in answer["result"]["content"][0]["text"]


# ---------------------------------------------------------- transports


def test_http_endpoint(workspace) -> None:
    del workspace  # fixture keeps store isolation
    from fastapi.testclient import TestClient

    from qwenpaw.app._app import app

    with TestClient(app) as client:
        initialized = client.post(
            "/api/mcp-server",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert initialized.status_code == 200
        assert initialized.json()["result"]["serverInfo"]["name"] == "qwenpaw"

        notification = client.post(
            "/api/mcp-server",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert notification.status_code == 202

        listed = client.post(
            "/api/mcp-server",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert len(listed.json()["result"]["tools"]) == 4


def test_stdio_entry_point(workspace) -> None:
    """DoD shape: an external host drives us over stdio."""
    del workspace  # fixture keeps store isolation
    request = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    completed = subprocess.run(
        [sys.executable, "-m", "qwenpaw.mcp_server"],
        input=request + "\n",
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0
    answer = json.loads(completed.stdout.strip())
    assert len(answer["result"]["tools"]) == 4
