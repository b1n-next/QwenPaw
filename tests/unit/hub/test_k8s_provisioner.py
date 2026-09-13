# -*- coding: utf-8 -*-
"""Unit tests for the Kubernetes runtime provisioner (EP-1-6/1-7)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from httpx import MockTransport

from qwenpaw.hub.models import RuntimeRecord, RuntimeState
from qwenpaw.hub.provisioners.k8s import (
    K8S_RUNTIME_CAPS,
    K8sClient,
    K8sClientError,
    K8sNotFoundError,
    K8sRuntimeProvisioner,
)
from qwenpaw.hub.provisioners.k8s.manifest import (
    pod_manifest,
    pvc_manifest,
    service_manifest,
)


def _record() -> RuntimeRecord:
    return RuntimeRecord(
        runtime_id="personal-user1",
        tenant_id="personal-user1",
        owner_user_id="user1",
        provisioner="k8s",
        host="",
        port=0,
        state=RuntimeState.CREATED,
        working_dir=Path("/tmp/w"),
        secret_dir=Path("/tmp/s"),
        backup_dir=Path("/tmp/b"),
        log_file=Path("/tmp/w.log"),
    )


def _credentials() -> dict[str, str]:
    return {
        "QWENPAW_RUNTIME_INTERNAL_TOKEN": "tok-1",
        "QWENPAW_MODEL_BOOTSTRAP_JSON": json.dumps(
            [{"id": "corp", "base_url": "http://x", "models": ["m"]}],
        ),
    }


class TestManifests:
    def test_pvc_manifest_shape(self):
        m = pvc_manifest(
            _record(),
            namespace="qwenpaw-runtimes",
            storage_class="fast",
            size="20Gi",
        )
        assert m["kind"] == "PersistentVolumeClaim"
        assert m["metadata"]["namespace"] == "qwenpaw-runtimes"
        assert m["spec"]["accessModes"] == ["ReadWriteOnce"]
        assert m["spec"]["storageClassName"] == "fast"
        assert m["spec"]["resources"]["requests"]["storage"] == "20Gi"

    def test_service_manifest_selects_by_runtime_label(self):
        m = service_manifest(
            _record(),
            namespace="qwenpaw-runtimes",
            port=8420,
        )
        assert m["spec"]["type"] == "ClusterIP"
        assert m["spec"]["selector"] == {
            "qwenpaw.io/runtime-id": "personal-user1",
        }
        assert m["spec"]["ports"][0]["port"] == 8420

    def test_pod_manifest_carries_env_and_mount(self):
        m = pod_manifest(
            _record(),
            namespace="qwenpaw-runtimes",
            image="qwenpaw:2.2",
            port=8420,
            environment=_credentials(),
            resources={"limits": {"cpu": "2", "memory": "4Gi"}},
        )
        container = m["spec"]["containers"][0]
        env = {e["name"]: e["value"] for e in container["env"]}
        assert env["QWENPAW_RUNTIME_INTERNAL_TOKEN"] == "tok-1"
        assert "corp" in env["QWENPAW_MODEL_BOOTSTRAP_JSON"]
        assert container["resources"]["limits"]["cpu"] == "2"
        assert container["volumeMounts"][0]["mountPath"] == "/app/working"
        assert (
            m["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]
            == "personal-user1-working"
        )

    def test_node_selector_and_tolerations(self):
        m = pod_manifest(
            _record(),
            namespace="ns",
            image="i",
            port=1,
            environment={},
            node_selector={"gpu": "true"},
            tolerations=[{"key": "gpu", "operator": "Exists"}],
        )
        assert m["spec"]["nodeSelector"] == {"gpu": "true"}
        assert m["spec"]["tolerations"] == [
            {"key": "gpu", "operator": "Exists"},
        ]

    def test_caps_declared(self):
        assert K8S_RUNTIME_CAPS.isolation == "pod-ns"
        assert K8S_RUNTIME_CAPS.network == "cluster-local"
        assert K8S_RUNTIME_CAPS.filesystem == "tenant-pvc"
        assert K8S_RUNTIME_CAPS.gpu is False


class _Cluster:
    """In-memory fake Kubernetes API over MockTransport."""

    def __init__(self):
        self.objects: dict[tuple[str, str, str], dict] = {}
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        self.requests.append((method, path))
        parts = path.strip("/").split("/")
        if path.startswith("/api/v1/namespaces/qwenpaw-runtimes/"):
            plural = parts[4]
            if method == "POST":
                body = json.loads(request.content or b"{}")
                self.objects[(plural, "", body["metadata"]["name"])] = body
                return httpx.Response(201, json=body)
            name = parts[5]
            key = (plural, "", name)
            if method == "GET":
                if key not in self.objects:
                    return httpx.Response(404, json={"reason": "NotFound"})
                return httpx.Response(200, json=self.objects[key])
            if method == "DELETE":
                self.objects.pop(key, None)
                return httpx.Response(200, json={})
        if path.startswith("/api/v1/namespaces/") and len(parts) == 4:
            return httpx.Response(200, json={"kind": "Namespace"})
        return httpx.Response(404, json={"reason": "NotFound"})

    def mark_ready(self, pod_name: str) -> None:
        pod = self.objects[("pods", "", pod_name)]
        pod.setdefault("status", {})["conditions"] = [
            {"type": "Ready", "status": "True"},
        ]
        pod["status"]["phase"] = "Running"


def _client(cluster: _Cluster) -> K8sClient:
    return K8sClient(
        base_url="http://k8s.test",
        token="t",
        transport=MockTransport(cluster.handler),
    )


class TestProvisionerFlow:
    def _provisioner(self, cluster: _Cluster) -> K8sRuntimeProvisioner:
        provisioner = K8sRuntimeProvisioner(_client(cluster))
        provisioner.configure(
            {
                "namespace": "qwenpaw-runtimes",
                "image": "qwenpaw:2.2",
                "startup_timeout_seconds": 2,
            },
        )
        return provisioner

    def test_start_creates_pvc_service_pod_and_backfills_host(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv(
            "QWENPAW_HUB_RUNTIME_HOST_SUFFIXES",
            "svc.cluster.local",
        )
        cluster = _Cluster()
        # Wrap the fake so the pod flips Ready the moment it is created.
        original_handler = cluster.handler

        def handler(request):
            response = original_handler(request)
            if request.method == "POST" and "/pods" in request.url.path:
                cluster.mark_ready("personal-user1")
            return response

        cluster.handler = handler  # type: ignore[method-assign]
        provisioner = K8sRuntimeProvisioner(_client(cluster))
        provisioner.configure(
            {
                "namespace": "qwenpaw-runtimes",
                "image": "qwenpaw:2.2",
                "startup_timeout_seconds": 2,
            },
        )
        availability = provisioner.preflight(Path("."))
        assert availability.available, availability.reason

        record = provisioner.start(_record(), _credentials())
        assert record.state == RuntimeState.RUNNING
        assert record.host == (
            "personal-user1.qwenpaw-runtimes.svc.cluster.local"
        )
        assert record.port == 8088
        created = {key[0] for key in cluster.objects}
        assert {"persistentvolumeclaims", "services", "pods"} <= created

    def test_stop_deletes_pod_keeps_pvc(self, monkeypatch):
        monkeypatch.setenv(
            "QWENPAW_HUB_RUNTIME_HOST_SUFFIXES",
            "svc",
        )
        cluster = _Cluster()
        provisioner = self._provisioner(cluster)
        cluster.objects[("pods", "", "personal-user1")] = {"spec": {}}
        cluster.objects[
            ("persistentvolumeclaims", "", "personal-user1-working")
        ] = {"spec": {}}

        record = provisioner.stop(_record())

        assert record.state == RuntimeState.STOPPED
        # PVC preserved
        assert (
            "persistentvolumeclaims",
            "",
            "personal-user1-working",
        ) in cluster.objects
        # pod gone
        assert ("pods", "", "personal-user1") not in cluster.objects

    def test_status_maps_phases(self, monkeypatch):
        monkeypatch.setenv("QWENPAW_HUB_RUNTIME_HOST_SUFFIXES", "svc")
        cluster = _Cluster()
        provisioner = self._provisioner(cluster)
        record = _record()

        assert provisioner.status(record).state == RuntimeState.STOPPED

        cluster.objects[("pods", "", "personal-user1")] = {
            "status": {"phase": "Pending"},
        }
        assert provisioner.status(record).state == RuntimeState.STARTING

        cluster.objects[("pods", "", "personal-user1")] = {
            "status": {"phase": "Running"},
        }
        assert provisioner.status(record).state == RuntimeState.RUNNING

        cluster.objects[("pods", "", "personal-user1")] = {
            "status": {"phase": "Failed"},
        }
        assert provisioner.status(record).state == RuntimeState.FAILED

    def test_preflight_requires_suffix_env(self, monkeypatch):
        monkeypatch.delenv(
            "QWENPAW_HUB_RUNTIME_HOST_SUFFIXES",
            raising=False,
        )
        cluster = _Cluster()
        provisioner = self._provisioner(cluster)
        availability = provisioner.preflight(Path("."))
        assert not availability.available
        assert "QWENPAW_HUB_RUNTIME_HOST_SUFFIXES" in (
            availability.reason or ""
        )

    def test_preflight_unreachable_api(self, monkeypatch):
        monkeypatch.setenv(
            "QWENPAW_HUB_RUNTIME_HOST_SUFFIXES",
            "svc.cluster.local",
        )

        def dead(request):
            raise httpx.ConnectError("no cluster")

        provisioner = K8sRuntimeProvisioner(
            K8sClient(
                base_url="http://k8s.test",
                transport=MockTransport(dead),
            ),
        )
        availability = provisioner.preflight(Path("."))
        assert not availability.available

    def test_configure_rejects_unknown_keys(self):
        provisioner = K8sRuntimeProvisioner()
        with pytest.raises(ValueError):
            provisioner.configure({"nope": 1})


class TestClient:
    def test_not_found_raises_typed(self):
        cluster = _Cluster()

        async def case() -> None:
            with pytest.raises(K8sNotFoundError):
                await _client(cluster).get(
                    "qwenpaw-runtimes",
                    "pods",
                    "missing",
                )

        import asyncio

        asyncio.run(case())

    def test_transport_error_wrapped(self):
        def dead(request):
            raise httpx.ConnectError("down")

        client = K8sClient(
            base_url="http://k8s.test",
            transport=MockTransport(dead),
        )

        async def case() -> None:
            with pytest.raises(K8sClientError):
                await client.get("ns", "pods", "x")

        import asyncio

        asyncio.run(case())

    def test_record_replace_immutability(self):
        base = _record()
        mutated = replace(base, state=RuntimeState.FAILED)
        assert base.state == RuntimeState.CREATED
        assert mutated.state == RuntimeState.FAILED
