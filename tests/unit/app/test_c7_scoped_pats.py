# -*- coding: utf-8 -*-
"""C7: fine-grained scoped PATs (issue → enforce → revoke)."""

from __future__ import annotations

import base64
import json

import pytest

from qwenpaw.app import auth as app_auth
from qwenpaw.app.routers import auth as auth_router


def _payload(token: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(token.split(".")[0]))


def test_normalize_scopes_valid_and_invalid() -> None:
    assert app_auth.normalize_scopes(
        ["agents:read", "FILES", "*"],
    ) == ["agents:read", "files", "*"]
    with pytest.raises(ValueError):
        app_auth.normalize_scopes(["nope"])
    with pytest.raises(ValueError):
        app_auth.normalize_scopes(["agents:rw"])


def test_scoped_token_carries_scp_session_token_does_not() -> None:
    session = app_auth.create_token("owner")
    assert "scp" not in _payload(session)
    scoped = app_auth.create_token(
        "owner",
        scopes=["agents:read"],
    )
    assert _payload(scoped)["scp"] == ["agents:read"]
    assert app_auth.token_scopes(scoped) == ["agents:read"]
    assert app_auth.token_scopes(session) is None


def test_request_allowed_by_scopes_matrix() -> None:
    scopes = ["agents:read", "files"]
    assert app_auth.request_allowed_by_scopes(
        scopes,
        "GET",
        "/api/agents/list",
    )
    assert not app_auth.request_allowed_by_scopes(
        scopes,
        "POST",
        "/api/agents/list",
    )
    assert app_auth.request_allowed_by_scopes(
        scopes,
        "DELETE",
        "/api/files/x",
    )
    assert not app_auth.request_allowed_by_scopes(
        scopes,
        "GET",
        "/api/chat/history",
    )
    assert app_auth.request_allowed_by_scopes(
        ["*"],
        "POST",
        "/api/anything",
    )


def test_pat_metadata_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_auth, "AUTH_FILE", tmp_path / "auth.json")

    class _StubRouter:  # isolate router functions under test
        pass

    assert not app_auth.list_personal_tokens()
    app_auth.add_personal_token(
        {"jti": "j1", "user": "owner", "scopes": ["files"]},
    )
    app_auth.add_personal_token(
        {"jti": "j2", "user": "owner", "scopes": ["chat"]},
    )
    assert len(app_auth.list_personal_tokens()) == 2
    assert app_auth.remove_personal_token("j1") is True
    assert app_auth.remove_personal_token("j1") is False
    assert len(app_auth.list_personal_tokens()) == 1


def test_router_issues_scoped_pat(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_auth, "AUTH_FILE", tmp_path / "auth.json")

    class _Req:
        headers = {"Authorization": "Bearer session-token"}

    class _Body:
        name = "ci-bot"
        scopes = ["agents:read"]
        expiry_seconds = None

    real_verify = auth_router.verify_token
    real_create = auth_router.create_token

    def fake_verify(token):
        return "owner" if token == "session-token" else None

    monkeypatch.setattr(auth_router, "verify_token", fake_verify)

    import asyncio

    result = asyncio.run(
        auth_router.create_personal_token(_Req(), _Body()),
    )
    assert result["scopes"] == ["agents:read"]
    assert result["token"].count(".") == 1
    assert _payload(result["token"])["scp"] == ["agents:read"]
    assert any(
        e.get("jti") == result["jti"] for e in app_auth.list_personal_tokens()
    )
    monkeypatch.setattr(auth_router, "verify_token", real_verify)
    monkeypatch.setattr(auth_router, "create_token", real_create)
