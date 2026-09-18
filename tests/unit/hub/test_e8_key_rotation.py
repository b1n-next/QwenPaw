# -*- coding: utf-8 -*-
"""E8: key rotation — provider keys (preflight) + runtime tokens (grace)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import qwenpaw.hub.control_app as control_app_module
from qwenpaw.hub.control_app import create_hub_app


def _patch_probe_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> None:
    """Force every AsyncClient the endpoint builds onto the mock."""

    real_client = httpx.AsyncClient

    class _Patched(real_client):  # type: ignore[misc,valid-type]
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(
        control_app_module.httpx,
        "AsyncClient",
        _Patched,
    )


@pytest.fixture(name="admin_client")
def _admin_client(tmp_path: Path):
    app = create_hub_app(
        root_dir=tmp_path,
        public_bind=False,
        model_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": []}),
        ),
    )
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        yield client


def _seed_provider(client: TestClient) -> str:
    create = client.post(
        "/api/hub/admin/models/providers",
        json={
            "provider_id": "prov-e8",
            "name": "E8 Provider",
            "base_url": "https://upstream.example",
            "api_key": "old-key",
            "models": ["m1"],
        },
    )
    assert create.status_code == 201, create.text
    return "prov-e8"


def test_provider_key_rotation_preflight_passes(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_id = _seed_provider(admin_client)

    calls: list[str] = []

    async def probe(  # pylint: disable=unused-argument
        request: httpx.Request,
    ) -> httpx.Response:
        calls.append(request.headers.get("Authorization", ""))
        return httpx.Response(200, json={"data": []})

    _patch_probe_transport(monkeypatch, probe)
    rotated = admin_client.post(
        f"/api/hub/admin/models/providers/{provider_id}/rotate-key",
        json={"api_key": "new-key"},
    )
    assert rotated.status_code == 200, rotated.text
    assert "Bearer new-key" in calls
    # key landed: catalog probe with the new key echoes api_key_set
    # (secrets never echo); verify via the store the app was built on
    from qwenpaw.hub.model_catalog import ModelCatalogStore

    store = ModelCatalogStore(
        admin_client.app.state.runtime_service.registry.database_path,
        admin_client.app.state.runtime_service.root_dir
        / "secrets"
        / ".model_catalog_key",
    )
    assert store.get_api_key(provider_id) == "new-key"
    audit = admin_client.get(
        "/api/hub/admin/audit",
        params={"action": "model_catalog.key_rotated"},
    )
    assert audit.status_code == 200
    assert any(
        e.get("action") == "model_catalog.key_rotated"
        for e in audit.json().get("items", [])
    )


def test_provider_key_rotation_rejected_keeps_old_key(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_id = _seed_provider(admin_client)

    async def probe(  # pylint: disable=unused-argument
        request: httpx.Request,
    ) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    _patch_probe_transport(monkeypatch, probe)
    rejected = admin_client.post(
        f"/api/hub/admin/models/providers/{provider_id}/rotate-key",
        json={"api_key": "bad-key"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "ROTATE_PREFLIGHT_FAILED"
    from qwenpaw.hub.model_catalog import ModelCatalogStore

    store = ModelCatalogStore(
        admin_client.app.state.runtime_service.registry.database_path,
        admin_client.app.state.runtime_service.root_dir
        / "secrets"
        / ".model_catalog_key",
    )
    assert store.get_api_key(provider_id) == "old-key"


def test_provider_key_rotation_requires_key(admin_client: TestClient) -> None:
    provider_id = _seed_provider(admin_client)
    missing = admin_client.post(
        f"/api/hub/admin/models/providers/{provider_id}/rotate-key",
        json={},
    )
    assert missing.status_code == 422


def test_runtime_token_rotation_stores_previous_and_new(
    admin_client: TestClient,
) -> None:
    app = admin_client.app
    vault = app.state.credential_vault
    record = type(
        "R",
        (),
        {
            "runtime_id": "rt-e8",
            "tenant_id": "t-e8",
            "owner_user_id": "u-e8",
            "state": "running",
            "host": "127.0.0.1",
            "port": 1,
        },
    )()

    original = type(app.state.runtime_service).list

    app.state.runtime_service.list = lambda: [  # type: ignore[method-assign]
        record,
    ]
    try:
        # seed an existing token as if provisioned earlier
        scope = "runtime-control:t-e8:rt-e8"
        vault.put(
            tenant_id="__qwenpaw_hub_system__",
            scope=scope,
            name="QWENPAW_RUNTIME_INTERNAL_TOKEN",
            value="old-token",
            trusted=True,
        )
        rotated = admin_client.post(
            "/api/hub/admin/runtimes/rt-e8/rotate-token",
        )
        assert rotated.status_code == 200, rotated.text
        body = rotated.json()
        assert body["applies_on"] == "next runtime (re)start"
        assert vault.get(
            tenant_id="__qwenpaw_hub_system__",
            scope=scope,
            name="QWENPAW_RUNTIME_INTERNAL_TOKEN",
        ) not in (None, "old-token")
        assert (
            vault.get(
                tenant_id="__qwenpaw_hub_system__",
                scope=scope,
                name="QWENPAW_RUNTIME_INTERNAL_TOKEN_PREVIOUS",
            )
            == "old-token"
        )
    finally:
        app.state.runtime_service.list = (  # type: ignore[method-assign]
            original
        )


def test_runtime_token_rotation_unknown_runtime(
    admin_client: TestClient,
) -> None:
    missing = admin_client.post(
        "/api/hub/admin/runtimes/nope/rotate-token",
    )
    assert missing.status_code == 404
