# -*- coding: utf-8 -*-
"""F7: tenant-scoped runtime log tail retention."""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.runtime_logs import (
    RuntimeLogCollector,
    RuntimeLogStore,
)


class _Record:
    def __init__(self, **kwargs: object) -> None:
        self.runtime_id = "rt-1"
        self.tenant_id = "t-1"
        self.host = "127.0.0.1"
        self.port = 6199
        self.state = "running"
        self.owner_user_id = "owner-1"
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Service:
    def __init__(self, records: list[_Record]) -> None:
        self._records = records

    def list(self) -> list[_Record]:
        return self._records


class _Vault:
    def get_runtime_secret(  # pylint: disable=unused-argument
        self,
        *,
        tenant_id,
        runtime_id,
        name,
    ):
        return "tok"


def _collector(
    tmp_path: Path,
    payloads: dict[str, dict],
) -> RuntimeLogCollector:
    store = RuntimeLogStore(tmp_path / "hub.db")

    async def handler(request: httpx.Request) -> httpx.Response:
        port = request.url.port
        return httpx.Response(
            200,
            json=payloads.get(str(port), {"content": ""}),
        )

    return RuntimeLogCollector(
        runtime_service=_Service([_Record()]),
        credential_vault=_Vault(),
        store=store,
        transport=httpx.MockTransport(handler),
    )


def test_collect_stores_tail_and_prunes_window(
    tmp_path: Path,
) -> None:
    payload = {
        "content": "line1\nline2\n",
        "updated_at": 123.0,
        "size": 14,
        "lines": 2,
    }
    collector = _collector(tmp_path, {"6199": payload})
    stored = collector.store
    stored.set_keep(3)
    import asyncio

    first = asyncio.run(collector.collect_once())
    assert first == len("line1\nline2\n")
    rows = stored.latest("rt-1")
    assert len(rows) == 1
    assert rows[0]["content"] == "line1\nline2\n"

    # window prune: older snapshots fall out beyond the keep limit
    for _ in range(5):
        asyncio.run(collector.collect_once())
    assert len(stored.latest("rt-1", snapshots=10)) == 3


def test_collect_skips_empty_content(tmp_path: Path) -> None:
    collector = _collector(tmp_path, {"6199": {"content": ""}})
    import asyncio

    assert asyncio.run(collector.collect_once()) == 0
    assert collector.store.latest("rt-1") == []


def test_unreachable_runtime_records_error_not_crash(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    store = RuntimeLogStore(tmp_path / "hub.db")
    collector = RuntimeLogCollector(
        runtime_service=_Service([_Record()]),
        credential_vault=_Vault(),
        store=store,
        transport=httpx.MockTransport(handler),
    )
    import asyncio

    assert asyncio.run(collector.collect_once()) == 0
    assert collector.last_error is not None
    assert collector.last_error.startswith("rt-1")


def test_admin_endpoints_require_admin_and_serve_rows(
    tmp_path: Path,
) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        seeded = client.app.state.runtime_log_store
        seeded.insert_snapshot(
            runtime_id="rt-x",
            tenant_id="t-x",
            log_mtime=1.0,
            log_size=3,
            tail_lines=1,
            content="abc",
        )
        index = client.get("/api/hub/admin/runtimes/logs", headers=headers)
        assert index.status_code == 200, index.text
        assert index.json()["runtimes"][0]["runtime_id"] == "rt-x"

        one = client.get(
            "/api/hub/admin/runtimes/rt-x/logs",
            headers=headers,
        )
        assert one.status_code == 200
        body = one.json()
        assert body["snapshots"][0]["content"] == "abc"

        missing = client.get(
            "/api/hub/admin/runtimes/nope/logs",
            headers=headers,
        )
        assert missing.status_code == 404

        anonymous = client.get("/api/hub/admin/runtimes/logs")
        assert anonymous.status_code == 401
