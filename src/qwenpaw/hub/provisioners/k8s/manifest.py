# -*- coding: utf-8 -*-
"""Pod / PVC / Service manifest builders for the K8s provisioner.

Pure dict builders — no templating engine, no helm dependency. The
output is plain Kubernetes API objects, easy to assert in tests.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_DNS_LABEL_RE = re.compile(r"[^a-z0-9-]+")

APP_NAME = "qwenpaw-runtime"
LABEL_TENANT = "qwenpaw.io/tenant"
LABEL_RUNTIME = "qwenpaw.io/runtime-id"
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"


def dns_name(value: str) -> str:
    """Sanitize to a DNS-1123 label (lowercase alnum + dashes)."""
    cleaned = _DNS_LABEL_RE.sub("-", value.lower()).strip("-")
    if not cleaned:
        raise ValueError(f"cannot derive a DNS name from {value!r}")
    return cleaned[:63]


def runtime_labels(record: Any) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": APP_NAME,
        "app.kubernetes.io/instance": dns_name(record.runtime_id),
        LABEL_TENANT: dns_name(record.tenant_id),
        LABEL_RUNTIME: dns_name(record.runtime_id),
        LABEL_MANAGED_BY: "qwenpaw-hub",
    }


def pod_name(record: Any) -> str:
    return dns_name(record.runtime_id)


def service_name(record: Any) -> str:
    return dns_name(record.runtime_id)


def pvc_name(record: Any) -> str:
    return f"{dns_name(record.runtime_id)}-working"


def pvc_manifest(
    record: Any,
    *,
    namespace: str,
    storage_class: str | None,
    size: str,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": size}},
    }
    if storage_class:
        spec["storageClassName"] = storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": pvc_name(record),
            "namespace": namespace,
            "labels": runtime_labels(record),
        },
        "spec": spec,
    }


def service_manifest(
    record: Any,
    *,
    namespace: str,
    port: int,
) -> dict[str, Any]:
    name = service_name(record)
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": runtime_labels(record),
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {LABEL_RUNTIME: dns_name(record.runtime_id)},
            "ports": [
                {"name": "http", "port": port, "targetPort": port},
            ],
        },
    }


def pod_manifest(
    record: Any,
    *,
    namespace: str,
    image: str,
    port: int,
    environment: Mapping[str, str],
    resources: Mapping[str, Any] | None = None,
    node_selector: Mapping[str, str] | None = None,
    tolerations: list[dict[str, Any]] | None = None,
    image_pull_policy: str = "IfNotPresent",
    service_account: str | None = None,
    startup_timeout_seconds: int = 300,
) -> dict[str, Any]:
    labels = runtime_labels(record)
    env_list = [
        {"name": str(key), "value": str(value)}
        for key, value in sorted(environment.items())
    ]
    container: dict[str, Any] = {
        "name": "qwenpaw",
        "image": image,
        "imagePullPolicy": image_pull_policy,
        "ports": [{"name": "http", "containerPort": port}],
        "env": env_list,
        # The managed-runtime boundary answers 401 to anonymous
        # probes; exec with the injected internal token instead.
        "readinessProbe": {
            "exec": {
                "command": [
                    "/bin/sh",
                    "-c",
                    "curl -sf -H "
                    '"X-QwenPaw-Runtime-Token: '
                    '$QWENPAW_RUNTIME_INTERNAL_TOKEN" '
                    f"http://127.0.0.1:{port}/api/healthz",
                ],
            },
            "initialDelaySeconds": 3,
            "periodSeconds": 3,
            "failureThreshold": 30,
        },
        "volumeMounts": [
            {
                "name": "working",
                "mountPath": "/app/working",
            },
        ],
    }
    if resources:
        container["resources"] = dict(resources)
    spec: dict[str, Any] = {
        "restartPolicy": "Always",
        "serviceAccountName": service_account or "default",
        "containers": [container],
        "volumes": [
            {
                "name": "working",
                "persistentVolumeClaim": {
                    "claimName": pvc_name(record),
                },
            },
        ],
    }
    if node_selector:
        spec["nodeSelector"] = dict(node_selector)
    if tolerations:
        spec["tolerations"] = list(tolerations)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name(record),
            "namespace": namespace,
            "labels": labels,
            "annotations": {
                "qwenpaw.io/startup-timeout-seconds": str(
                    startup_timeout_seconds,
                ),
            },
        },
        "spec": spec,
    }


def sandbox_job_manifest(
    record: Any,
    *,
    namespace: str,
    image: str,
    command: list[str],
    job_id: str,
    ttl_seconds: int = 3600,
    timeout_seconds: int = 600,
    environment: Mapping[str, str] | None = None,
    resources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One on-demand sandbox Job (G7 two-tier execution).

    Tier 1 is the resident agent Pod (unchanged); tier 2 is this
    short-lived hardened Job for untrusted/heavy code execution —
    K8s never boots a fresh agent Pod per call. Hardened by
    default: non-root, all capabilities dropped, no privilege
    escalation, writable /tmp via emptyDir, TTL auto-cleanup and a
    bounded activeDeadline.
    """
    environment = environment or {}
    labels = runtime_labels(record)
    labels["qwenpaw.ai/tier"] = "sandbox"
    labels["qwenpaw.ai/job-id"] = job_id
    env_list = [
        {"name": str(key), "value": str(value)}
        for key, value in sorted(environment.items())
    ]
    container: dict[str, Any] = {
        "name": "sandbox",
        "image": image,
        "command": command,
        "env": env_list,
        "securityContext": {
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": [
            {"name": "tmp", "mountPath": "/tmp"},
        ],
    }
    if resources:
        container["resources"] = dict(resources)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"{pod_name(record)}-sbx-{job_id[:12]}",
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "ttlSecondsAfterFinished": ttl_seconds,
            "activeDeadlineSeconds": timeout_seconds,
            "backoffLimit": 0,
            "restartPolicy": "Never",
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [container],
                    "volumes": [
                        {"name": "tmp", "emptyDir": {}},
                    ],
                },
            },
        },
    }


__all__ = [
    "dns_name",
    "pod_manifest",
    "pvc_manifest",
    "runtime_labels",
    "sandbox_job_manifest",
    "service_manifest",
]
