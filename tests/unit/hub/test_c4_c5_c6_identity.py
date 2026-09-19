# -*- coding: utf-8 -*-
"""C4 LDAP fallback + C5 SCIM deprovision + C6 group hierarchy."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.ldap_auth import (
    LdapAuthenticator,
    LdapSettings,
    _escape_filter,
)


def _admin(client: TestClient) -> dict:
    token = client.post(
        "/api/auth/register",
        json={"username": "owner", "password": "pw-123456"},
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


# ── C6 ──────────────────────────────────────────────────────────────


def test_group_nesting_and_inherited_policy(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        admin = _admin(client)
        store = app.state.group_store
        tenant = store.create_group("acme")
        dept = store.create_group(
            "engineering",
            parent_group_id=tenant,
        )
        team = store.create_group(
            "platform",
            parent_group_id=dept,
        )
        # depth cap
        sub = store.create_group(
            "infra",
            parent_group_id=team,
        )
        try:
            store.create_group("too-deep", parent_group_id=sub)
            raise AssertionError("depth cap missed")
        except ValueError:
            pass
        # members in the leaf; department quota sees them via subtree
        client.post(
            "/api/hub/admin/users",
            headers=admin,
            json={
                "username": "leafmember",
                "password": "pw-123456",
                "role": "user",
            },
        )
        uid = next(
            u["user_id"]
            for u in client.get(
                "/api/hub/admin/users",
                headers=admin,
            ).json()["items"]
            if u["username"] == "leafmember"
        )
        store.add_member(team, uid)
        assert store.member_ids("platform") == [uid]
        assert store.descendant_member_ids("engineering") == [uid]
        assert store.descendant_member_ids("acme") == [uid]
        # department allow-policy applies to the leaf member
        store.create_policy(
            subject_kind="group",
            subject_value="engineering",
            resource="menu:reports",
            effect="allow",
        )
        live = store.policies_for(
            user_id=uid,
            groups=["platform"],
            role="user",
        )
        assert any(p.resource == "menu:reports" for p in live)
        # groups listing carries the parent linkage
        listed = {g["name"]: g for g in store.list_groups()}
        assert listed["engineering"]["parent_id"] == tenant
        assert listed["acme"]["parent_id"] is None


# ── C5 ──────────────────────────────────────────────────────────────


def _arm_scim(root: Path) -> str:
    token = "scim-secret-token"
    path = root / "scim_token"
    path.write_text(token, encoding="utf-8")
    os.chmod(path, 0o600)
    return token


def test_scim_deprovision_erases_user(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        admin = _admin(client)
        token = _arm_scim(tmp_path)
        app.state.scim_token = token
        assert (
            client.get(
                "/api/hub/admin/scim/status",
                headers=admin,
            ).json()["enabled"]
            is True
        )
        created = client.post(
            "/api/hub/admin/users",
            headers=admin,
            json={
                "username": "leaver",
                "password": "pw-123456",
                "role": "user",
            },
        )
        uid = created.json()["user_id"]
        result = client.post(
            "/api/hub/scim/v2/Users/leaver",
            headers={"Authorization": f"Bearer {token}"},
            json={"active": False},
        )
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["active"] is False
        assert body["anonymized"] == 1
        names = [
            u["username"]
            for u in client.get(
                "/api/hub/admin/users",
                headers=admin,
            ).json()["items"]
        ]
        assert "leaver" not in names
        audit = client.get(
            "/api/hub/admin/audit",
            params={"action": "scim.user.deprovisioned"},
            headers=admin,
        ).json()["items"]
        assert any(a["resource_id"] == uid for a in audit)


def test_scim_auth_gates(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        # not configured
        off = client.post(
            "/api/hub/scim/v2/Users/x",
            json={"active": False},
        )
        assert off.status_code == 503
        token = _arm_scim(tmp_path)
        app.state.scim_token = token
        # wrong token
        bad = client.post(
            "/api/hub/scim/v2/Users/x",
            headers={"Authorization": "Bearer wrong"},
            json={"active": False},
        )
        assert bad.status_code == 401
        # unknown user
        missing = client.post(
            "/api/hub/scim/v2/Users/ghost",
            headers={"Authorization": f"Bearer {token}"},
            json={"active": False},
        )
        assert missing.status_code == 404
        # DELETE alias works on a real user
        admin = _admin(client)
        client.post(
            "/api/hub/admin/users",
            headers=admin,
            json={
                "username": "alias",
                "password": "pw-123456",
                "role": "user",
            },
        )
        deleted = client.delete(
            "/api/hub/scim/v2/Users/alias",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert deleted.status_code == 200
        assert json.loads(deleted.text)["active"] is False


# ── C4 ──────────────────────────────────────────────────────────────


def test_ldap_settings_load(tmp_path: Path) -> None:
    config = tmp_path / "ldap.json"
    config.write_text(
        json.dumps(
            {
                "url": "ldaps://ldap.corp:636",
                "base_dn": "dc=corp",
                "bind_dn": "cn=srv,dc=corp",
                "bind_password": "s3cret",
            },
        ),
        encoding="utf-8",
    )
    settings = LdapSettings.from_file(config)
    assert settings is not None
    assert settings.uses_tls is True
    assert (tmp_path / "missing.json").exists() is False
    assert LdapSettings.from_file(tmp_path / "missing.json") is None


def test_ldap_filter_escapes_injection() -> None:
    assert _escape_filter("a*b(c)\\d") == "a\\2ab\\28c\\29\\5cd"
    assert _escape_filter("plain") == "plain"


def test_ldap_login_fallback_provisions(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    (tmp_path / "ldap.json").write_text(
        json.dumps(
            {"url": "ldap://ldap.corp", "base_dn": "dc=corp"},
        ),
        encoding="utf-8",
    )
    app.state.ldap_authenticator = LdapAuthenticator(
        LdapSettings(url="ldap://ldap.corp", base_dn="dc=corp"),
    )

    # stub the directory verdict (unit scope: hub-side behaviour)
    def _stub_verify(u: str, p: str) -> bool:
        return u == "diruser" and p == "dir-pass"

    app.state.ldap_authenticator.verify = _stub_verify
    with TestClient(app) as client:
        admin = _admin(client)
        # wrong password rejected
        denied = client.post(
            "/api/auth/login",
            json={"username": "diruser", "password": "nope-123456"},
        )
        assert denied.status_code == 401
        # directory vouches -> auto-provisioned member session
        ok = client.post(
            "/api/auth/login",
            json={"username": "diruser", "password": "dir-pass"},
        )
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["user"]["role"] == "user"
        assert body["username"] == "diruser"
        # second login finds the existing row (no duplicate)
        again = client.post(
            "/api/auth/login",
            json={"username": "diruser", "password": "dir-pass"},
        )
        assert again.status_code == 200
        users = client.get(
            "/api/hub/admin/users",
            headers=admin,
        ).json()["items"]
        assert sum(1 for u in users if u["username"] == "diruser") == 1


def test_ldap_disabled_keeps_local_only(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    assert app.state.ldap_authenticator is None
    with TestClient(app) as client:
        _admin(client)
        denied = client.post(
            "/api/auth/login",
            json={"username": "owner", "password": "wrong-123456"},
        )
        assert denied.status_code == 401
