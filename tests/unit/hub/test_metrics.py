# -*- coding: utf-8 -*-
"""EP-2-4: Prometheus metrics registry and /metrics endpoint tests."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.metrics import HubMetrics


def _register(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "pw-123456"},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


# ----------------------------------------------------- registry


def test_counter_renders_with_labels_sorted() -> None:
    metrics = HubMetrics()
    metrics.inc("m_total", role="user", decision="denied")
    metrics.inc("m_total", role="user", decision="denied")
    metrics.inc("m_total", role="admin", decision="allowed")
    out = metrics.render()
    assert 'm_total{decision="denied",role="user"} 2.0' in out
    assert 'm_total{decision="allowed",role="admin"} 1.0' in out
    assert "# TYPE m_total counter" in out


def test_gauge_replaces_same_label_set() -> None:
    metrics = HubMetrics()
    metrics.set_gauge("g", 1, tenant="t", state="running")
    metrics.set_gauge("g", 1, tenant="t", state="running")
    metrics.set_gauge("g", 0, tenant="t", state="stopped")
    out = metrics.render()
    assert out.count('state="running"') == 1
    assert 'g{state="stopped",tenant="t"} 0' in out
    assert "# TYPE g gauge" in out


def test_label_escaping() -> None:
    metrics = HubMetrics()
    metrics.inc("m_total", model='qwen"2.5\n')
    out = metrics.render()
    assert 'model="qwen\\"2.5\\n"' in out


def test_unlabeled_counter_renders_bare() -> None:
    metrics = HubMetrics()
    metrics.inc("plain_total")
    metrics.inc("plain_total")
    assert "plain_total 2.0" in metrics.render()


def test_snapshot_helpers() -> None:
    metrics = HubMetrics()
    metrics.inc("m_total", role="user")
    assert metrics.snapshot_counter("m_total", role="user") == 1.0
    assert metrics.snapshot_counter("m_total", role="admin") == 0.0
    metrics.set_gauge("g", 7)
    assert metrics.snapshot_gauge("g") == 7.0
    metrics.reset()
    assert metrics.snapshot_counter("m_total", role="user") == 0.0


def test_clear_gauge_family_drops_stale_states() -> None:
    metrics = HubMetrics()
    metrics.set_gauge("g", 1, tenant="a", state="running")
    metrics.set_gauge("g", 1, tenant="b", state="running")
    metrics.clear_gauge_family("g")
    assert "state=" not in metrics.render()


# ----------------------------------------------------- endpoint


def test_metrics_endpoint_renders_families(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = _register(client, "owner")
        response = client.get(
            "/api/hub/metrics",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        body = response.text
        assert (
            "# TYPE qwenpaw_usage_last_success_timestamp_seconds gauge" in body
        )


def test_acl_denial_increments_counter(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        owner = _register(client, "owner")  # first user: admin
        created = client.post(
            "/api/hub/admin/users",
            json={"username": "member", "password": "pw-123456"},
            headers={"Authorization": f"Bearer {owner}"},
        )
        assert created.status_code in (200, 201), created.text
        member = client.app.state.auth_service.authenticate(
            "member",
            "pw-123456",
        )[1]
        # /api/config is a proxied path: members hit the proxy ACL
        # gate (default-deny) BEFORE any runtime lookup, so the
        # counter fires without a runtime harness
        denied = client.get(
            "/api/config",
            headers={"Authorization": f"Bearer {member}"},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "ACL_DENIED"
        body = client.get(
            "/api/hub/metrics",
            headers={"Authorization": f"Bearer {member}"},
        ).text
        assert (
            'qwenpaw_hub_requests_total{decision="denied",role="user"} 1.0'
            in body
        )
        assert "# TYPE qwenpaw_hub_requests_total counter" in body


def test_metrics_requires_auth(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        assert client.get("/api/hub/metrics").status_code == 401
