# -*- coding: utf-8 -*-
"""A4 follow-up: the OIDC client secret lands in the encrypted vault."""

from __future__ import annotations

import importlib
from pathlib import Path

from fastapi.testclient import TestClient

import qwenpaw.hub.control_app as control_app
from qwenpaw.hub.control_app import _build_oidc_client, create_hub_app


class _VaultStub:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.reads = 0

    def get(self, *, tenant_id, scope, name):
        self.reads += 1
        assert tenant_id == "__qwenpaw_hub_system__"
        assert scope == "control"
        assert name == "QWENPAW_HUB_OIDC_CLIENT_SECRET"
        return self.value


class _OidcStub:
    def __init__(self, *, enabled=True, secret="") -> None:
        from types import SimpleNamespace

        self.config = SimpleNamespace(
            control_plane=SimpleNamespace(
                oidc=SimpleNamespace(
                    enabled=enabled,
                    issuer="https://idp.example",
                    client_id="cid",
                    client_secret=secret,
                    username_claim="preferred_username",
                    groups_claim="groups",
                    display_name_claim="name",
                ),
            ),
        )

    def __getattr__(self, item):
        return getattr(self.config, item)


def test_client_secret_prefers_explicit_then_vault() -> None:
    # explicit hub.yaml value wins (compat with file-only setups)
    vault = _VaultStub("vault-value")
    client = _build_oidc_client(_OidcStub(secret="file-value"), vault=vault)
    assert client._settings.client_secret == "file-value"
    assert vault.reads == 0
    # empty file value falls through to the vault entry
    client = _build_oidc_client(_OidcStub(secret=""), vault=vault)
    assert client._settings.client_secret == "vault-value"
    assert vault.reads == 1
    # disabled oidc never touches the vault
    client = _build_oidc_client(_OidcStub(enabled=False), vault=vault)
    assert client is None
    assert vault.reads == 1


def test_env_secret_imported_to_vault_and_cleared(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("QWENPAW_HUB_OIDC_CLIENT_SECRET", "env-secret")
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        import os

        assert (
            os.environ.get("QWENPAW_HUB_OIDC_CLIENT_SECRET") is None
        ), "import must clear the plaintext env value"
        stored = client.app.state.credential_vault.get(
            tenant_id="__qwenpaw_hub_system__",
            scope="control",
            name="QWENPAW_HUB_OIDC_CLIENT_SECRET",
        )
        assert stored == "env-secret"
        # a second boot with no env still resolves via the vault
        rebuilt = _build_oidc_client(
            _OidcStub(secret=""),
            vault=client.app.state.credential_vault,
        )
        assert rebuilt._settings.client_secret == "env-secret"


def test_no_env_leaves_vault_untouched(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("QWENPAW_HUB_OIDC_CLIENT_SECRET", raising=False)
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        stored = client.app.state.credential_vault.get(
            tenant_id="__qwenpaw_hub_system__",
            scope="control",
            name="QWENPAW_HUB_OIDC_CLIENT_SECRET",
        )
        assert stored is None
