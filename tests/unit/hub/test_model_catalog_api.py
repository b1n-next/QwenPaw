# -*- coding: utf-8 -*-
"""Integration tests for the admin model catalog API (EP-1-1).

Exercises the wired control_app: admin CRUD over
/api/hub/admin/models/providers with masked secrets, member denial via
ACL, and audit rows.
"""

from pathlib import Path

import httpx
from httpx import MockTransport

from tests.unit.hub.test_control_app import (
    _ProxyStream,
    _client,
    _create_user,
    _headers,
    _register,
)


def _ok_transport() -> MockTransport:
    return MockTransport(
        lambda request: httpx.Response(200, stream=_ProxyStream()),
    )


def _provider_body() -> dict:
    return {
        "provider_id": "corp-gpt",
        "name": "Corp GPT Gateway",
        "base_url": "https://llm.corp.internal/v1",
        "api_key": "sk-live-secret",
        "models": ["gpt-4o", "gpt-4o-mini"],
        "default_model": "gpt-4o",
        "enabled": True,
    }


def test_admin_upsert_masks_api_key(tmp_path: Path) -> None:
    with _client(tmp_path, _ok_transport()) as client:
        admin_token = _register(client, "owner")

        response = client.post(
            "/api/hub/admin/models/providers",
            headers=_headers(admin_token),
            json=_provider_body(),
        )

        assert response.status_code == 201
        body = response.json()
        assert body["provider_id"] == "corp-gpt"
        assert body["models"] == ["gpt-4o", "gpt-4o-mini"]
        assert body["api_key_set"] is True
        assert "api_key" not in body
        assert "sk-live-secret" not in response.text


def test_admin_list_patch_delete_roundtrip(tmp_path: Path) -> None:
    with _client(tmp_path, _ok_transport()) as client:
        admin_token = _register(client, "owner")
        client.post(
            "/api/hub/admin/models/providers",
            headers=_headers(admin_token),
            json=_provider_body(),
        )

        listing = client.get(
            "/api/hub/admin/models/providers",
            headers=_headers(admin_token),
        )
        assert listing.status_code == 200
        assert listing.json()["total"] == 1

        patched = client.patch(
            "/api/hub/admin/models/providers/corp-gpt",
            headers=_headers(admin_token),
            json={"enabled": False, "name": "Corp (off)"},
        )
        assert patched.status_code == 200
        assert patched.json()["enabled"] is False
        assert patched.json()["name"] == "Corp (off)"
        # key survived the api_key-less patch
        assert patched.json()["api_key_set"] is True

        deleted = client.delete(
            "/api/hub/admin/models/providers/corp-gpt",
            headers=_headers(admin_token),
        )
        assert deleted.status_code == 200
        assert (
            client.get(
                "/api/hub/admin/models/providers",
                headers=_headers(admin_token),
            ).json()["total"]
            == 0
        )

        missing = client.delete(
            "/api/hub/admin/models/providers/corp-gpt",
            headers=_headers(admin_token),
        )
        assert missing.status_code == 404


def test_member_denied_on_catalog_routes(tmp_path: Path) -> None:
    with _client(tmp_path, _ok_transport()) as client:
        _register(client, "owner")
        _, member_token = _create_user(client, "member")

        for method, path in (
            ("GET", "/api/hub/admin/models/providers"),
            ("POST", "/api/hub/admin/models/providers"),
            ("DELETE", "/api/hub/admin/models/providers/corp-gpt"),
        ):
            response = client.request(
                method,
                path,
                headers=_headers(member_token),
                json=_provider_body(),
            )
            assert response.status_code == 403, (method, path)


def test_invalid_provider_id_returns_400(tmp_path: Path) -> None:
    with _client(tmp_path, _ok_transport()) as client:
        admin_token = _register(client, "owner")
        body = _provider_body()
        body["provider_id"] = "Not A Slug"
        response = client.post(
            "/api/hub/admin/models/providers",
            headers=_headers(admin_token),
            json=body,
        )
        assert response.status_code == 400


def test_catalog_actions_audited(tmp_path: Path) -> None:
    with _client(tmp_path, _ok_transport()) as client:
        admin_token = _register(client, "owner")
        client.post(
            "/api/hub/admin/models/providers",
            headers=_headers(admin_token),
            json=_provider_body(),
        )
        events = client.get(
            "/api/hub/admin/audit?action=model_catalog.upsert",
            headers=_headers(admin_token),
        ).json()
        assert events["total"] >= 1
