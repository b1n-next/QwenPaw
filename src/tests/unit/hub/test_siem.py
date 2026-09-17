# -*- coding: utf-8 -*-
"""F9: SIEM relay — audit mirroring, admin configuration."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.siem import SiemRelay


def test_relay_unit_semantics() -> None:
    relay = SiemRelay()
    relay.enqueue({"a": 1})  # no endpoint: no-op
    assert relay.stats()["queued"] == 0
    relay.configure("http://127.0.0.1:1/sink")
    relay.enqueue({"b": 2})
    assert relay.stats()["queued"] == 1
    # unreachable endpoint: batch dropped, error recorded, no raise
    attempted = relay.flush()
    assert attempted == 0
    stats = relay.stats()
    assert stats["dropped"] == 1
    assert stats["last_error"]


def test_relay_delivers_batches() -> None:
    received: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request.read())
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    relay = SiemRelay(
        batch_size=2,
        flush_interval=3600,
        transport=transport,
    )
    relay.configure(
        "http://siem.test/sink",
        secret="k-1",
        batch_size=2,
    )
    relay.enqueue({"i": 1})
    relay.enqueue({"i": 2})  # hits batch size -> flush
    assert relay.stats()["sent"] == 2
    assert relay.stats()["queued"] == 0
    assert len(received) == 1
    rows = [json.loads(line) for line in received[0].splitlines()]
    assert [row["i"] for row in rows] == [1, 2]


def test_admin_configure_and_mirror(tmp_path: Path) -> None:
    sink: list[bytes] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            sink.append(request.read())
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    # rebuild the relay on the mock transport before serving
    app.state.siem_relay = SiemRelay(transport=transport)
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        # validation
        assert (
            client.put(
                "/api/hub/admin/siem",
                json={"endpoint": "ftp://bad"},
                headers=headers,
            ).status_code
            == 422
        )
        # configure with a tiny batch so events ship immediately
        configured = client.put(
            "/api/hub/admin/siem",
            json={
                "endpoint": "http://siem.test/sink",
                "secret": "topsecret",
                "batch_size": 1,
            },
            headers=headers,
        )
        assert configured.status_code == 200
        assert configured.json()["endpoint"].endswith("/sink")
        # the configure event itself was audited AFTER the relay
        # went live; trigger another audited action to mirror
        pruned = client.post(
            "/api/hub/admin/audit/prune",
            json={"before": "2000-01-01"},
            headers=headers,
        )
        assert pruned.status_code == 200
        client.post(
            "/api/hub/admin/users",
            json={"username": "member", "password": "pw-123456"},
            headers=headers,
        )
        client.get("/api/hub/admin/siem", headers=headers)
        status = client.get(
            "/api/hub/admin/siem",
            headers=headers,
        ).json()
        assert status["sent"] >= 1
        assert status["last_error"] is None
        # SIEM sink received JSONL rows
        with lock:
            rows = [
                json.loads(line)
                for chunk in sink
                for line in chunk.decode("utf-8").splitlines()
            ]
        assert rows
        assert any(row["action"] == "audit.prune" for row in rows)
        # disable
        disabled = client.put(
            "/api/hub/admin/siem",
            json={"endpoint": ""},
            headers=headers,
        )
        assert disabled.json()["endpoint"] is None
