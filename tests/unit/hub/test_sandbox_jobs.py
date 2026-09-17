# -*- coding: utf-8 -*-
"""G7: two-tier execution — resident Pod + on-demand sandbox Jobs."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.provisioners.k8s.manifest import sandbox_job_manifest


class _Record:
    runtime_id = "rt-1"
    tenant_id = "t1"
    owner_user_id = "u1"


def test_sandbox_job_manifest_hardened() -> None:
    manifest = sandbox_job_manifest(
        _Record(),
        namespace="ns",
        image="qwenpaw:2.2",
        command=["python", "-c", "print(1)"],
        job_id="ab" * 8,
        ttl_seconds=1800,
        timeout_seconds=120,
    )
    assert manifest["apiVersion"] == "batch/v1"
    assert manifest["kind"] == "Job"
    assert manifest["metadata"]["name"].startswith("rt-1-sbx-")
    spec = manifest["spec"]
    assert spec["ttlSecondsAfterFinished"] == 1800
    assert spec["activeDeadlineSeconds"] == 120
    assert spec["backoffLimit"] == 0
    assert spec["restartPolicy"] == "Never"
    labels = manifest["metadata"]["labels"]
    assert labels["qwenpaw.ai/tier"] == "sandbox"
    container = spec["template"]["spec"]["containers"][0]
    security = container["securityContext"]
    assert security["runAsNonRoot"] is True
    assert security["allowPrivilegeEscalation"] is False
    assert security["readOnlyRootFilesystem"] is True
    assert security["capabilities"] == {"drop": ["ALL"]}
    # writable scratch without loosening the root filesystem
    mounts = container["volumeMounts"]
    assert mounts == [{"name": "tmp", "mountPath": "/tmp"}]


def _app_with_job_provisioner(tmp_path: Path, job_name: str):
    """create_hub_app with a local provisioner exposing job launch."""
    from qwenpaw.hub.service import RuntimeService
    from qwenpaw.hub.registry import RuntimeRegistry
    from qwenpaw.hub.provisioner import (
        RuntimeProvisioner,
        RuntimeProvisionerAvailability,
    )

    registry = RuntimeRegistry(tmp_path / "control.db")

    class _FakeProvisioner(RuntimeProvisioner):
        name = "local"
        security_level = "isolated-job"

        def __init__(self) -> None:
            self.launched: list[dict] = []

        def preflight(self, root_dir):  # type: ignore[no-untyped-def]
            del root_dir
            return RuntimeProvisionerAvailability(
                available=True,
                reason=None,
            )

        def start(self, record, credentials):  # type: ignore[no-untyped-def]
            del credentials
            return record

        def stop(self, record):  # type: ignore[no-untyped-def]
            return record

        def status(self, record):  # type: ignore[no-untyped-def]
            return record

        def close(self) -> None:
            return None

        def launch_sandbox_job(
            self,
            record,  # type: ignore[no-untyped-def]
            *,
            command: list[str],
            job_id: str,
            environment: dict | None = None,
            ttl_seconds: int = 3600,
            timeout_seconds: int = 600,
        ) -> str:
            del record, environment
            self.launched.append(
                {
                    "command": command,
                    "job_id": job_id,
                    "ttl": ttl_seconds,
                    "timeout": timeout_seconds,
                },
            )
            return job_name

    provisioner = _FakeProvisioner()
    service = RuntimeService(
        root_dir=tmp_path,
        registry=registry,
        provisioners={"local": provisioner},
        credential_provider=lambda record: {},
    )
    return create_hub_app(service)


def test_sandbox_job_endpoint_flow(tmp_path: Path) -> None:
    app = _app_with_job_provisioner(tmp_path, "job-abc")
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        created = client.post(
            "/api/hub/runtimes",
            json={"runtime_id": "rt-1", "metadata": {}},
            headers=headers,
        )
        assert created.status_code in (201, 409), created.text
        # validation first
        assert (
            client.post(
                "/api/hub/runtimes/rt-1/sandbox-jobs",
                json={"command": "not-a-list"},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/hub/runtimes/rt-1/sandbox-jobs",
                json={"command": ["ls"], "ttl_seconds": 5},
                headers=headers,
            ).status_code
            == 422
        )
        launched = client.post(
            "/api/hub/runtimes/rt-1/sandbox-jobs",
            json={
                "command": ["python", "-c", "print(1)"],
                "ttl_seconds": 1800,
                "timeout_seconds": 120,
            },
            headers=headers,
        )
        assert launched.status_code == 201, launched.text
        body = launched.json()
        assert body["job_name"] == "job-abc"
        assert body["ttl_seconds"] == 1800
        # unknown runtime -> 404
        assert (
            client.post(
                "/api/hub/runtimes/nope/sandbox-jobs",
                json={"command": ["ls"]},
                headers=headers,
            ).status_code
            == 404
        )
        # the dispatch is audited
        entries = client.get(
            "/api/hub/admin/audit",
            headers=headers,
        ).json()["items"]
        assert any(
            entry["action"] == "runtime.sandbox_job" for entry in entries
        )
