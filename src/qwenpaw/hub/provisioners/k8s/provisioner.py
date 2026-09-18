# -*- coding: utf-8 -*-
"""Kubernetes runtime provisioner (EP-1-6/1-7).

One Pod + one PVC + one ClusterIP Service per tenant runtime, aligned
with the hub's existing per-tenant orchestration model (06 §1). The
hub proxies to ``<runtime>.<namespace>.svc:<port>``; because that is
not a loopback address, deployments MUST configure
``QWENPAW_HUB_RUNTIME_HOST_SUFFIXES`` (fail-closed in preflight).
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from qwenpaw.hub.models import RuntimeRecord, RuntimeState
from qwenpaw.hub.provisioner import (
    RuntimeProvisioner,
    RuntimeProvisionerAvailability,
)
from .client import K8sClient, K8sClientError, K8sNotFoundError
from .manifest import (
    pod_manifest,
    pod_name,
    pvc_manifest,
    pvc_name,
    sandbox_job_manifest,
    service_manifest,
    service_name,
)

DEFAULT_NAMESPACE = "qwenpaw-runtimes"
DEFAULT_IMAGE = "qwenpaw:latest"
# Matches the official runtime image entrypoint (QWENPAW_PORT).
DEFAULT_PORT = 8088
RUNTIME_HOST_SUFFIXES_ENV = "QWENPAW_HUB_RUNTIME_HOST_SUFFIXES"

_CONFIG_KEYS = {
    "namespace",
    "image",
    "port",
    "image_pull_policy",
    "storage_class",
    "pvc_size",
    "cpu_request",
    "cpu_limit",
    "memory_request",
    "memory_limit",
    "node_selector",
    "tolerations",
    "service_account",
    "startup_timeout_seconds",
    "cluster_domain",
}


def _run(coro: Any) -> Any:
    """Drive an async call from provisioner sync methods.

    The hub invokes provisioner methods on worker threads (never on
    the event loop), so a private loop per call is safe.
    """
    return asyncio.run(coro)


class K8sRuntimeProvisioner(RuntimeProvisioner):
    """Manage per-tenant QwenPaw runtimes as Kubernetes Pods."""

    name = "k8s"
    security_level = "pod-namespace"

    def __init__(
        self,
        client_factory: Callable[[], K8sClient] | None = None,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        image: str = DEFAULT_IMAGE,
        port: int = DEFAULT_PORT,
        cluster_domain: str = "cluster.local",
    ) -> None:
        # httpx.AsyncClient binds to the loop that created it; every
        # provisioner call drives its own private loop via _run(), so
        # each call gets a fresh one-shot client from this factory.
        self._client_factory = client_factory
        self._namespace = namespace
        self._image = image
        self._port = int(port)
        self._cluster_domain = cluster_domain
        self._image_pull_policy = "IfNotPresent"
        self._storage_class: str | None = None
        self._pvc_size = "10Gi"
        self._resources: dict[str, Any] = {}
        self._node_selector: dict[str, str] = {}
        self._tolerations: list[dict[str, Any]] = []
        self._service_account: str | None = None
        self._startup_timeout_seconds = 300

    # -- configuration ---------------------------------------------------

    def validate_config(self, value: object) -> dict[str, object]:
        if value in ({}, None):
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("k8s provisioner config must be an object")
        unknown = set(value) - _CONFIG_KEYS
        if unknown:
            raise ValueError(
                f"unknown k8s provisioner keys: {sorted(unknown)}",
            )
        return dict(value)

    def configure(self, config: Mapping[str, object]) -> None:
        # pylint: disable=too-many-branches
        normalized = self.validate_config(config)
        if "namespace" in normalized:
            self._namespace = str(normalized["namespace"])
        if "image" in normalized:
            self._image = str(normalized["image"])
        if "port" in normalized:
            self._port = int(str(normalized["port"]))
        if "cluster_domain" in normalized:
            self._cluster_domain = str(normalized["cluster_domain"])
        if "image_pull_policy" in normalized:
            self._image_pull_policy = str(
                normalized["image_pull_policy"],
            )
        if "storage_class" in normalized:
            self._storage_class = str(normalized["storage_class"]) or None
        if "pvc_size" in normalized:
            self._pvc_size = str(normalized["pvc_size"])
        requests: dict[str, str] = {}
        limits: dict[str, str] = {}
        if "cpu_request" in normalized:
            requests["cpu"] = str(normalized["cpu_request"])
        if "memory_request" in normalized:
            requests["memory"] = str(normalized["memory_request"])
        if "cpu_limit" in normalized:
            limits["cpu"] = str(normalized["cpu_limit"])
        if "memory_limit" in normalized:
            limits["memory"] = str(normalized["memory_limit"])
        if requests or limits:
            self._resources = {
                **({"requests": requests} if requests else {}),
                **({"limits": limits} if limits else {}),
            }
        if "node_selector" in normalized:
            raw_selector = normalized["node_selector"] or {}
            self._node_selector = {
                str(key): str(value) for key, value in raw_selector.items()
            }
        if "tolerations" in normalized:
            self._tolerations = [
                dict(entry) for entry in (normalized["tolerations"] or [])
            ]
        if "service_account" in normalized:
            self._service_account = str(normalized["service_account"]) or None
        if "startup_timeout_seconds" in normalized:
            raw_timeout = normalized["startup_timeout_seconds"]
            self._startup_timeout_seconds = int(str(raw_timeout))

    # -- helpers -----------------------------------------------------------

    def _make_client(self) -> K8sClient:
        if self._client_factory is None:
            self._client_factory = K8sClient.from_environment
        return self._client_factory()

    def launch_sandbox_job(
        self,
        record: RuntimeRecord,
        *,
        command: list[str],
        job_id: str,
        environment: dict[str, str] | None = None,
        ttl_seconds: int = 3600,
        timeout_seconds: int = 600,
    ) -> str:
        """Create one on-demand sandbox Job (G7 tier-2 execution).

        Returns the job name. The resident agent Pod stays put —
        untrusted/heavy work runs here and self-cleans via TTL.
        """
        manifest = sandbox_job_manifest(
            record,
            namespace=str(self._namespace),
            image=str(self._image),
            command=command,
            job_id=job_id,
            ttl_seconds=ttl_seconds,
            timeout_seconds=timeout_seconds,
            environment=environment or {},
        )
        client = self._make_client()
        job = _run(
            client.create_at(
                f"/apis/batch/v1/namespaces/{self._namespace}/jobs",
                manifest,
            ),
        )
        return str(job["metadata"]["name"])

    def runtime_host(self, record: RuntimeRecord) -> str:
        """Cluster-local Service DNS name for *record*."""
        return (
            f"{service_name(record)}.{self._namespace}"
            f".svc.{self._cluster_domain}"
        )

    # -- RuntimeProvisioner -------------------------------------------------

    def preflight(
        self,
        root_dir: Path,  # pylint: disable=unused-argument
    ) -> RuntimeProvisionerAvailability:
        suffixes = [
            part.strip().lstrip(".")
            for part in os.environ.get(
                RUNTIME_HOST_SUFFIXES_ENV,
                "",
            ).split(",")
            if part.strip()
        ]
        host = self.runtime_host(_probe_record())
        if not any(host.endswith(f".{suffix}") for suffix in suffixes):
            return RuntimeProvisionerAvailability(
                available=False,
                reason=(
                    f"runtime Service DNS names ({host}) are not "
                    f"loopback; set {RUNTIME_HOST_SUFFIXES_ENV} (e.g. "
                    f"'svc,svc.{self._cluster_domain}') to allow the "
                    f"hub proxy to reach them"
                ),
            )

        async def _probe() -> None:
            client = self._make_client()
            try:
                await client.get_namespace(self._namespace)
            finally:
                await client.close()

        try:
            _run(_probe())
        except K8sClientError as exc:
            return RuntimeProvisionerAvailability(
                available=False,
                reason=f"kubernetes API unusable: {exc}",
            )
        return RuntimeProvisionerAvailability(available=True, reason=None)

    def start(
        self,
        record: RuntimeRecord,
        credentials: Mapping[str, str],
    ) -> RuntimeRecord:
        async def _ensure_pvc(client: K8sClient) -> None:
            try:
                await client.get(
                    self._namespace,
                    "persistentvolumeclaims",
                    pvc_name(record),
                )
            except K8sNotFoundError:
                await client.create(
                    self._namespace,
                    "persistentvolumeclaims",
                    pvc_manifest(
                        record,
                        namespace=self._namespace,
                        storage_class=self._storage_class,
                        size=self._pvc_size,
                    ),
                )

        async def _ensure_service(client: K8sClient) -> None:
            try:
                await client.get(
                    self._namespace,
                    "services",
                    service_name(record),
                )
            except K8sNotFoundError:
                await client.create(
                    self._namespace,
                    "services",
                    service_manifest(
                        record,
                        namespace=self._namespace,
                        port=self._port,
                    ),
                )

        async def _replace_pod(client: K8sClient) -> None:
            try:
                await client.delete(
                    self._namespace,
                    "pods",
                    pod_name(record),
                )
            except K8sNotFoundError:
                pass
            await client.create(
                self._namespace,
                "pods",
                pod_manifest(
                    record,
                    namespace=self._namespace,
                    image=self._image,
                    port=self._port,
                    environment=dict(credentials),
                    resources=self._resources or None,
                    node_selector=self._node_selector or None,
                    tolerations=self._tolerations or None,
                    image_pull_policy=self._image_pull_policy,
                    service_account=self._service_account,
                    startup_timeout_seconds=self._startup_timeout_seconds,
                ),
            )

        async def _wait_ready(client: K8sClient) -> bool:
            deadline = time.monotonic() + self._startup_timeout_seconds
            while time.monotonic() < deadline:
                try:
                    pod = await client.get(
                        self._namespace,
                        "pods",
                        pod_name(record),
                    )
                except K8sNotFoundError:
                    return False
                conditions = pod.get("status", {}).get("conditions") or []
                if any(
                    condition.get("type") == "Ready"
                    and condition.get("status") == "True"
                    for condition in conditions
                ):
                    return True
                await asyncio.sleep(0.5)
            return False

        async def _start_all() -> bool:
            client = self._make_client()
            try:
                await _ensure_pvc(client)
                await _ensure_service(client)
                await _replace_pod(client)
                return await _wait_ready(client)
            finally:
                await client.close()

        try:
            ready = _run(_start_all())
        except K8sClientError:
            return replace(record, state=RuntimeState.FAILED)
        if not ready:
            return replace(record, state=RuntimeState.FAILED)
        return replace(
            record,
            host=self.runtime_host(record),
            port=self._port,
            state=RuntimeState.RUNNING,
        )

    def stop(self, record: RuntimeRecord) -> RuntimeRecord:
        async def _stop() -> None:
            client = self._make_client()
            try:
                try:
                    await client.delete(
                        self._namespace,
                        "pods",
                        pod_name(record),
                    )
                except K8sNotFoundError:
                    return
                # Wait for the pod to actually disappear so a
                # subsequent start does not race the deletion.
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    try:
                        await client.get(
                            self._namespace,
                            "pods",
                            pod_name(record),
                        )
                    except K8sNotFoundError:
                        return
                    await asyncio.sleep(0.5)
            finally:
                await client.close()

        try:
            _run(_stop())
        except K8sClientError:
            return replace(record, state=RuntimeState.FAILED)
        return replace(record, state=RuntimeState.STOPPED)

    def pod_health(self, record: RuntimeRecord) -> dict[str, Any] | None:
        """Config-level pod health for the admin panel (F8).

        Reads the live Pod object: phase, restart count, start time,
        and the container's requests/limits as scheduled. Live usage
        (actual CPU/memory consumption) intentionally stays on the
        Prometheus plane (EP-2-4) — this needs no metrics-server.
        """

        async def _read() -> dict[str, Any] | None:
            client = self._make_client()
            try:
                try:
                    pod = await client.get(
                        self._namespace,
                        "pods",
                        pod_name(record),
                    )
                except K8sNotFoundError:
                    return None
                container_status = _runtime_container(
                    pod.get("status", {}).get("containerStatuses", []),
                )
                container_spec = _runtime_container(
                    pod.get("spec", {}).get("containers", []),
                )
                return {
                    "phase": pod.get("status", {}).get("phase"),
                    "restart_count": container_status.get(
                        "restartCount",
                    ),
                    "started_at": (
                        container_status.get("state", {})
                        .get("running", {})
                        .get("startedAt")
                    ),
                    "resources": container_spec.get("resources") or {},
                    "node": pod.get("spec", {}).get("nodeName"),
                }
            finally:
                await client.close()

        try:
            return _run(_read())
        except K8sClientError:
            return None

    def status(self, record: RuntimeRecord) -> RuntimeRecord:
        async def _phase() -> str | None:
            client = self._make_client()
            try:
                try:
                    pod = await client.get(
                        self._namespace,
                        "pods",
                        pod_name(record),
                    )
                except K8sNotFoundError:
                    return None
                return pod.get("status", {}).get("phase")
            finally:
                await client.close()

        try:
            phase = _run(_phase())
        except K8sClientError:
            return replace(record, state=RuntimeState.FAILED)
        return replace(
            record,
            state=_PHASE_TO_STATE.get(phase, RuntimeState.STARTING),
        )

    def close(self) -> None:
        # Clients are one-shot per call; nothing persistent to release.
        self._client_factory = None


def _runtime_container(items: Any) -> dict[str, Any]:
    """First container named "runtime" from a Pod section ({} if none)."""
    for item in items:
        if isinstance(item, dict) and item.get("name") == "runtime":
            return item
    return {}


_PHASE_TO_STATE: Mapping[str | None, RuntimeState] = {
    None: RuntimeState.STOPPED,
    "Pending": RuntimeState.STARTING,
    "Running": RuntimeState.RUNNING,
    "Succeeded": RuntimeState.STOPPED,
    "Failed": RuntimeState.FAILED,
    "Unknown": RuntimeState.STARTING,
}


def _probe_record() -> RuntimeRecord:
    return RuntimeRecord(
        runtime_id="probe",
        tenant_id="probe",
        owner_user_id="probe",
        provisioner="k8s",
        host="",
        port=0,
        state=RuntimeState.CREATED,
        working_dir=Path("."),
        secret_dir=Path("."),
        backup_dir=Path("."),
        log_file=Path("."),
    )
