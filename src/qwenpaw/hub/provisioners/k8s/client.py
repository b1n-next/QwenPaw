# -*- coding: utf-8 -*-
"""Minimal Kubernetes REST client for the K8s runtime provisioner.

Loads credentials from (in order):
1. ``KUBECONFIG`` / ``~/.kube/config`` (kubeconfig YAML);
2. in-cluster ServiceAccount (``KUBERNETES_SERVICE_HOST`` +
   ``/var/run/secrets/kubernetes.io/serviceaccount``).

Only the handful of namespaced CRUD calls the provisioner needs are
implemented — no cluster-wide discovery, no watch. An injectable
httpx transport keeps the client fully unit-testable.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml


class K8sClientError(RuntimeError):
    """One API call failed (status code or transport)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class K8sNotFoundError(K8sClientError):
    """The requested object does not exist (HTTP 404)."""


class K8sClient:
    """Tiny namespaced-object client over the Kubernetes REST API."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        ca_cert_path: Path | None = None,
        client_cert_path: Path | None = None,
        client_key_path: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        ssl_context: ssl.SSLContext | bool
        if ca_cert_path and Path(ca_cert_path).exists():
            ssl_context = ssl.create_default_context(
                cafile=str(ca_cert_path),
            )
            if client_cert_path and client_key_path:
                ssl_context.load_cert_chain(
                    str(client_cert_path),
                    str(client_key_path),
                )
        else:
            ssl_context = False
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            verify=ssl_context,
            transport=transport,
            timeout=timeout,
        )

    @classmethod
    def from_kubeconfig(cls, kubeconfig_path: Path) -> "K8sClient":
        """Build a client from a kubeconfig file (current context)."""
        payload = yaml.safe_load(kubeconfig_path.read_text("utf-8"))
        context_name = payload.get("current-context")
        contexts = payload.get("contexts") or []
        context = next(
            (
                entry["context"]
                for entry in contexts
                if entry.get("name") == context_name
            ),
            None,
        )
        if context is None:
            raise K8sClientError(
                f"kubeconfig has no usable context ({context_name!r})",
            )
        cluster_name = context.get("cluster")
        clusters = payload.get("clusters") or []
        cluster = next(
            (
                entry["cluster"]
                for entry in clusters
                if entry.get("name") == cluster_name
            ),
            None,
        )
        if cluster is None:
            raise K8sClientError(
                f"kubeconfig context references missing cluster "
                f"({cluster_name!r})",
            )
        user_name = context.get("user")
        users = payload.get("users") or []
        user = next(
            (
                entry["user"]
                for entry in users
                if entry.get("name") == user_name
            ),
            {},
        )
        ca_path: Path | None = None
        ca_data = cluster.get("certificate-authority-data")
        ca_file = cluster.get("certificate-authority")
        if ca_file:
            ca_path = Path(ca_file).expanduser()
        elif ca_data:
            import base64

            ca_path = Path(
                str(kubeconfig_path.parent / ".qwenpaw-k8s-ca.pem"),
            )
            ca_path.write_bytes(base64.b64decode(ca_data))
        token = user.get("token")
        return cls(
            base_url=cluster.get("server", "https://127.0.0.1:6443"),
            token=token,
            ca_cert_path=ca_path,
            client_cert_path=(
                Path(user["client-certificate"]).expanduser()
                if user.get("client-certificate")
                else None
            ),
            client_key_path=(
                Path(user["client-key"]).expanduser()
                if user.get("client-key")
                else None
            ),
        )

    @classmethod
    def from_environment(
        cls,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "K8sClient":
        """Auto-detect kubeconfig or in-cluster config."""
        import os

        kubeconfig_env = os.environ.get("KUBECONFIG")
        candidates = (
            [
                Path(part).expanduser()
                for part in kubeconfig_env.split(":")
                if part
            ]
            if kubeconfig_env
            else [Path.home() / ".kube" / "config"]
        )
        for candidate in candidates:
            if candidate.exists():
                return cls.from_kubeconfig(candidate)
        service_host = os.environ.get("KUBERNETES_SERVICE_HOST")
        if service_host:
            service_port = os.environ.get(
                "KUBERNETES_SERVICE_PORT",
                "443",
            )
            sa_dir = Path(
                "/var/run/secrets/kubernetes.io/serviceaccount",
            )
            token = (sa_dir / "token").read_text("utf-8").strip()
            return cls(
                base_url=f"https://{service_host}:{service_port}",
                token=token,
                ca_cert_path=sa_dir / "ca.crt",
                transport=transport,
            )
        raise K8sClientError(
            "no kubeconfig (~/.kube/config or $KUBECONFIG) and not "
            "running inside a cluster (KUBERNETES_SERVICE_HOST unset)",
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                path,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise K8sClientError(f"kubernetes API unreachable: {exc}") from exc
        if response.status_code == 404:
            raise K8sNotFoundError(path, status_code=404)
        if response.status_code >= 300:
            raise K8sClientError(
                f"kubernetes API {method} {path} -> "
                f"HTTP {response.status_code}: "
                f"{response.text[:200]}",
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        return response.json()

    def _ns(self, namespace: str, plural: str, name: str | None = None):
        base = f"/api/v1/namespaces/{quote(namespace)}/{plural}"
        return f"{base}/{quote(name)}" if name else base

    async def create_at(
        self,
        path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """POST to an explicit non-core API path (e.g. batch jobs)."""
        result = await self._request("POST", path, json_body=body)
        return result if isinstance(result, dict) else {}

    async def get(
        self,
        namespace: str,
        plural: str,
        name: str,
    ) -> dict[str, Any]:
        return await self._request("GET", self._ns(namespace, plural, name))

    async def create(
        self,
        namespace: str,
        plural: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            self._ns(namespace, plural),
            json_body=body,
        )

    async def delete(
        self,
        namespace: str,
        plural: str,
        name: str,
    ) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            self._ns(namespace, plural, name),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_namespace(self, name: str) -> dict[str, Any]:
        """Fetch one namespace object (used by provisioner preflight)."""
        return await self._request(
            "GET",
            f"/api/v1/namespaces/{quote(name)}",
        )
