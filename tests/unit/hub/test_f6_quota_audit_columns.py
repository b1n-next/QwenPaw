# -*- coding: utf-8 -*-
"""F6: quota decision lifted into queryable audit columns."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.operations import HubOperationsStore


def test_quota_columns_recorded_and_filterable(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    ops: HubOperationsStore = app.state.operations
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        # one quota-scoped event, one ordinary event
        ops.record(
            actor_user_id="u1",
            actor_username="alice",
            action="quota.exceeded",
            resource_type="api",
            resource_id="/api/chat/x",
            outcome="denied",
            detail={"method": "POST"},
            quota_dimension="daily_requests",
            quota_used=100,
            quota_limit=100,
        )
        ops.record(
            actor_user_id="u1",
            actor_username="alice",
            action="template.published",
            resource_type="template",
            resource_id="t1",
        )
        # payload exposes the columns
        body = client.get(
            "/api/hub/admin/audit",
            params={"action": "quota.exceeded"},
            headers=headers,
        ).json()
        assert body["total"] == 1
        event = body["items"][0]
        assert event["quota_dimension"] == "daily_requests"
        assert event["quota_used"] == 100
        assert event["quota_limit"] == 100
        # ordinary events carry NULLs
        plain = client.get(
            "/api/hub/admin/audit",
            params={"action": "template.published"},
            headers=headers,
        ).json()["items"][0]
        assert plain["quota_dimension"] is None
        # dedicated quota_dimension filter
        filtered = client.get(
            "/api/hub/admin/audit",
            params={"quota_dimension": "daily_requests"},
            headers=headers,
        ).json()
        assert filtered["total"] == 1
        assert filtered["items"][0]["action"] == "quota.exceeded"


def test_chain_still_verifies_with_quota_rows(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    ops: HubOperationsStore = app.state.operations
    for i in range(3):
        ops.record(
            actor_user_id="u",
            actor_username="u",
            action="quota.exceeded",
            resource_type="api",
            resource_id=f"/r{i}",
            outcome="denied",
            quota_dimension="daily_tokens",
            quota_used=1000 + i,
            quota_limit=1000,
        )
    verdict = ops.verify_chain()
    assert verdict["valid"] is True


def test_legacy_store_upgrades_idempotently(tmp_path: Path) -> None:
    # first init creates columns; re-init must not fail
    create_hub_app(root_dir=tmp_path, public_bind=False)
    app2 = create_hub_app(root_dir=tmp_path, public_bind=False)
    assert app2.state.operations.verify_chain()["valid"] is True
