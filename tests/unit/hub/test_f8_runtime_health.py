# -*- coding: utf-8 -*-
"""F8: admin runtime health detail (config-level, K8s plane)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app


class _StubK8sProvisioner:
    """Stands in for the K8s provisioner on the app's registry."""

    def close(self):  # registry shutdown calls this
        """No-op."""

    def pod_health(self, record):  # pylint: disable=unused-argument
        return {
            "phase": "Running",
            "restart_count": 1,
            "started_at": "2026-09-18T10:00:00Z",
            "resources": {
                "requests": {"cpu": "500m", "memory": "1Gi"},
                "limits": {"cpu": "2", "memory": "4Gi"},
            },
            "node": "kind-worker",
        }


def test_k8s_runtime_reports_pod_health(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        record = type(
            "R",
            (),
            {
                "runtime_id": "rt-f8",
                "tenant_id": "t",
                "owner_user_id": "u",
                "provisioner": "k8s",
                "state": type(
                    "S",
                    (),
                    {"value": "running"},
                )(),
                "host": "h",
                "port": 1,
            },
        )()
        service = client.app.state.runtime_service
        original = service.list
        service.list = lambda: [record]  # type: ignore[method-assign]
        try:
            service.provisioners["k8s"] = _StubK8sProvisioner()
            body = client.get(
                "/api/hub/admin/runtimes/rt-f8/health",
                headers=headers,
            ).json()
        finally:
            service.list = original  # type: ignore[method-assign]
        assert body["supported"] is True
        assert body["pod"]["phase"] == "Running"
        assert body["pod"]["restart_count"] == 1
        assert body["pod"]["resources"]["limits"]["memory"] == "4Gi"
        assert body["pod"]["node"] == "kind-worker"


def test_non_k8s_runtime_reports_unsupported(
    tmp_path: Path,
) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        record = type(
            "R",
            (),
            {
                "runtime_id": "rt-local",
                "tenant_id": "t",
                "owner_user_id": "u",
                "provisioner": "local",
                "state": type("S", (), {"value": "running"})(),
                "host": "h",
                "port": 1,
            },
        )()
        service = client.app.state.runtime_service
        original = service.list
        service.list = lambda: [record]  # type: ignore[method-assign]
        try:
            body = client.get(
                "/api/hub/admin/runtimes/rt-local/health",
                headers=headers,
            ).json()
        finally:
            service.list = original  # type: ignore[method-assign]
        assert body["supported"] is False
        assert "pod" not in body


def test_unknown_runtime_404(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        missing = client.get(
            "/api/hub/admin/runtimes/nope/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert missing.status_code == 404


def test_health_requires_admin(tmp_path: Path) -> None:
    app = create_hub_app(root_dir=tmp_path, public_bind=False)
    with TestClient(app) as client:
        anonymous = client.get("/api/hub/admin/runtimes/x/health")
        assert anonymous.status_code == 401
