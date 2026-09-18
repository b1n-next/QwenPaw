# -*- coding: utf-8 -*-
"""E5 consumption side: the model gateway walks admin fallback chains."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from types import SimpleNamespace

import qwenpaw.hub.model_service.gateway as gateway_module
from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.model_service.gateway import ModelGateway


class _FakeAttempt:
    """Stands in for GatewayRequest: reserve maps model -> connection."""

    counter = {"closed": 0}
    request_index = {"n": 0}

    def __init__(self, budgets, catalog):
        self._budgets = budgets
        self._catalog = catalog
        self.request_id = f"req-{self.request_index['n']}"
        self.request_index["n"] += 1
        self.reserved_models: list[str] = []
        self.stack = contextlib.AsyncExitStack()

    async def reserve(self, identity, body, *, admin_test=False):
        model = str(body.get("model") or "")
        self.reserved_models.append(model)
        connection = {
            "base_url": f"https://{model}.example",
            "provider": "openai",
            "quota_scope": "shared",
        }
        return (
            identity,
            {
                "upstream_model": model,
                "output_limit_field": "max_tokens",
            },
            connection,
            {"max_tokens": 512},
        )

    async def dispatch(self) -> None:
        return None

    async def close(self, actual=None, error=None) -> None:
        self.counter["closed"] += 1


def _gateway(
    chain: dict[str, list[str]],
    *,
    failures: set[str],
) -> tuple[ModelGateway, list[str]]:
    """Build a gateway whose transport fails for listed models."""
    events: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.host.rsplit(".", 2)[0]
        if model in failures:
            return httpx.Response(500, text="boom")
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {"index": 0, "message": {"role": "a", "content": "ok"}},
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    # catalog only supplies the provider key inside _open; a plain
    # namespace keeps the unit surface on the failover walk itself
    gateway = ModelGateway(
        catalog=SimpleNamespace(
            store=None,
            key=lambda connection: "sk-test",
        ),
        budgets=None,
        transport=httpx.MockTransport(handler),
    )
    gateway.fallbacks_for = lambda model_id: chain.get(model_id, [])
    gateway.on_fallback = lambda original, used, rid: events.append(
        f"{original}->{used}",
    )
    return gateway, events


def _call(gateway: ModelGateway, model: str) -> Any:
    return asyncio.run(
        gateway.call(
            {"user_id": "u1"},
            {"model": model, "messages": [{"role": "u", "content": "hi"}]},
        ),
    )


def test_gateway_walks_chain_on_upstream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module, "GatewayRequest", _FakeAttempt)
    _FakeAttempt.counter["closed"] = 0
    gateway, events = _gateway(
        {"m1": ["m2", "m3"]},
        failures={"m1", "m2"},
    )
    response = _call(gateway, "m1")
    body = json.loads(response.body)
    assert body["model"] == "m3"
    assert response.headers["X-QwenPaw-Fallback"] == "m1->m3"
    assert events == ["m1->m3"]
    # every failed attempt was closed (budgets metered per hop)
    assert _FakeAttempt.counter["closed"] >= 2


def test_gateway_surfaces_502_when_chain_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    monkeypatch.setattr(gateway_module, "GatewayRequest", _FakeAttempt)
    gateway, _ = _gateway({"m1": ["m2"]}, failures={"m1", "m2"})
    with pytest.raises(HTTPException) as excinfo:
        _call(gateway, "m1")
    assert excinfo.value.status_code == 502


def test_gateway_dedupes_cycle_in_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module, "GatewayRequest", _FakeAttempt)
    _FakeAttempt.request_index["n"] = 0
    gateway, _ = _gateway(
        {"m1": ["m2", "m1", "m2"]},
        failures={"m1", "m2"},
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _call(gateway, "m1")
    # m2 appears twice in the chain but reserves exactly once — the
    # seen-set collapsed the cycle before trying (m1, m2 only)
    assert _FakeAttempt.request_index["n"] == 2


def test_control_app_wires_chain_consumption(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    gateway = app.state.model_gateway
    assert callable(gateway.fallbacks_for)
    assert gateway.fallbacks_for("m-none") == []

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        )
        token = login.json().get("token")
        assert token
        headers = {"Authorization": f"Bearer {token}"}
        put = client.put(
            "/api/hub/admin/models/m-primary/fallbacks",
            json={"fallbacks": ["m-backup"]},
            headers=headers,
        )
        assert put.status_code == 200, put.text
        # the gateway reads the same store the admin endpoint wrote
        assert gateway.fallbacks_for("m-primary") == ["m-backup"]
