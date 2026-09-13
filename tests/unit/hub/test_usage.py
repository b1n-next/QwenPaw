# -*- coding: utf-8 -*-
"""Unit tests for hub usage accounting (EP-1-4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx
from httpx import MockTransport

from qwenpaw.hub.usage import UsageCollector, UsageStore

from tests.unit.hub.test_control_app import (
    _client,
    _create_user,
    _headers,
    _register,
)


def _row(
    date="2026-09-13",
    provider="corp-gpt",
    model="demo-model",
    agent=None,
    prompt=100,
    completion=50,
    calls=2,
):
    return {
        "date": date,
        "provider_id": provider,
        "model": model,
        "agent_id": agent,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "call_count": calls,
    }


class TestUsageStore:
    def test_upsert_and_summary(self, tmp_path: Path):
        store = UsageStore(tmp_path / "usage.db")
        store.upsert_rows(
            "personal-u1",
            [
                _row(model="m1", prompt=100, completion=40, calls=1),
                _row(model="m2", prompt=10, completion=5, calls=2),
            ],
        )
        store.upsert_rows(
            "personal-u2",
            [_row(model="m1", prompt=7, calls=1)],
        )
        summary = store.summary()
        assert summary["total"]["prompt_tokens"] == 117
        assert summary["total"]["call_count"] == 4
        assert set(summary["by_user"]) == {"personal-u1", "personal-u2"}
        assert summary["by_user"]["personal-u1"]["prompt_tokens"] == 110
        models = {entry["model"] for entry in summary["by_model"]}
        assert models == {"m1", "m2"}

    def test_repoll_is_idempotent(self, tmp_path: Path):
        store = UsageStore(tmp_path / "usage.db")
        store.upsert_rows("t1", [_row(prompt=100)])
        # same counters again -> no double counting
        store.upsert_rows("t1", [_row(prompt=100)])
        assert store.summary()["total"]["prompt_tokens"] == 100

    def test_growing_counters_take_latest(self, tmp_path: Path):
        store = UsageStore(tmp_path / "usage.db")
        store.upsert_rows("t1", [_row(prompt=100)])
        store.upsert_rows("t1", [_row(prompt=150)])
        assert store.summary()["total"]["prompt_tokens"] == 150

    def test_date_and_tenant_filters(self, tmp_path: Path):
        store = UsageStore(tmp_path / "usage.db")
        store.upsert_rows(
            "t1",
            [
                _row(date="2026-09-01", prompt=10),
                _row(date="2026-09-10", prompt=20),
            ],
        )
        store.upsert_rows("t2", [_row(date="2026-09-10", prompt=5)])
        window = store.summary(
            start_date="2026-09-05",
            end_date="2026-09-30",
        )
        assert window["total"]["prompt_tokens"] == 25
        only_t2 = store.summary(tenant_id="t2")
        assert only_t2["total"]["prompt_tokens"] == 5


@dataclass
class _Record:
    tenant_id: str = "personal-u1"
    runtime_id: str = "r1"
    state: str = "running"
    host: str = "127.0.0.1"
    port: int = 8901
    extras: dict = field(default_factory=dict)


class _FakeService:
    def __init__(self, records):
        self._records = records

    def list(self, *_args, **_kwargs):
        return self._records


class _FakeVault:
    def get_runtime_secret(self, *, name, **_kwargs):
        assert name == "QWENPAW_RUNTIME_INTERNAL_TOKEN"
        return "tok"


def _details_transport(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/token-usage/details"
        assert request.headers["X-QwenPaw-Runtime-Token"] == "tok"
        return httpx.Response(200, json=payload)

    return MockTransport(handler)


class TestUsageCollector:
    async def test_collect_once_upserts_rows(self, tmp_path: Path):
        store = UsageStore(tmp_path / "usage.db")
        collector = UsageCollector(
            runtime_service=_FakeService([_Record()]),
            credential_vault=_FakeVault(),
            store=store,
            transport=_details_transport([_row(prompt=42)]),
        )
        collected = await collector.collect_once()
        assert collected == 1
        assert store.summary()["total"]["prompt_tokens"] == 42
        assert collector.last_error is None

    async def test_runtime_error_is_swallowed(self, tmp_path: Path):
        store = UsageStore(tmp_path / "usage.db")

        def boom(request):
            raise httpx.ConnectError("down")

        collector = UsageCollector(
            runtime_service=_FakeService([_Record()]),
            credential_vault=_FakeVault(),
            store=store,
            transport=MockTransport(boom),
        )
        assert await collector.collect_once() == 0
        assert collector.last_error is not None
        assert store.summary()["total"]["prompt_tokens"] == 0

    async def test_non_running_runtimes_skipped(self, tmp_path: Path):
        store = UsageStore(tmp_path / "usage.db")
        collector = UsageCollector(
            runtime_service=_FakeService(
                [_Record(state="stopped"), _Record(runtime_id="r2")],
            ),
            credential_vault=_FakeVault(),
            store=store,
            transport=_details_transport([_row()]),
        )
        assert await collector.collect_once() == 1


def test_usage_endpoints_admin_only(tmp_path: Path):
    with _client(tmp_path) as client:
        admin_token = _register(client, "owner")
        _, member_token = _create_user(client, "member")

        response = client.get(
            "/api/hub/admin/usage/summary",
            headers=_headers(member_token),
        )
        assert response.status_code == 403

        response = client.get(
            "/api/hub/admin/usage/summary",
            headers=_headers(admin_token),
        )
        assert response.status_code == 200
        assert response.json()["total"]["prompt_tokens"] == 0

        forced = client.post(
            "/api/hub/admin/usage/collect",
            headers=_headers(admin_token),
        )
        assert forced.status_code == 200
        assert "collected_rows" in forced.json()
