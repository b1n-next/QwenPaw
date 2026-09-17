# -*- coding: utf-8 -*-
"""EP-2-2: OIDC SSO — client, routes, JIT provisioning, group sync."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.oidc import OidcClient, OidcError, OidcSettings


# ----------------------------------------------------- client


def _fake_idp(claims: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "openid-configuration" in url:
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://idp.example/auth",
                    "token_endpoint": "https://idp.example/token",
                    "userinfo_endpoint": "https://idp.example/userinfo",
                },
            )
        if url == "https://idp.example/token":
            body = request.read().decode()
            assert "grant_type=authorization_code" in body
            assert "code=good-code" in body
            return httpx.Response(200, json={"access_token": "AT"})
        if url == "https://idp.example/userinfo":
            return httpx.Response(200, json=claims)
        raise AssertionError(url)

    return httpx.MockTransport(handler)


def _client(claims: dict) -> OidcClient:
    return OidcClient(
        OidcSettings(
            issuer="https://idp.example",
            client_id="cid",
            client_secret="sec",
        ),
        transport=_fake_idp(claims),
    )


def _state_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


def test_authorization_url_registers_state() -> None:
    client = _client({})
    url = client.authorization_url("https://hub/cb", "/console")
    assert url.startswith("https://idp.example/auth?")
    assert "client_id=cid" in url
    state = _state_of(url)
    assert client.consume_state(state) == "/console"
    assert client.consume_state(state) is None  # single use


def test_exchange_resolves_identity() -> None:
    import asyncio

    client = _client(
        {
            "preferred_username": "alice",
            "name": "Alice Chen",
            "groups": ["finance", "devs"],
        },
    )
    identity = asyncio.run(
        client.exchange_and_resolve("good-code", "https://hub/cb"),
    )
    assert identity.username == "alice"
    assert identity.display_name == "Alice Chen"
    assert identity.groups == ("finance", "devs")


def test_invalid_username_claim_rejected() -> None:
    import asyncio

    client = _client({"preferred_username": "bad:name"})
    with pytest.raises(OidcError) as excinfo:
        asyncio.run(
            client.exchange_and_resolve("good-code", "https://hub/cb"),
        )
    assert str(excinfo.value) == "oidc_username_invalid"


def test_groups_claim_accepts_single_string() -> None:
    client = _client({"preferred_username": "bob", "groups": "finance"})
    identity = (
        client._identity_from_claims(  # pylint: disable=protected-access
            {"preferred_username": "bob", "groups": "finance"},
        )
    )
    assert identity.groups == ("finance",)


def test_disabled_settings_are_off() -> None:
    assert not OidcSettings().enabled


# ----------------------------------------------------- routes


def _enable_oidc(client: TestClient, admin: str) -> None:
    settings = client.get(
        "/api/hub/admin/settings",
        headers={"Authorization": f"Bearer {admin}"},
    ).json()
    config = settings["config"]
    config["control_plane"]["oidc"] = {
        "issuer": "https://idp.example",
        "client_id": "cid",
        "client_secret": "sec",
    }
    response = client.put(
        "/api/hub/admin/settings",
        json={
            "revision": settings["revision"],
            "config": config,
        },
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert response.status_code == 200, response.text
    app = client.app
    app.state.oidc_client = _client(
        {
            "preferred_username": "alice",
            "name": "Alice Chen",
            "groups": ["finance"],
        },
    )


def _register_admin(client: TestClient) -> str:
    response = client.post(
        "/api/auth/register",
        json={"username": "owner", "password": "pw-123456"},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def test_login_endpoint_redirects_to_idp(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        admin = _register_admin(client)
        # before enable: 503
        disabled = client.get(
            "/api/hub/auth/oidc/login",
            follow_redirects=False,
        )
        assert disabled.status_code == 503
        _enable_oidc(client, admin)
        response = client.get(
            "/api/hub/auth/oidc/login?next=/console",
            follow_redirects=False,
        )
        assert response.status_code == 302
        target = response.headers["location"]
        assert target.startswith("https://idp.example/auth?")


def test_callback_jit_provisions_and_syncs_groups(
    tmp_path: Path,
) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        admin = _register_admin(client)
        _enable_oidc(client, admin)
        login = client.get(
            "/api/hub/auth/oidc/login?next=/console",
            follow_redirects=False,
        )
        state = _state_of(login.headers["location"])
        callback = client.get(
            "/api/hub/auth/oidc/callback",
            params={"code": "good-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 302, callback.text
        location = callback.headers["location"]
        assert location.startswith("/console#qwenpaw_token=")
        token = location.split("qwenpaw_token=")[1]
        me = client.get(
            "/api/hub/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me.status_code == 200
        assert me.json()["username"] == "alice"
        groups = client.app.state.group_store.group_names_for(
            me.json()["user_id"],
        )
        assert groups == ("finance",)
        listing = client.app.state.group_store.list_groups()
        assert listing[0]["source"] == "oidc"


def test_callback_rejects_bad_state(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        admin = _register_admin(client)
        _enable_oidc(client, admin)
        response = client.get(
            "/api/hub/auth/oidc/callback",
            params={"code": "good-code", "state": "forged"},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "OIDC_BAD_STATE"


def test_callback_refuses_disabled_account(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        admin = _register_admin(client)
        # pre-create alice and disable her
        created = client.post(
            "/api/hub/admin/users",
            json={"username": "alice", "password": "pw-123456"},
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert created.status_code in (200, 201)
        alice_id = client.app.state.auth_service.find_by_username(
            "alice",
        ).user_id
        patch = client.patch(
            f"/api/hub/admin/users/{alice_id}",
            json={"disabled": True},
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert patch.status_code in (200, 204), patch.text
        _enable_oidc(client, admin)
        login = client.get(
            "/api/hub/auth/oidc/login",
            follow_redirects=False,
        )
        state = _state_of(login.headers["location"])
        response = client.get(
            "/api/hub/auth/oidc/callback",
            params={"code": "good-code", "state": state},
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "OIDC_ACCOUNT_DISABLED"
