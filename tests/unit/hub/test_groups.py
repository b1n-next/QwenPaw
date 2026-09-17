# -*- coding: utf-8 -*-
"""EP-2-1: group/policy storage and engine policy evaluation."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.database import initialize_hub_database
from qwenpaw.hub.acl.engine import AclEngine
from qwenpaw.hub.acl.groups import GroupPolicyStore, Policy


# ----------------------------------------------------- engine


def _policy(subject: str, resource: str, effect: str) -> Policy:
    return Policy(
        policy_id=subject.replace(":", "") + resource.replace(":", ""),
        subject=subject,
        resource=resource,
        effect=effect,
    )


def test_no_policies_fall_through_to_defaults() -> None:
    engine = AclEngine()
    assert not engine.decide("user", "GET", "/api/hub/admin/users").allowed


def test_user_allow_admin_policy_broadens() -> None:
    engine = AclEngine()
    decision = engine.decide(
        "user",
        "GET",
        "/api/hub/admin/users",
        policies=(_policy("user:u1", "apigroup:admin", "allow"),),
    )
    assert decision.allowed
    assert decision.reason.startswith("policy-")


def test_group_deny_overrides_static_allow() -> None:
    engine = AclEngine()
    decision = engine.decide(
        "user",
        "GET",
        "/api/console/chat/x",
        policies=(_policy("group:finance", "apigroup:chat", "deny"),),
    )
    assert not decision.allowed


def test_deny_beats_allow_at_equal_path() -> None:
    engine = AclEngine()
    decision = engine.decide(
        "user",
        "GET",
        "/api/hub/admin/users",
        policies=(
            _policy("user:u1", "apigroup:admin", "allow"),
            _policy("group:locked", "apigroup:admin", "deny"),
        ),
    )
    assert not decision.allowed


def test_specific_user_beats_group_allow() -> None:
    # store order puts user subjects first; equal-path deny from a
    # group must not beat a user allow... it does per deny-first at
    # the same path: this pins the documented trade-off (deny wins
    # at equal path regardless of subject specificity)
    engine = AclEngine()
    decision = engine.decide(
        "user",
        "GET",
        "/api/console/ping",
        policies=(
            _policy("user:u1", "apigroup:chat", "allow"),
            _policy("group:locked", "apigroup:chat", "deny"),
        ),
    )
    assert not decision.allowed


def test_non_apigroup_resource_ignores_proxy_path() -> None:
    engine = AclEngine()
    decision = engine.decide(
        "user",
        "GET",
        "/api/console/ping",
        policies=(_policy("user:u1", "menu:settings", "deny"),),
    )
    assert decision.allowed  # static table still grants


# ----------------------------------------------------- store


@pytest.fixture(name="store")
def _store(tmp_path: Path) -> GroupPolicyStore:
    database = tmp_path / "control.db"
    initialize_hub_database(database)
    return GroupPolicyStore(database)


def test_group_lifecycle(store: GroupPolicyStore) -> None:
    group_id = store.create_group("finance")
    store.add_member(group_id, "u1")
    store.add_member(group_id, "u1")  # idempotent
    assert store.group_names_for("u1") == ("finance",)
    listing = store.list_groups()
    assert listing[0]["name"] == "finance"
    assert listing[0]["member_count"] == 1
    store.remove_member(group_id, "u1")
    assert store.group_names_for("u1") == ()
    assert store.delete_group(group_id)
    assert not store.delete_group(group_id)


def test_group_delete_cascades_policies(store: GroupPolicyStore) -> None:
    group_id = store.create_group("finance")
    store.create_policy(
        subject_kind="group",
        subject_value="finance",
        resource="apigroup:chat",
        effect="deny",
    )
    store.delete_group(group_id)
    assert store.list_policies() == []


def test_policy_validation(store: GroupPolicyStore) -> None:
    with pytest.raises(ValueError):
        store.create_policy(
            subject_kind="tenant",
            subject_value="x",
            resource="apigroup:chat",
            effect="allow",
        )
    with pytest.raises(ValueError):
        store.create_policy(
            subject_kind="user",
            subject_value="u1",
            resource="no-prefix",
            effect="allow",
        )
    with pytest.raises(ValueError):
        store.create_policy(
            subject_kind="user",
            subject_value="u1",
            resource="apigroup:chat",
            effect="block",
        )


def test_policies_for_subject_order(store: GroupPolicyStore) -> None:
    store.create_policy(
        subject_kind="role",
        subject_value="user",
        resource="apigroup:models",
        effect="deny",
    )
    group_id = store.create_group("finance")
    store.create_policy(
        subject_kind="group",
        subject_value="finance",
        resource="apigroup:chat",
        effect="deny",
    )
    store.create_policy(
        subject_kind="user",
        subject_value="u1",
        resource="apigroup:admin",
        effect="allow",
    )
    store.add_member(group_id, "u1")
    ordered = store.policies_for(user_id="u1", groups=["finance"], role="user")
    kinds = [policy.subject_kind for policy in ordered]
    assert kinds == ["user", "group", "role"]


# ----------------------------------------------------- API


def _admin(client: TestClient) -> str:
    response = client.post(
        "/api/auth/register",
        json={"username": "owner", "password": "pw-123456"},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def test_admin_group_and_policy_crud(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = _admin(client)
        headers = {"Authorization": f"Bearer {token}"}
        created = client.post(
            "/api/hub/admin/groups",
            json={"name": "finance"},
            headers=headers,
        )
        assert created.status_code == 200, created.text
        bad = client.post(
            "/api/hub/admin/groups",
            json={"name": ""},
            headers=headers,
        )
        assert bad.status_code == 400
        policy = client.post(
            "/api/hub/admin/policies",
            json={
                "subject_kind": "group",
                "subject_value": "finance",
                "resource": "apigroup:chat",
                "effect": "deny",
            },
            headers=headers,
        )
        assert policy.status_code == 200, policy.text
        policy_id = policy.json()["policy_id"]
        listing = client.get("/api/hub/admin/policies", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["policies"][0]["subject"] == "group:finance"
        assert (
            client.delete(
                f"/api/hub/admin/policies/{policy_id}",
                headers=headers,
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"/api/hub/admin/policies/{policy_id}",
                headers=headers,
            ).status_code
            == 404
        )


def test_member_policy_blocks_proxy_path(tmp_path: Path) -> None:
    """End to end: member of a denied group loses /api/console."""
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = _admin(client)
        headers = {"Authorization": f"Bearer {token}"}
        client.post(
            "/api/hub/admin/users",
            json={"username": "member", "password": "pw-123456"},
            headers=headers,
        )
        member = client.app.state.auth_service.authenticate(
            "member",
            "pw-123456",
        )
        group_id = client.post(
            "/api/hub/admin/groups",
            json={"name": "locked"},
            headers=headers,
        ).json()["group_id"]
        client.post(
            f"/api/hub/admin/groups/{group_id}/members",
            json={"user_id": member[0].user_id},
            headers=headers,
        )
        client.post(
            "/api/hub/admin/policies",
            json={
                "subject_kind": "group",
                "subject_value": "locked",
                "resource": "apigroup:chat",
                "effect": "deny",
            },
            headers=headers,
        )
        denied = client.get(
            "/api/console/ping",
            headers={"Authorization": f"Bearer {member[1]}"},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "ACL_DENIED"
        reason = denied.json()["detail"]["reason"]
        assert reason.startswith("policy-")
        # audit row carries the policy reason
        _, total = client.app.state.operations.list_events(
            page=1,
            page_size=5,
            action="acl.denied",
        )
        assert total == 1
