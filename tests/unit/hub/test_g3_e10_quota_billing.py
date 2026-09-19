# -*- coding: utf-8 -*-
"""G3 group-aggregate quotas + E10 agent-level billing export."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.quota.engine import QuotaEngine
from qwenpaw.hub.quota.engine import UsageSnapshot


def _engine(tmp_path: Path, config: dict) -> QuotaEngine:
    path = tmp_path / "quota.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return QuotaEngine(path)


# ── G3 engine-level ─────────────────────────────────────────────────


def test_group_quota_hard_and_soft(tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        {
            "groups": {
                "gold": {"daily_requests": 10},
            },
            "soft_threshold": 0.8,
        },
    )
    hard = engine.check_group(
        "gold",
        UsageSnapshot(tokens=0, requests=10),
    )
    assert hard.allowed is False
    assert hard.dimension == "daily_requests"
    soft = engine.check_group(
        "gold",
        UsageSnapshot(tokens=0, requests=8),
    )
    assert soft.allowed is True and soft.soft_hit is True
    free = engine.check_group(
        "gold",
        UsageSnapshot(tokens=0, requests=3),
    )
    assert free.allowed is True and not free.soft_hit


def test_group_without_config_is_unlimited(tmp_path: Path) -> None:
    engine = _engine(tmp_path, {"groups": {}})
    decision = engine.check_group(
        "anyone",
        UsageSnapshot(tokens=10**9, requests=10**9),
    )
    assert decision.allowed is True and not decision.soft_hit


def test_group_snapshot_cached(tmp_path: Path) -> None:
    engine = _engine(tmp_path, {})
    calls = []

    def fetch():
        calls.append(1)
        return UsageSnapshot(tokens=5, requests=5)

    first = engine.group_snapshot("g", fetch)
    second = engine.group_snapshot("g", fetch)
    assert first is second
    assert len(calls) == 1


# ── E10 export + G3 endpoint wiring (app level) ─────────────────────


def _seed(app, client=None) -> None:  # pylint: disable=unused-argument
    store = app.state.usage_store
    store.upsert_rows(
        "personal-u1",
        [
            {
                "date": "2026-09-19",
                "provider_id": "prov",
                "model": "m1",
                "agent_id": "agent-alpha",
                "prompt_tokens": 1000,
                "completion_tokens": 2000,
                "call_count": 3,
            },
        ],
    )
    app.state.model_extensions.put(
        resource_type="model",
        resource_id="m1",
        namespace="governance",
        key="pricing",
        value={
            "input_per_mtok": 1.0,
            "output_per_mtok": 2.0,
            "currency": "CNY",
        },
    )


def test_costs_include_by_agent(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        _seed(app, client)
        body = client.get(
            "/api/hub/admin/usage/costs",
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        agents = {row["agent_id"]: row for row in body.get("by_agent", [])}
        assert agents["agent-alpha"]["requests"] == 3
        assert agents["agent-alpha"]["prompt_tokens"] == 1000


def test_costs_export_csv_exact_pricing(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        _seed(app, client)
        response = client.get(
            "/api/hub/admin/usage/costs/export",
            headers=headers,
        )
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        assert "attachment" in response.headers["content-disposition"]
        rows = list(
            csv.DictReader(io.StringIO(response.text)),
        )
        assert len(rows) == 1
        row = rows[0]
        # 1000/1M*1.0 + 2000/1M*2.0 = 0.005
        assert float(row["cost"]) == 0.005
        assert row["currency"] == "CNY"
        assert row["agent_id"] == "agent-alpha"
        assert row["model"] == "m1"


def test_group_quota_endpoint_lists_groups(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        groups = app.state.group_store
        groups.create_group("gold")
        body = client.get(
            "/api/hub/admin/quota/groups",
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        names = {row["group"] for row in body["groups"]}
        assert "gold" in names
        gold = next(r for r in body["groups"] if r["group"] == "gold")
        assert gold["dimensions"][0]["limit"] is None  # unconfigured


def test_member_ids_roundtrip(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    store = app.state.group_store
    group_id = store.create_group("team-x")
    store.add_member(group_id, "user-abc")
    assert store.member_ids("team-x") == ["user-abc"]
    assert store.member_ids("missing") == []
