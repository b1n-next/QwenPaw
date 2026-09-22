# -*- coding: utf-8 -*-
"""FastAPI control plane and server entry point for QwenPaw Hub."""

from __future__ import annotations

import asyncio
import datetime
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import quote, unquote

import httpx
import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from ..__version__ import __version__
from ..app.exception_handlers import register_exception_handlers
from ..constant import CORS_ORIGINS
from ..utils.http import is_loopback_host, runtime_host_allowed
from ..utils.oauth_callback import HUB_OAUTH_CALLBACK_URL_HEADER
from ..plugins.browser_access import PAWAPP_SCOPE_HEADER
from .access_security import HubAccessSecurity
from .acl import AclEngine
from .acl.console_map import effective_permissions
from .ldap_auth import load_ldap_authenticator
from .acl.resource_policies import allowed_resource_ids, resource_baseline
from .capability import (
    CapabilityRequirement,
    RuntimeCapability,
    negotiate,
)
from .acl.groups import GroupPolicyStore
from .pawapp_access import (
    SESSION_SECONDS,
    clear_sessions,
    issue_session,
    read_session,
    require_session_runtime,
    validate_app_id,
)
from .api_models import (
    AdminUserCreateBody,
    AdminUserPatchBody,
    CredentialBody,
    CredentialsBody,
    DockerImagePullBody,
    HubSettingsBody,
    PasswordChangeBody,
    RuntimeCreateBody,
    RuntimeRequirementsBody,
)
from .auth import HubAuthService, HubDatabaseBusyError, HubUser
from .bootstrap import get_hub_root
from .config import HubConfig, HubConfigStore
from .credentials import TenantCredentialVault
from .model_catalog import ModelCatalogStore
from .provisioner import (
    RuntimeModelNetwork,
    RuntimeProvisionerUnavailableError,
)
from .provisioners.k8s import K8sRuntimeProvisioner
from .metrics import HubMetrics
from .oidc import OidcClient, OidcError, OidcSettings
from .database import HubExtensionStore
from .ratelimit import RateLimiter
from .siem import SiemRelay
from .quota import QuotaEngine, UsageSnapshot
from .local_provisioner import LocalProcessRuntimeProvisioner
from .docker_images import DockerImagePullStore
from .docker_provisioner import (
    OFFICIAL_DOCKER_IMAGES,
    OFFICIAL_DOCKER_TAGS,
    DockerRuntimeProvisioner,
)
from .models import (
    RuntimeRecord,
    RuntimeSpec,
    RuntimeStartPolicy,
    RuntimeState,
)
from .operations import HubOperationsStore
from .policy_catalog import PolicyCatalogStore
from .key_pool import KeyPool
from .prompt_library import PromptLibrary
from .templates import TemplateStore
from .trace import (
    TRACE_HEADER,
    TRACEPARENT_HEADER,
    trace_id_from_headers,
    traceparent_from_headers,
)
from .usage import UsageCollector, UsageStore
from .runtime_logs import RuntimeLogCollector, RuntimeLogStore
from .oauth_routes import oauth_callback_route, runtime_oauth_callback_path
from .proxy_limits import (
    ProxyRequestIdleTimeoutError,
    ProxyRequestTooLargeError,
    limited_request_stream,
    send_with_response_header_timeout,
)
from .registry import RuntimeRegistry
from .service import RuntimeOperationConflictError, RuntimeService
from .static_files import (
    CompressedStaticFiles,
    resolve_console_response,
    resolve_console_static_dir,
)
from .invitations import InvitationService
from .model_service.storage import GovernanceStore
from .model_service.catalog import ModelCatalog
from .model_service.budget import TokenBudgetService
from .model_service.gateway import ModelGateway
from .model_service.routes import governance_router
from .model_service.listener import ModelListener
from .model_service.runtime_policy import require_model_route
from . import websocket_proxy


def build_runtime_service(
    root_dir: Path | None = None,
    hub_config: HubConfig | None = None,
) -> RuntimeService:
    """Build the local service through deployment-neutral interfaces."""
    resolved_root = (root_dir or get_hub_root()).resolve()
    registry = RuntimeRegistry(resolved_root / "control.db")
    credential_vault = TenantCredentialVault(
        registry.database_path,
        resolved_root / "secrets" / ".vault_key",
    )
    model_catalog = ModelCatalogStore(
        registry.database_path,
        resolved_root / "secrets" / ".model_catalog_key",
    )
    policy_catalog = PolicyCatalogStore(registry.database_path)
    local_provisioner = LocalProcessRuntimeProvisioner()
    docker_provisioner = DockerRuntimeProvisioner(resolved_root)
    # EP-1-6: k8s provisioner; preflight fail-closes until the cluster
    # is reachable and QWENPAW_HUB_RUNTIME_HOST_SUFFIXES is configured.
    k8s_provisioner = K8sRuntimeProvisioner()
    k8s_provisioner.configure(_k8s_provisioner_config())

    def runtime_environment(record: Any) -> dict[str, str]:
        environment = credential_vault.resolve_environment(
            tenant_id=record.tenant_id,
            runtime_id=record.runtime_id,
        )
        environment[
            "QWENPAW_RUNTIME_INTERNAL_TOKEN"
        ] = credential_vault.get_or_create_runtime_secret(
            tenant_id=record.tenant_id,
            runtime_id=record.runtime_id,
            name="QWENPAW_RUNTIME_INTERNAL_TOKEN",
        )
        # EP-1-2: inject the admin-maintained model catalog (decrypted
        # server-side only; consumed by the runtime bootstrap hook).
        bootstrap = model_catalog.bootstrap_payload()
        if bootstrap:
            environment["QWENPAW_MODEL_BOOTSTRAP_JSON"] = json.dumps(
                bootstrap,
            )
        # EP-2-13: organization policy baseline (hub_rules outrank the
        # runtime's builtin/user layers once applied at startup).
        baseline = policy_catalog.baseline_payload()
        if baseline:
            environment["QWENPAW_POLICY_BASELINE_JSON"] = json.dumps(
                baseline,
            )
        return environment

    return RuntimeService(
        root_dir=resolved_root,
        registry=registry,
        provisioners={
            local_provisioner.name: local_provisioner,
            docker_provisioner.name: docker_provisioner,
            k8s_provisioner.name: k8s_provisioner,
        },
        credential_provider=runtime_environment,
        hub_config=hub_config,
    )


def _catalog_provider_ids(model_catalog: Any) -> set[str]:
    """Enabled provider ids from the admin catalog."""
    return {
        record.provider_id
        for record in model_catalog.list_providers(include_disabled=False)
    }


def _filter_models_payload(
    raw: bytes,
    allowed_ids: set[str],
) -> bytes:
    """Keep only catalog providers in a ``GET /api/models`` body.

    Non-admins must not even see cloud providers they could never use
    (unreachable from the intranet, and a data-egress surface). A body
    that fails to parse is passed through untouched — the runtime owns
    that contract and a parse failure means an upstream anomaly, not a
    governance decision.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(data, list):
        return raw
    filtered = [
        provider
        for provider in data
        if isinstance(provider, dict) and provider.get("id") in allowed_ids
    ]
    return json.dumps(filtered).encode("utf-8")


def _catalog_allows_activation(
    model_catalog: Any,
    raw_body: bytes,
) -> bool:
    """Return True when the activation payload targets a catalog model.

    Parses a ``PUT /api/models/active`` body and checks the
    provider/model pair against the enabled admin catalog.
    """
    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    provider_id = str(payload.get("provider_id") or "")
    model_id = str(payload.get("model") or payload.get("model_id") or "")
    if not provider_id or not model_id:
        return False
    provider = model_catalog.get_provider(provider_id)
    if provider is None or not provider.enabled:
        return False
    return model_id in (provider.models or [])


class ModelNotInCatalogError(Exception):
    """Member model activation targets outside the admin catalog."""


async def _enforce_model_activation_catalog(
    app: FastAPI,
    user: HubUser,
    request: Request,
) -> bytes | None:
    """Enforce the admin catalog on member model activations.

    Returns the buffered request body when it was consumed for
    validation (the proxy must then forward those exact bytes), or
    ``None`` when the request is not a member activation and can
    stream through untouched. Raises 403 MODEL_NOT_IN_CATALOG when
    the target is outside the enabled admin catalog.
    """
    if (
        request.method != "PUT"
        or request.url.path != "/api/models/active"
        or user.role == "admin"
    ):
        return None
    raw_body = await request.body()
    allowed_target = await run_in_threadpool(
        _catalog_allows_activation,
        app.state.catalog_store,
        raw_body,
    )
    if not allowed_target:
        raise ModelNotInCatalogError()
    # E4: per-user/group model routing policies refine the catalog.
    # Deny beats allow at equal target (same as AclEngine/B5).
    target_model = _activation_target_model(raw_body)
    if target_model:
        user_groups = await run_in_threadpool(
            app.state.group_store.group_names_for,
            user.user_id,
        )
        user_policies = await run_in_threadpool(
            app.state.group_store.policies_for,
            user_id=user.user_id,
            groups=user_groups,
            role=user.role,
        )
        if not _model_policies_allow(user_policies, target_model):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "MODEL_FORBIDDEN_BY_POLICY",
                    "message": (
                        "Model is not routable for your account " "or groups."
                    ),
                    "model": target_model,
                },
            )
    return raw_body


def _activation_target_model(raw_body: bytes) -> str:
    """Best-effort model id from an activation payload (E4)."""
    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("model") or payload.get("model_id") or "")


def _model_policies_allow(policies: Any, model_id: str) -> bool:
    """Evaluate ``model:<id>`` policies: deny beats allow (E4)."""
    allowed: bool | None = None
    for policy in policies:
        kind, _, value = policy.resource.partition(":")
        if kind != "model" or not value:
            continue
        if value not in ("*", model_id):
            continue
        if policy.effect == "deny":
            return False
        allowed = True
    return allowed is not False  # no policy -> catalog decision stands


def _k8s_provisioner_config() -> dict[str, object]:
    """Read k8s provisioner settings from QWENPAW_HUB_K8S_* env vars.

    Keeps configuration ops-only (no upstream schema change): every
    key is optional; an unconfigured provisioner simply fails its
    preflight until the cluster is reachable.
    """
    mapping = {
        "QWENPAW_HUB_K8S_NAMESPACE": "namespace",
        "QWENPAW_HUB_K8S_IMAGE": "image",
        "QWENPAW_HUB_K8S_PORT": "port",
        "QWENPAW_HUB_K8S_CLUSTER_DOMAIN": "cluster_domain",
        "QWENPAW_HUB_K8S_IMAGE_PULL_POLICY": "image_pull_policy",
        "QWENPAW_HUB_K8S_STORAGE_CLASS": "storage_class",
        "QWENPAW_HUB_K8S_PVC_SIZE": "pvc_size",
        "QWENPAW_HUB_K8S_CPU_REQUEST": "cpu_request",
        "QWENPAW_HUB_K8S_CPU_LIMIT": "cpu_limit",
        "QWENPAW_HUB_K8S_MEMORY_REQUEST": "memory_request",
        "QWENPAW_HUB_K8S_MEMORY_LIMIT": "memory_limit",
        "QWENPAW_HUB_K8S_SERVICE_ACCOUNT": "service_account",
        "QWENPAW_HUB_K8S_STARTUP_TIMEOUT": "startup_timeout_seconds",
    }
    config: dict[str, object] = {}
    for env_name, key in mapping.items():
        value = os.environ.get(env_name)
        if value:
            config[key] = value
    return config


def _oidc_redirect_uri(request: Request) -> str:
    """Callback URL advertised to the IdP for this request."""
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/hub/auth/oidc/callback"


def _build_oidc_client(
    hub_config: Any,
    vault: Any = None,
) -> OidcClient | None:
    """Construct the OIDC client from current settings (or None).

    Secret resolution order (A4): explicit hub.yaml value (compat) →
    encrypted vault entry imported from the provisioner env → empty.
    """
    if hub_config is None:
        return None
    oidc_config = getattr(hub_config.control_plane, "oidc", None)
    if oidc_config is None or not oidc_config.enabled:
        return None
    client_secret = oidc_config.client_secret
    if not client_secret and vault is not None:
        client_secret = (
            vault.get(
                tenant_id="__qwenpaw_hub_system__",
                scope="control",
                name="QWENPAW_HUB_OIDC_CLIENT_SECRET",
            )
            or ""
        )
    return OidcClient(
        OidcSettings(
            issuer=oidc_config.issuer,
            client_id=oidc_config.client_id,
            client_secret=client_secret,
            username_claim=oidc_config.username_claim,
            groups_claim=oidc_config.groups_claim,
            display_name_claim=oidc_config.display_name_claim,
        ),
    )


logger = logging.getLogger(__name__)


def _ldap_provision_login(hub_auth: Any, username: str) -> Any:
    """Find-or-create the LDAP-vouched member and mint a token.

    Runs in a worker thread (sync sqlite); the random password is
    never usable — local auth for this row stays closed.
    """
    existing = hub_auth.find_by_username(username)
    if existing is not None:
        if existing.disabled:
            raise PermissionError("Account is disabled.")
        return existing, hub_auth.create_token(existing)
    created = hub_auth.create_user(
        username=username,
        password=secrets.token_urlsafe(32),
        role="user",
    )
    return created, hub_auth.create_token(created)


def _load_scim_token(path: Path) -> str | None:
    """Read the ops-managed SCIM bearer token (C5), if deployed."""
    try:
        if not path.is_file():
            return None
        if path.stat().st_mode & 0o077:
            logger.warning(
                "SCIM token file %s is group/world accessible; "
                "refusing to enable SCIM until chmod 600.",
                path,
            )
            return None
        token = path.read_text(encoding="utf-8").strip()
        return token or None
    except OSError:
        return None


def create_hub_app(  # pylint: disable=too-many-statements
    service: RuntimeService | None = None,
    auth_service: HubAuthService | None = None,
    proxy_transport: httpx.AsyncBaseTransport | None = None,
    hub_config: HubConfig | None = None,
    root_dir: Path | None = None,
    public_bind: bool = False,
    model_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Create a Hub control-plane app with an injectable runtime service."""
    runtime_service = service or build_runtime_service(
        root_dir=root_dir,
        hub_config=hub_config,
    )
    config_store = HubConfigStore(runtime_service.registry.database_path)
    effective_config = config_store.ensure(
        hub_config or runtime_service.hub_config,
        available_provisioners=set(runtime_service.provisioners),
    )
    runtime_service.apply_config(effective_config)
    credential_vault = TenantCredentialVault(
        runtime_service.registry.database_path,
        runtime_service.root_dir / "secrets" / ".vault_key",
    )
    hub_auth = auth_service or HubAuthService(
        runtime_service.registry.database_path,
        credential_vault,
    )
    governance = GovernanceStore(runtime_service.registry.database_path)
    governance_catalog = ModelCatalog(governance, credential_vault)
    model_budgets = TokenBudgetService(governance)
    model_gateway = ModelGateway(
        governance_catalog,
        model_budgets,
        model_transport,
    )
    invitations = InvitationService(governance, hub_auth)
    model_listener = ModelListener(
        governance,
        governance_catalog,
        model_gateway,
    )
    model_networks: dict[str, RuntimeModelNetwork] = {}
    original_credentials = runtime_service.credential_provider

    def _owner_resource_baseline_env(owner_user_id: str) -> dict[str, str]:
        """D2/D3: per-owner allow-list env (skills/MCP/channels).

        Computed from the owner's group policies on every (re)start so
        admin edits hot-apply on the next drift rebuild.
        """
        groups = app.state.group_store.group_names_for(owner_user_id)
        policies = app.state.group_store.policies_for(
            user_id=owner_user_id,
            groups=groups,
            role="user",
        )
        resources = resource_baseline(policies)
        if not resources:
            return {}
        return {
            "QWENPAW_RESOURCE_BASELINE_JSON": json.dumps(resources),
        }

    def managed_credentials(record):
        values = dict(original_credentials(record))
        network = model_networks[record.provisioner]
        values["QWENPAW_HUB_MODEL_URL"] = network.url(model_listener.port)
        values["QWENPAW_HUB_MODEL_TOKEN"] = governance_catalog.issue_token(
            record,
        )
        if getattr(record, "owner_user_id", None):
            values.update(_owner_resource_baseline_env(record.owner_user_id))
        return values

    def user_profile(record):
        owner = hub_auth.get_user(record.owner_user_id)
        if owner is None:
            raise ValueError("Runtime owner is unavailable")
        return owner.profile

    runtime_service.profile_provider = user_profile
    runtime_service.credential_provider = managed_credentials
    operations = HubOperationsStore(
        runtime_service.registry.database_path,
        runtime_service.root_dir,
    )
    # EP-1-4: pull-based usage accounting (no runtime patches).
    usage_store = UsageStore(runtime_service.registry.database_path)
    usage_collector = UsageCollector(
        runtime_service=runtime_service,
        credential_vault=credential_vault,
        store=usage_store,
        transport=proxy_transport,
    )
    access_security = HubAccessSecurity(
        effective_config.control_plane.security,
    )
    docker_provisioner = runtime_service.provisioners.get("docker")
    docker_pulls = (
        DockerImagePullStore(docker_provisioner)
        if isinstance(docker_provisioner, DockerRuntimeProvisioner)
        else None
    )

    async def runtime_payload(record: Any) -> dict[str, Any]:
        owner = await run_in_threadpool(
            hub_auth.get_user,
            record.owner_user_id,
        )
        return _runtime_payload(
            runtime_service,
            record,
            owner=owner,
        )

    async def runtime_payloads(records: list[Any]) -> list[dict[str, Any]]:
        owners = await run_in_threadpool(
            hub_auth.get_users,
            {record.owner_user_id for record in records},
        )
        return [
            _runtime_payload(
                runtime_service,
                record,
                owner=owners.get(record.owner_user_id),
            )
            for record in records
        ]

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        usage_collector.start()
        runtime_log_collector.start()
        try:
            await run_in_threadpool(model_gateway.recover)
            model_networks.clear()
            for name, status in runtime_service.provisioner_statuses().items():
                if status["available"]:
                    model_networks[name] = await run_in_threadpool(
                        runtime_service.provisioners[name].model_network,
                    )
            bind_hosts = {item.bind_host for item in model_networks.values()}
            async with model_listener.serve(bind_hosts):
                yield
        finally:
            await usage_collector.stop()
            await runtime_log_collector.stop()
            if docker_pulls is not None:
                await run_in_threadpool(docker_pulls.close)
            await run_in_threadpool(runtime_service.close)

    app = FastAPI(title="QwenPaw Hub", lifespan=lifespan)
    register_exception_handlers(app)
    if CORS_ORIGINS:
        origins = [item.strip() for item in CORS_ORIGINS.split(",")]
        if "*" in origins:
            raise ValueError("Hub credentialed CORS requires explicit origins")
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[origin for origin in origins if origin],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.state.runtime_service = runtime_service
    app.state.auth_service = hub_auth
    app.state.hub_config = effective_config
    app.state.config_store = config_store
    app.state.credential_vault = credential_vault
    # A4 follow-up: if the provisioner injected the OIDC client
    # secret via environment (helm Secret → env), import it once into
    # the encrypted vault and clear the process env so the plaintext
    # value only ever exists for the microseconds of this import.
    _oidc_secret_env = "QWENPAW_HUB_OIDC_CLIENT_SECRET"
    _oidc_secret_value = os.environ.get(_oidc_secret_env, "")
    if _oidc_secret_value:
        credential_vault.put(
            tenant_id="__qwenpaw_hub_system__",
            scope="control",
            name="QWENPAW_HUB_OIDC_CLIENT_SECRET",
            value=_oidc_secret_value,
            trusted=True,
        )
        os.environ.pop(_oidc_secret_env, None)
    app.state.operations = operations
    app.state.access_security = access_security
    app.state.docker_pulls = docker_pulls
    # Enterprise ACL: optional overlay at <hub root>/acl.json (hot reload).
    app.state.acl = AclEngine.from_env(config_dir=runtime_service.root_dir)
    # EP-2-1: groups/policies storage on the hub database
    app.state.group_store = GroupPolicyStore(
        runtime_service.registry.database_path,
    )
    # EP-2-3: hot-reloadable quota overlay at <hub root>/quota.json
    # (evaluated at the proxy AFTER the ACL gate, BEFORE forwarding).
    app.state.quota = QuotaEngine(
        runtime_service.root_dir / "quota.json",
    )
    # C5: SCIM deprovisioning bearer token, deployed as a file next
    # to the vault key (0600) — no schema/UI surface, ops-managed.
    app.state.ldap_authenticator = load_ldap_authenticator(
        runtime_service.root_dir,
    )
    app.state.scim_token = _load_scim_token(
        runtime_service.root_dir / "scim_token",
    )

    # EP-2-4: hand-rolled Prometheus registry (07 §4)
    app.state.metrics = HubMetrics()
    # EP-2-2: OIDC SSO client (rebuilt when admin updates settings)
    app.state.oidc_client = _build_oidc_client(
        hub_config,
        vault=credential_vault,
    )
    # E6: per-user rate/concurrency caps (hot-reloadable overlay)
    app.state.rate_limiter = RateLimiter(
        runtime_service.root_dir / "ratelimit.json",
    )
    # E5: model fallback chains as resource extensions
    app.state.model_extensions = HubExtensionStore(
        operations.database_path,
    )

    # E5 consumption side: the model gateway walks the admin-defined
    # chain when the requested model's upstream fails. Read fresh per
    # call so admin edits hot-apply; failures fail open to direct.
    def _fallback_chain_for(model_id: str) -> list[str]:
        document = app.state.model_extensions.get(
            resource_type="model",
            resource_id=model_id,
            namespace="governance",
            key="fallbacks",
        )
        if not document:
            return []
        value = document.get("value") or document.get("fallbacks")
        chain = (value or {}).get("fallbacks", [])
        return [str(item) for item in chain]

    model_gateway.fallbacks_for = _fallback_chain_for

    def _fallback_metric(original: str, used: str, _rid: str) -> None:
        try:
            app.state.metrics.inc(
                "qwenpaw_hub_model_fallback_total",
                requested=original,
                served=used,
            )
        except Exception:  # noqa: BLE001 - observability must not raise
            pass

    model_gateway.on_fallback = _fallback_metric

    # G2: runtime scheduling requirements live as a hub extension
    app.state.capability_requirement = (
        lambda: CapabilityRequirement.from_document(None)
    )
    # F9: SIEM relay (best-effort mirror of audit events)
    app.state.siem_relay = SiemRelay()
    # EP-1-1: central model provider catalog (shares control.db + its
    # own secrets key under <hub root>/secrets/).
    app.state.catalog_store = ModelCatalogStore(
        runtime_service.registry.database_path,
        runtime_service.root_dir / "secrets" / ".model_catalog_key",
    )
    model_catalog = app.state.catalog_store
    # EP-2-13: organization governance baseline (same control.db).
    app.state.policy_catalog = PolicyCatalogStore(
        runtime_service.registry.database_path,
    )
    # EP-2-19: agent template marketplace (same control.db).
    app.state.template_store = TemplateStore(
        runtime_service.registry.database_path,
    )
    # EP-2-24: prompt library + provider key pool (same control.db).
    app.state.prompt_library = PromptLibrary(
        runtime_service.registry.database_path,
    )
    app.state.key_pool = KeyPool(
        runtime_service.registry.database_path,
    )
    # EP-1-4: usage accounting store (collector started in lifespan).
    app.state.usage_store = usage_store
    app.state.usage_collector = usage_collector
    # F7: tenant-scoped runtime log tail retention (pull-based)
    runtime_log_store = RuntimeLogStore(
        runtime_service.registry.database_path,
    )
    runtime_log_collector = RuntimeLogCollector(
        runtime_service=runtime_service,
        credential_vault=credential_vault,
        store=runtime_log_store,
    )
    app.state.runtime_log_store = runtime_log_store
    app.state.runtime_log_collector = runtime_log_collector

    def require_loopback_runtime(record: RuntimeRecord) -> None:
        # EP-1-6: k8s runtimes live at cluster Service DNS names; the
        # shared runtime_host_allowed helper honours the ops-provisioned
        # suffix allowlist (fail-closed otherwise).
        if runtime_host_allowed(record.host, record.provisioner):
            return
        raise HTTPException(
            status_code=503,
            detail="Managed runtime endpoint must be loopback-only",
        )

    def runtime_url(
        record: RuntimeRecord,
        *,
        scheme: str,
        path: str,
        query: bytes = b"",
    ) -> httpx.URL:
        require_loopback_runtime(record)
        return httpx.URL(
            scheme=scheme,
            host=record.host.strip().strip("[]"),
            port=record.port,
            path=path,
            query=query,
        )

    def require_user(
        authorization: str | None = Header(default=None),
    ) -> HubUser:
        prefix = "Bearer "
        token = (
            authorization[len(prefix) :]
            if authorization and authorization.startswith(prefix)
            else ""
        )
        user = hub_auth.verify_token(token) if token else None
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return user

    def require_personal_runtime_user(
        path: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> HubUser:
        if authorization is None:
            session = read_session(hub_auth, request, path)
            if session is not None:
                user, payload = session
                request.state.pawapp_session = payload
                return user
        # Match decoding by the Runtime ASGI server and file preview router.
        normalized_path = unquote(unquote(path)).replace("\\", "/")
        # Native file previews cannot attach an Authorization header.
        if (
            authorization is None
            and request.method in {"GET", "HEAD"}
            and path.startswith("files/preview/")
            and not {".", ".."}.intersection(normalized_path.split("/"))
        ):
            token = request.query_params.get("token", "")
            authorization = f"Bearer {token}"
        return require_user(authorization)

    def require_admin(user: HubUser = Depends(require_user)) -> HubUser:
        if not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Administrator permission required",
            )
        return user

    def require_auth_access(request: Request, action: str) -> str:
        client_ip = access_security.client_ip(request)
        if access_security.is_blacklisted(client_ip):
            raise HTTPException(
                status_code=403,
                detail="This IP address is blocked by the Hub administrator.",
            )
        retry_after = access_security.retry_after(action, client_ip)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many authentication attempts. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )
        return client_ip

    async def require_runtime_access(
        runtime_id: str,
        user: HubUser,
    ) -> None:
        try:
            record = await run_in_threadpool(
                runtime_service.get,
                runtime_id,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Runtime not found",
            ) from exc
        if not user.is_admin and record.owner_user_id != user.user_id:
            raise HTTPException(status_code=404, detail="Runtime not found")

    def personal_tenant_id(user: HubUser) -> str:
        return f"personal-{user.user_id}"

    def _group_usage_fetcher(
        group: str,
    ) -> Callable[[], UsageSnapshot]:
        """Zero-arg snapshot fetcher bound to one group (mypy-friendly)."""

        def _fetch() -> UsageSnapshot:
            return _group_usage_snapshot_for(group)

        return _fetch

    def _group_usage_snapshot_for(group: str) -> UsageSnapshot:
        """Today's aggregate usage across one group's members (G3).

        Maps group -> member user ids, sums their personal tenants'
        counters from one summary pass (same 30s cache window as the
        per-user fetcher).
        """
        members = app.state.group_store.descendant_member_ids(group)
        if not members:
            return UsageSnapshot(tokens=0, requests=0)
        today = (
            datetime.datetime.now(
                datetime.timezone.utc,
            )
            .date()
            .isoformat()
        )
        summary = usage_store.summary(
            start_date=today,
            end_date=today,
        )
        tokens = 0
        requests = 0
        by_tenant = summary.get("by_user", {})
        for user_id in members:
            totals = by_tenant.get(f"personal-{user_id}")
            if not totals:
                continue
            tokens += int(totals.get("prompt_tokens", 0)) + int(
                totals.get("completion_tokens", 0),
            )
            requests += int(totals.get("call_count", 0))
        return UsageSnapshot(tokens=tokens, requests=requests)

    def _usage_snapshot_for(user_id: str) -> UsageSnapshot:
        """Today's usage counters for one user from the usage store.

        Synchronous on purpose: the engine's 30s snapshot cache calls
        this fetcher inline, so the SQLite aggregation runs at most
        once per user per cache window.
        """
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        summary = usage_store.summary(
            start_date=today,
            end_date=today,
            tenant_id=f"personal-{user_id}",
        )
        by_tenant = (
            summary.get("by_tenant", {}) if isinstance(summary, dict) else {}
        )
        totals = next(iter(by_tenant.values()), None)
        if totals is None:
            return UsageSnapshot()
        return UsageSnapshot(
            tokens=totals.prompt_tokens + totals.completion_tokens,
            requests=totals.call_count,
        )

    async def record_audit(
        user: HubUser,
        action: str,
        resource_type: str,
        resource_id: str,
        detail: dict[str, Any] | None = None,
        trace_id: str | None = None,
        *,
        outcome: str = "success",
        remote_address: str | None = None,
        quota_dimension: str | None = None,
        quota_used: int | None = None,
        quota_limit: int | None = None,
    ) -> None:
        await run_in_threadpool(
            operations.record,
            actor_user_id=user.user_id,
            actor_username=user.username,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail,
            trace_id=trace_id,
            outcome=outcome,
            remote_address=remote_address,
            quota_dimension=quota_dimension,
            quota_used=quota_used,
            quota_limit=quota_limit,
        )
        # F9: mirror to the SIEM relay when configured (never blocks
        # or fails the request path — see SiemRelay.enqueue)
        app.state.siem_relay.enqueue(
            {
                "actor_user_id": user.user_id,
                "actor_username": user.username,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "outcome": outcome,
                "trace_id": trace_id,
                "detail": detail or {},
            },
        )

    async def record_auth_event(
        action: str,
        username: str,
        remote_address: str,
        *,
        outcome: str,
        user: HubUser | None = None,
        reason: str | None = None,
    ) -> None:
        """Persist one authentication attempt on a best-effort basis.

        Rejected attempts have no authenticated actor yet, so the
        attempted username is the only usable identity. Telemetry must
        never turn a granted login into a server error, nor replace the
        real rejection status of a failed attempt, so store failures are
        logged instead of propagated.
        """
        try:
            await run_in_threadpool(
                operations.record,
                actor_user_id=user.user_id if user is not None else "",
                actor_username=(
                    user.username if user is not None else username
                ),
                action=action,
                resource_type="user",
                resource_id=(user.user_id if user is not None else username),
                detail={"reason": reason} if reason else None,
                outcome=outcome,
                remote_address=remote_address,
            )
        except Exception:  # pylint: disable=broad-except
            logging.getLogger(__name__).warning(
                "Hub authentication audit event %s was not persisted",
                action,
            )

    async def personal_runtime(user: HubUser) -> RuntimeRecord:
        try:
            runtime_service.require_provisioner_available(
                runtime_service.default_provisioner,
            )
        except RuntimeProvisionerUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        records = await run_in_threadpool(
            runtime_service.list,
            user.user_id,
        )
        preferred = next(
            (
                record
                for record in records
                if record.metadata.get("hub_default") is True
            ),
            None,
        )
        record = preferred or (records[0] if records else None)
        if record is None:
            runtime_id = f"personal-{user.user_id[:24]}"
            try:
                record = await run_in_threadpool(
                    runtime_service.create,
                    RuntimeSpec(
                        runtime_id=runtime_id,
                        tenant_id=personal_tenant_id(user),
                        owner_user_id=user.user_id,
                        metadata={"hub_default": True},
                    ),
                )
            except ValueError as exc:
                record = await run_in_threadpool(
                    runtime_service.get,
                    runtime_id,
                )
                if record.owner_user_id != user.user_id:
                    raise HTTPException(
                        status_code=409,
                        detail="Personal runtime ID is unavailable",
                    ) from exc
        return record

    async def ensure_personal_runtime(user: HubUser) -> RuntimeRecord:
        record = await personal_runtime(user)
        if record.desired_state is RuntimeState.STOPPED:
            detail = (
                "Personal runtime was disabled by an administrator."
                if record.start_policy is RuntimeStartPolicy.ADMIN_ONLY
                else "Personal runtime is stopped. Restart it to continue."
            )
            raise HTTPException(status_code=423, detail=detail)
        if record.state is RuntimeState.FAILED:
            raise HTTPException(
                status_code=503,
                detail=record.last_error
                or (
                    "Personal QwenPaw failed to start. "
                    "Resolve the failure and restart it explicitly."
                ),
            )
        if record.state is not RuntimeState.RUNNING:
            try:
                record = await runtime_service.execute(
                    "start",
                    record.runtime_id,
                )
            except RuntimeOperationConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"Personal QwenPaw failed to start: {exc}",
                ) from exc
        return record

    async def validate_credential_scope(
        scope: str,
        user: HubUser,
    ) -> None:
        if scope == "tenant":
            return
        prefix = "runtime:"
        if not scope.startswith(prefix):
            raise HTTPException(
                status_code=400,
                detail="Invalid credential scope",
            )
        runtime_id = scope[len(prefix) :]
        try:
            record = await run_in_threadpool(
                runtime_service.get,
                runtime_id,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Runtime not found",
            ) from exc
        if record.owner_user_id != user.user_id:
            raise HTTPException(status_code=404, detail="Runtime not found")

    app.include_router(
        governance_router(
            governance,
            governance_catalog,
            model_budgets,
            model_gateway,
            invitations,
            hub_auth,
            require_user,
            require_admin,
            record_audit,
        ),
    )
    app.state.model_listener = model_listener
    app.state.model_catalog = governance_catalog
    app.state.model_budgets = model_budgets
    app.state.model_gateway = model_gateway

    @app.get("/api/hub/metrics")
    async def prometheus_metrics(
        _user: HubUser = Depends(require_user),
    ) -> Response:
        """Prometheus exposition endpoint (EP-2-4, 07 §4).

        Store-backed families are sampled here (scrape frequency,
        not request frequency): runtime states from the registry,
        usage totals from the usage store, collector freshness.
        Event families (request decisions, quota soft warnings) are
        incremented at the gates.
        """
        metrics: HubMetrics = app.state.metrics
        records = await run_in_threadpool(runtime_service.registry.list)
        for record in records:
            metrics.set_gauge(
                "qwenpaw_runtime_state",
                1,
                tenant=record.tenant_id,
                state=record.state.value
                if hasattr(record.state, "value")
                else str(record.state),
            )
        usage = await run_in_threadpool(
            usage_store.summary,
            start_date=None,
            end_date=None,
        )
        by_model = usage.get("by_model", []) if isinstance(usage, dict) else []
        for row in by_model:
            metrics.set_gauge(
                "qwenpaw_hub_tokens_total",
                float(
                    row["prompt_tokens"] + row["completion_tokens"],
                ),
                model=str(row["model"]),
            )
        if usage_collector.last_pass_at is not None:
            metrics.set_gauge(
                "qwenpaw_usage_last_success_timestamp_seconds",
                float(usage_collector.last_pass_epoch or 0.0),
            )
        return Response(
            content=metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/api/hub/healthz")
    async def healthz(
        user: HubUser = Depends(require_user),
    ) -> dict[str, Any]:
        runtime_available = runtime_service.runtime_available()
        record = await personal_runtime(user) if runtime_available else None
        if (
            record is not None
            and record.desired_state is not RuntimeState.STOPPED
            and record.state in {RuntimeState.CREATED, RuntimeState.STOPPED}
        ):
            try:
                runtime_service.submit("start", record.runtime_id)
            except RuntimeOperationConflictError:
                pass
        active_operation = (
            runtime_service.active_operation(record.runtime_id)
            if record is not None
            else None
        )
        if record is not None and active_operation is None:
            record = await run_in_threadpool(
                runtime_service.get,
                record.runtime_id,
            )
        runtime_state = (
            RuntimeState.STARTING
            if active_operation in {"start", "restart", "rebuild"}
            else record.state
            if record is not None
            else None
        )
        security_levels = {
            name: provisioner.security_level
            for name, provisioner in runtime_service.provisioners.items()
        }
        return {
            "status": (
                "ok"
                if runtime_available
                and runtime_state is not RuntimeState.FAILED
                else "degraded"
            ),
            "mode": "hub",
            "security_levels": security_levels,
            "provisioners": sorted(runtime_service.provisioners),
            "provisioner_statuses": runtime_service.provisioner_statuses(),
            "default_provisioner": runtime_service.default_provisioner,
            "runtime_available": runtime_available,
            "runtime_state": runtime_state.value if runtime_state else None,
            "runtime_desired_state": (
                record.desired_state.value if record else None
            ),
            "runtime_start_policy": (
                record.start_policy.value if record else None
            ),
            "runtime_last_error": record.last_error if record else None,
        }

    @app.get("/api/version")
    async def version() -> dict[str, str]:
        """Return a public-safe control-plane readiness payload."""
        return {"version": __version__}

    @app.get("/api/auth/status")
    async def auth_status() -> dict[str, object]:
        result = await run_in_threadpool(hub_auth.status)
        return result

    def _provision_oidc_user(identity: Any) -> HubUser | None:
        """JIT login: create or find the user, sync oidc groups.

        Returns None when the account is disabled (local or by absence
        of an IdP identity previously mapped).
        """
        auth_service = app.state.auth_service
        group_store: GroupPolicyStore = app.state.group_store
        existing = auth_service.find_by_username(identity.username)
        if existing is None:
            user = auth_service.create_user(
                username=identity.username,
                password=secrets.token_urlsafe(32),
                role="user",
            )
        else:
            user = existing
            if user.disabled:
                return None
        # full-reset group sync for source='oidc' groups
        known = group_store.list_groups()
        oidc_groups = {
            row["name"]: row["group_id"]
            for row in known
            if row["source"] == "oidc"
        }
        for name in identity.groups:
            if name not in oidc_groups:
                group_id = group_store.create_group(name, source="oidc")
                oidc_groups[name] = group_id
            group_store.add_member(oidc_groups[name], user.user_id)
        for name, group_id in oidc_groups.items():
            if name not in identity.groups:
                group_store.remove_member(group_id, user.user_id)
        return user

    @app.get("/api/hub/auth/oidc/login")
    async def oidc_login(
        request: Request,
        next_path: str = Query(
            default="/",
            alias="next",
            max_length=512,
        ),
    ) -> RedirectResponse:
        """Start the OIDC authorization-code flow (EP-2-2)."""
        client: OidcClient | None = app.state.oidc_client
        if client is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "OIDC_DISABLED"},
            )
        redirect_uri = _oidc_redirect_uri(request)
        try:
            authorization_url = client.authorization_url(
                redirect_uri,
                next_path,
            )
        except OidcError as exc:
            raise HTTPException(502, {"code": str(exc)}) from exc
        return RedirectResponse(authorization_url, status_code=302)

    @app.get("/api/hub/auth/oidc/callback")
    async def oidc_callback(
        request: Request,
        code: str = Query(default="", max_length=1024),
        state: str = Query(default="", max_length=256),
    ) -> RedirectResponse:
        """Exchange the code, JIT-provision, sync groups, mint a hub token."""
        client: OidcClient | None = app.state.oidc_client
        if client is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "OIDC_DISABLED"},
            )
        next_path = client.consume_state(state)
        if next_path is None or not code:
            raise HTTPException(
                status_code=400,
                detail={"code": "OIDC_BAD_STATE"},
            )
        try:
            identity = await client.exchange_and_resolve(
                code,
                _oidc_redirect_uri(request),
            )
        except OidcError as exc:
            raise HTTPException(502, {"code": str(exc)}) from exc
        user = await run_in_threadpool(
            _provision_oidc_user,
            identity,
        )
        if user is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "OIDC_ACCOUNT_DISABLED"},
            )
        token = await run_in_threadpool(
            app.state.auth_service.create_token,
            user,
        )
        await record_audit(
            user,
            "auth.oidc.login",
            "user",
            user.user_id,
            {"username": identity.username, "groups": list(identity.groups)},
        )
        # fragment (not query) keeps the token out of server logs
        separator = "#" if "#" not in next_path else "&"
        return RedirectResponse(
            f"{next_path}{separator}qwenpaw_token={token}",
            status_code=302,
        )

    @app.post("/api/auth/register")
    async def register(
        body: CredentialsBody,
        request: Request,
    ) -> dict[str, object]:
        client_ip = require_auth_access(request, "registration")
        access_security.record_attempt("registration", client_ip)
        try:
            mode = await run_in_threadpool(hub_auth.registration_mode)
            if mode == "invite" and await run_in_threadpool(
                hub_auth.user_count,
            ):
                user, token = await run_in_threadpool(
                    invitations.redeem,
                    body.invite_code or "",
                    body.username,
                    body.password,
                )
            else:
                user, token = await run_in_threadpool(
                    hub_auth.register,
                    body.username,
                    body.password,
                )
        except PermissionError as exc:
            await record_auth_event(
                "auth.register",
                body.username,
                client_ip,
                outcome="failure",
                reason=str(exc),
            )
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except HubDatabaseBusyError as exc:
            raise HTTPException(
                status_code=503,
                detail=str(exc),
                headers={"Retry-After": "1"},
            ) from exc
        except ValueError as exc:
            await record_auth_event(
                "auth.register",
                body.username,
                client_ip,
                outcome="failure",
                reason=str(exc),
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await record_audit(
            user,
            "auth.register",
            "user",
            user.user_id,
            {"role": user.role},
            remote_address=client_ip,
        )
        return {
            "token": token,
            "username": user.username,
            "user": user.to_dict(),
        }

    @app.post("/api/auth/login")
    async def login(
        body: CredentialsBody,
        request: Request,
    ) -> dict[str, object]:
        client_ip = require_auth_access(request, "login")
        try:
            user, token = await run_in_threadpool(
                hub_auth.authenticate,
                body.username,
                body.password,
            )
        except PermissionError as exc:
            # C4: LDAP direct-bind fallback — the directory vouches
            # for the password; role/limits stay hub-owned. Unknown
            # usernames are auto-provisioned as regular members.
            ldap_auth = app.state.ldap_authenticator
            if ldap_auth is None:
                access_security.record_attempt("login", client_ip)
                await record_auth_event(
                    "auth.login",
                    body.username,
                    client_ip,
                    outcome="failure",
                    reason=str(exc),
                )
                raise HTTPException(
                    status_code=401,
                    detail=str(exc),
                ) from exc
            verified = await run_in_threadpool(
                ldap_auth.verify,
                body.username,
                body.password,
            )
            if not verified:
                access_security.record_attempt("login", client_ip)
                await record_auth_event(
                    "auth.login",
                    body.username,
                    client_ip,
                    outcome="failure",
                    reason="local+ldap both rejected",
                )
                raise HTTPException(
                    status_code=401,
                    detail="Invalid username or password.",
                ) from exc
            user, token = await run_in_threadpool(
                _ldap_provision_login,
                hub_auth,
                body.username,
            )
            await record_auth_event(
                "auth.login.ldap",
                body.username,
                client_ip,
                outcome="success",
                user=user,
            )
            return {
                "token": token,
                "username": user.username,
                "user": user.to_dict(),
            }
        access_security.clear("login", client_ip)
        await record_auth_event(
            "auth.login",
            body.username,
            client_ip,
            outcome="success",
            user=user,
        )
        return {
            "token": token,
            "username": user.username,
            "user": user.to_dict(),
        }

    @app.get("/api/auth/verify")
    async def verify(
        user: HubUser = Depends(require_user),
    ) -> dict[str, object]:
        return {
            "valid": True,
            "username": user.username,
            "user": user.to_dict(),
        }

    @app.get("/api/hub/me")
    async def current_identity(
        user: HubUser = Depends(require_user),
    ) -> dict[str, object]:
        return user.to_dict()

    @app.get("/api/hub/me/permissions")
    async def current_identity_permissions(
        user: HubUser = Depends(require_user),
    ) -> dict[str, object]:
        """Console menu deny-list, policy-aware (B5, UX only)."""
        user_groups = await run_in_threadpool(
            app.state.group_store.group_names_for,
            user.user_id,
        )
        user_policies = await run_in_threadpool(
            app.state.group_store.policies_for,
            user_id=user.user_id,
            groups=user_groups,
            role=user.role,
        )
        return effective_permissions(user.role, user_policies)

    @app.post("/api/hub/me/password")
    async def change_password(
        body: PasswordChangeBody,
        user: HubUser = Depends(require_user),
    ) -> dict[str, object]:
        updated = await run_in_threadpool(
            hub_auth.change_password,
            user.user_id,
            body.new_password,
        )
        await record_audit(
            updated,
            "auth.password_change",
            "user",
            updated.user_id,
        )
        return updated.to_dict()

    @app.post("/api/hub/me/runtime/restart")
    async def restart_own_runtime(
        user: HubUser = Depends(require_user),
    ) -> dict[str, Any]:
        record = await personal_runtime(user)
        try:
            restarted = await runtime_service.execute(
                "restart",
                record.runtime_id,
                owner_initiated=True,
            )
        except RuntimeOperationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc
        except RuntimeProvisionerUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Personal QwenPaw failed to restart: {exc}",
            ) from exc
        await record_audit(
            user,
            "runtime.restart",
            "runtime",
            restarted.runtime_id,
        )
        return await runtime_payload(restarted)

    @app.get("/api/hub/admin/users")
    async def list_users(
        _: HubUser = Depends(require_admin),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        query: str | None = Query(default=None, alias="q", max_length=128),
        role: str | None = Query(default=None),
        disabled: bool | None = Query(default=None),
    ) -> dict[str, object]:
        try:
            users, total = await run_in_threadpool(
                hub_auth.list_users_page,
                page=page,
                page_size=page_size,
                query=query,
                role=role,
                disabled=disabled,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _page_payload(
            [listed_user.to_dict() for listed_user in users],
            page,
            page_size,
            total,
        )

    @app.post("/api/hub/admin/users", status_code=201)
    async def create_user(
        body: AdminUserCreateBody,
        admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        try:
            user = await run_in_threadpool(
                hub_auth.create_user,
                username=body.username,
                password=body.password,
                role=body.role,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await record_audit(
            admin,
            "user.create",
            "user",
            user.user_id,
            {"role": user.role, "username": user.username},
        )
        return user.to_dict()

    def _erase_user_internals(user_id: str) -> dict[str, int]:
        """Shared GDPR/SCIM erasure: groups, vault, anonymize.

        Caller validates existence/self-erase rules; audit stays with
        the caller (different actor identities).
        """
        groups = list(
            app.state.group_store.group_names_for(user_id),
        )
        for group_name in groups:
            group = next(
                (
                    g
                    for g in app.state.group_store.list_groups()
                    if g.get("name") == group_name
                ),
                None,
            )
            if group:
                app.state.group_store.remove_member(
                    str(group.get("group_id")),
                    user_id,
                )
        vault = app.state.credential_vault
        tenant_creds = vault.list_metadata(
            tenant_id=f"personal-{user_id}",
        )
        for item in tenant_creds:
            vault.delete(
                tenant_id=f"personal-{user_id}",
                scope=str(item.get("scope") or ""),
                name=str(item.get("name") or ""),
            )
        anonymized = app.state.auth_service.anonymize_user(user_id)
        return {
            "groups_removed": len(groups),
            "credentials_deleted": len(tenant_creds),
            "anonymized": int(bool(anonymized)),
        }

    @app.get("/api/hub/admin/users/{user_id}/export")
    async def admin_export_user_data(
        user_id: str,
        user: HubUser = Depends(require_admin),
    ) -> Response:
        """GDPR-style data export for one user (H5).

        Aggregates the hub-plane personal data: profile (minus
        secrets), group memberships, usage summaries. Runtime
        workspace files live outside the hub plane and are exported
        via the runtime's own backup tooling.
        """
        target = await run_in_threadpool(
            app.state.auth_service.get_user,
            user_id,
        )
        if target is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "USER_NOT_FOUND"},
            )
        groups = list(
            await run_in_threadpool(
                app.state.group_store.group_names_for,
                user_id,
            ),
        )
        usage_summary = await run_in_threadpool(
            app.state.usage_store.summary,
            tenant_id=f"personal-{user_id}",
        )
        payload = {
            "exported_at": datetime.datetime.now(
                datetime.timezone.utc,
            ).isoformat(),
            "user": {
                "user_id": target.user_id,
                "username": target.username,
                "role": target.role,
                "created_at": target.created_at,
                "disabled": target.disabled,
            },
            "groups": groups,
            "usage": {
                "range": {
                    "start": usage_summary.get("start_date"),
                    "end": usage_summary.get("end_date"),
                },
                "total": usage_summary.get("total"),
                "by_model": usage_summary.get("by_model"),
                "by_agent": usage_summary.get("by_agent"),
            },
            "notes": [
                "Audit events are append-only (H2) and retained; they "
                "reference this user_id but carry no secrets.",
                "Runtime workspace files are not hub-plane data; use "
                "the runtime backup/export tooling for those.",
            ],
        }
        await record_audit(
            user,
            "user.data_exported",
            "user",
            user_id,
            {"groups": len(groups)},
        )
        stamp = datetime.datetime.now(
            datetime.timezone.utc,
        ).strftime("%Y%m%dT%H%M%SZ")
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="qwenpaw-user-{user_id}'
                    f'-{stamp}.json"'
                ),
            },
        )

    @app.delete("/api/hub/admin/users/{user_id}/data")
    async def admin_erase_user_data(
        user_id: str,
        user: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """GDPR-style erasure for one user (H5).

        Soft-deletes + anonymizes the account, strips group
        memberships and deletes the user's tenant credentials.
        Audit rows remain (append-only, no secrets — documented).
        The acting admin cannot erase themselves here.
        """
        if user_id == user.user_id:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "SELF_ERASE_FORBIDDEN",
                    "message": "Admins cannot erase their own account.",
                },
            )
        target = await run_in_threadpool(
            app.state.auth_service.get_user,
            user_id,
        )
        if target is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "USER_NOT_FOUND"},
            )
        erased = await run_in_threadpool(
            _erase_user_internals,
            user_id,
        )
        if not erased["anonymized"]:
            raise HTTPException(
                status_code=409,
                detail={"code": "ALREADY_DELETED"},
            )
        await record_audit(
            user,
            "user.data_erased",
            "user",
            user_id,
            {
                "groups_removed": erased["groups_removed"],
                "credentials_deleted": erased["credentials_deleted"],
                "anonymized": True,
            },
        )
        return {
            "erased": True,
            "user_id": user_id,
            "groups_removed": erased["groups_removed"],
            "credentials_deleted": erased["credentials_deleted"],
            "audit_retained": True,
        }

    def _require_scim(request: Request) -> None:
        """C5: constant-time bearer check against the deployed token."""
        expected = app.state.scim_token
        if not expected:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "SCIM_NOT_CONFIGURED",
                    "message": (
                        "SCIM is not enabled; deploy <hub root>"
                        "/scim_token (chmod 600)."
                    ),
                },
            )
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value:
            raise HTTPException(status_code=401, detail="bearer required")
        if not hmac.compare_digest(value, expected):
            raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/api/hub/admin/scim/status")
    async def admin_scim_status(
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Whether SCIM deprovisioning is armed (never echoes token)."""
        return {"enabled": bool(app.state.scim_token)}

    @app.post("/api/hub/scim/v2/Users/{username}")
    async def scim_update_user(
        username: str,
        request: Request,
    ) -> dict[str, object]:
        """C5: IdP-driven deprovisioning (SCIM-style PATCH/DELETE).

        IdPs push ``{"active": false}`` (or any non-true active) on
        offboarding; the hub then runs the full GDPR erasure (groups,
        credentials, anonymization) and records an audit event under
        a synthetic scim actor.
        """
        _require_scim(request)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        payload = await _scim_apply(username, body)
        return payload

    async def _scim_apply(username: str, body: dict) -> dict:
        active = body.get("active", False)
        if active is True or active == "true":
            return {
                "schemas": [
                    "urn:ietf:params:scim:schemas:core:2.0:User",
                ],
                "userName": username,
                "active": True,
                "note": "no-op; provisioning is managed locally",
            }
        target = await run_in_threadpool(
            app.state.auth_service.find_by_username,
            username,
        )
        if target is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "USER_NOT_FOUND",
                    "message": f"no live user {username!r}",
                },
            )
        erased = await run_in_threadpool(
            _erase_user_internals,
            target.user_id,
        )
        await run_in_threadpool(
            operations.record,
            actor_user_id="scim",
            actor_username="scim-idp",
            action="scim.user.deprovisioned",
            resource_type="user",
            resource_id=target.user_id,
            outcome="success",
            detail={
                "username": username,
                **erased,
            },
        )
        app.state.siem_relay.enqueue(
            {
                "action": "scim.user.deprovisioned",
                "resource_type": "user",
                "resource_id": target.user_id,
                "username": username,
            },
        )
        return {
            "schemas": [
                "urn:ietf:params:scim:schemas:core:2.0:User",
            ],
            "id": target.user_id,
            "userName": username,
            "active": False,
            **erased,
        }

    @app.delete("/api/hub/scim/v2/Users/{username}")
    async def scim_delete_user(
        username: str,
        request: Request,
    ) -> Response:
        """C5: DELETE is an alias of active=false for IdP variety."""
        _require_scim(request)
        payload = await _scim_apply(username, {"active": False})
        return Response(
            content=json.dumps(payload),
            media_type="application/scim+json",
            status_code=200,
        )

    @app.patch("/api/hub/admin/users/{user_id}")
    async def patch_user(
        user_id: str,
        body: AdminUserPatchBody,
        admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        try:
            user = await run_in_threadpool(
                hub_auth.update_user,
                user_id,
                role=body.role,
                disabled=body.disabled,
                actor_user_id=admin.user_id,
                profile=(
                    body.profile.model_dump(exclude_unset=True)
                    if body.profile is not None
                    else None
                ),
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="User not found",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await record_audit(
            admin,
            "user.update",
            "user",
            user_id,
            {"disabled": user.disabled, "role": user.role},
        )
        return user.to_dict()

    @app.get("/api/hub/admin/settings")
    async def get_hub_settings(
        _: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        config, revision, updated_at = await run_in_threadpool(
            config_store.snapshot,
        )
        config_payload = config.model_dump(mode="json")
        return {
            "config": config_payload,
            "revision": revision,
            "updated_at": updated_at,
            "available_provisioners": sorted(runtime_service.provisioners),
        }

    @app.put("/api/hub/admin/settings")
    async def update_hub_settings(
        body: HubSettingsBody,
        admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        if public_bind and body.config.control_plane.public_base_url is None:
            raise HTTPException(
                status_code=422,
                detail="Public Hub binding requires public_base_url",
            )
        try:
            config, revision, updated_at = await run_in_threadpool(
                config_store.update,
                body.config,
                expected_revision=body.revision,
                available_provisioners=set(runtime_service.provisioners),
                updated_by_user_id=admin.user_id,
            )
            await run_in_threadpool(runtime_service.apply_config, config)
            access_security.configure(config.control_plane.security)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        app.state.hub_config = config
        app.state.oidc_client = _build_oidc_client(config)
        await record_audit(
            admin,
            "settings.update",
            "setting",
            "hub_config",
            {"revision": revision},
        )
        return {
            "config": config.model_dump(mode="json"),
            "revision": revision,
            "updated_at": updated_at,
            "available_provisioners": sorted(runtime_service.provisioners),
        }

    @app.get("/api/hub/credentials")
    async def list_credentials(
        user: HubUser = Depends(require_user),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        query: str | None = Query(default=None, alias="q", max_length=128),
        scope: str | None = Query(default=None, max_length=128),
    ) -> dict[str, object]:
        items, total = await run_in_threadpool(
            credential_vault.list_metadata_page,
            tenant_id=personal_tenant_id(user),
            page=page,
            page_size=page_size,
            query=query,
            scope=scope,
        )
        return _page_payload(
            items,
            page,
            page_size,
            total,
        )

    @app.put("/api/hub/credentials", status_code=204)
    async def put_credential(
        body: CredentialBody,
        user: HubUser = Depends(require_user),
    ) -> None:
        await validate_credential_scope(body.scope, user)
        try:
            await run_in_threadpool(
                credential_vault.put,
                tenant_id=personal_tenant_id(user),
                scope=body.scope,
                name=body.name,
                value=body.value,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await record_audit(
            user,
            "credential.store",
            "credential",
            f"{body.scope}:{body.name}",
        )

    @app.delete("/api/hub/credentials/{scope}/{name}", status_code=204)
    async def delete_credential(
        scope: str,
        name: str,
        user: HubUser = Depends(require_user),
    ) -> None:
        await validate_credential_scope(scope, user)
        try:
            await run_in_threadpool(
                credential_vault.delete,
                tenant_id=personal_tenant_id(user),
                scope=scope,
                name=name,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Credential not found",
            ) from exc
        await record_audit(
            user,
            "credential.delete",
            "credential",
            f"{scope}:{name}",
        )

    @app.get("/api/hub/images")
    async def list_runtime_images(
        _: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """List administrator-managed Docker image choices and status."""
        provisioner = runtime_service.provisioners.get("docker")
        status = runtime_service.provisioner_statuses().get("docker")
        policy = runtime_service.hub_config.runtime.docker
        official = []
        if isinstance(provisioner, DockerRuntimeProvisioner):
            available = bool(status and status.get("available"))
            for source, repository in OFFICIAL_DOCKER_IMAGES.items():
                for tag in OFFICIAL_DOCKER_TAGS:
                    reference = f"{repository}:{tag}"
                    official.append(
                        {
                            "source": source,
                            "reference": reference,
                            "tag": tag,
                            "downloaded": (
                                await run_in_threadpool(
                                    provisioner.image_exists,
                                    reference,
                                )
                                if available
                                else False
                            ),
                        },
                    )
        local_images: list[dict[str, object]] = []
        if (
            isinstance(provisioner, DockerRuntimeProvisioner)
            and status
            and status.get("available")
        ):
            local_images = await run_in_threadpool(provisioner.list_images)
        return {
            "available": bool(status and status.get("available")),
            "reason": status.get("reason") if status else None,
            "sources": OFFICIAL_DOCKER_IMAGES,
            "official_images": official,
            "local_images": local_images,
            "policy": policy.model_dump(),
        }

    @app.get("/api/hub/images/pulls")
    async def list_image_pulls(
        _: HubUser = Depends(require_admin),
    ) -> list[dict[str, object]]:
        """List current and recent Docker image pulls."""
        if docker_pulls is None:
            return []
        pulls = await run_in_threadpool(docker_pulls.list)
        return [pull.to_dict() for pull in pulls]

    @app.get("/api/hub/images/pulls/{pull_id}")
    async def get_image_pull(
        pull_id: str,
        _: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Return one Docker image pull status."""
        if docker_pulls is None:
            raise HTTPException(
                status_code=503,
                detail="Docker is unavailable",
            )
        try:
            pull = await run_in_threadpool(docker_pulls.get, pull_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Pull not found",
            ) from exc
        return pull.to_dict()

    @app.post("/api/hub/images/pulls", status_code=202)
    async def pull_runtime_image(
        body: DockerImagePullBody,
        user: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Start a deduplicated Docker image pull."""
        if docker_pulls is None:
            raise HTTPException(
                status_code=503,
                detail="Docker is unavailable",
            )
        try:
            runtime_service.require_provisioner_available("docker")
            pull = await run_in_threadpool(
                docker_pulls.submit,
                body.reference,
            )
        except RuntimeProvisionerUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await record_audit(
            user,
            "image.pull",
            "docker_image",
            body.reference,
            {"pull_id": pull.pull_id},
        )
        return pull.to_dict()

    @app.get("/api/hub/runtimes")
    async def list_runtimes(
        user: HubUser = Depends(require_user),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        query: str | None = Query(default=None, alias="q", max_length=128),
        state: RuntimeState | None = Query(default=None),
        provisioner: str | None = Query(default=None, max_length=64),
        owner: str | None = Query(default=None, max_length=128),
    ) -> dict[str, object]:
        owner_user_id = None if user.is_admin else user.user_id
        records, total = await run_in_threadpool(
            runtime_service.list_page,
            page=page,
            page_size=page_size,
            owner_user_id=owner_user_id,
            query=query,
            state=state,
            provisioner=provisioner,
            owner=owner if user.is_admin else None,
        )
        items = await runtime_payloads(records)
        return _page_payload(items, page, page_size, total)

    @app.post("/api/hub/runtimes", status_code=201)
    async def create_runtime(
        body: RuntimeCreateBody,
        user: HubUser = Depends(require_user),
    ) -> dict[str, Any]:
        async def audit_creation_failure(reason: str) -> None:
            """Audit one denied creation without masking its real status."""
            try:
                await record_audit(
                    user,
                    "runtime.create",
                    "runtime",
                    body.runtime_id,
                    {
                        "auto_start": body.auto_start,
                        "reason": reason[:200],
                    },
                    outcome="failure",
                )
            except Exception:  # pylint: disable=broad-except
                logging.getLogger(__name__).warning(
                    "Hub runtime.create failure audit was not persisted",
                )

        reserved_metadata = {"local", "docker", "user_profile"} & set(
            body.metadata,
        )
        if reserved_metadata:
            await audit_creation_failure(
                "Runtime backend settings are administrator-controlled."
                f" rejected keys: {sorted(reserved_metadata)}",
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    "Runtime backend settings are " "administrator-controlled."
                ),
            )

        try:
            record = await run_in_threadpool(
                runtime_service.create,
                RuntimeSpec(
                    runtime_id=body.runtime_id,
                    tenant_id=personal_tenant_id(user),
                    owner_user_id=user.user_id,
                    provisioner=None,
                    metadata=body.metadata,
                ),
            )
            if body.auto_start:
                record = await runtime_service.execute(
                    "start",
                    body.runtime_id,
                )
        except RuntimeOperationConflictError as exc:
            await audit_creation_failure(str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeProvisionerUnavailableError as exc:
            await audit_creation_failure(str(exc))
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            await audit_creation_failure(str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            await audit_creation_failure(str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        await record_audit(
            user,
            "runtime.create",
            "runtime",
            record.runtime_id,
            {
                "auto_start": body.auto_start,
                "provisioner": record.provisioner,
            },
        )
        return await runtime_payload(record)

    @app.get("/api/hub/runtimes/{runtime_id}")
    async def get_runtime(
        runtime_id: str,
        user: HubUser = Depends(require_user),
    ) -> dict[str, Any]:
        await require_runtime_access(runtime_id, user)
        try:
            record = await run_in_threadpool(
                runtime_service.status,
                runtime_id,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Runtime not found",
            ) from exc
        return await runtime_payload(record)

    @app.post(
        "/api/hub/runtimes/{runtime_id}/sandbox-jobs",
        status_code=201,
    )
    async def launch_sandbox_job(
        runtime_id: str,
        payload: dict[str, Any],
        user: HubUser = Depends(require_user),
    ) -> dict[str, Any]:
        """Dispatch one on-demand sandbox Job (G7 tier-2).

        The resident agent Pod is untouched; untrusted or heavy
        execution lands in a hardened, TTL-cleaned Job.
        """
        await require_runtime_access(runtime_id, user)
        record = await run_in_threadpool(
            runtime_service.registry.get,
            runtime_id,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="Runtime not found")
        command = payload.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise HTTPException(422, "command must be a string list")
        if len(command) > 32:
            raise HTTPException(422, "command too long")
        ttl = int(payload.get("ttl_seconds") or 3600)
        if ttl < 60 or ttl > 86400:
            raise HTTPException(422, "ttl_seconds must be 60..86400")
        timeout = int(payload.get("timeout_seconds") or 600)
        if timeout < 10 or timeout > 7200:
            raise HTTPException(422, "timeout_seconds must be 10..7200")
        provisioner = runtime_service.provisioners.get(
            record.provisioner,
        )
        launcher = getattr(provisioner, "launch_sandbox_job", None)
        if launcher is None:
            raise HTTPException(
                status_code=501,
                detail={
                    "code": "SANDBOX_JOBS_UNSUPPORTED",
                    "message": (
                        "This runtime's provisioner does not offer "
                        "tier-2 sandbox jobs."
                    ),
                },
            )
        job_id = uuid.uuid4().hex
        try:
            job_name = await run_in_threadpool(
                launcher,
                record,
                command=command,
                job_id=job_id,
                ttl_seconds=ttl,
                timeout_seconds=timeout,
            )
        except Exception as exc:  # provisioner transport failures
            raise HTTPException(
                status_code=502,
                detail=f"Sandbox job dispatch failed: {exc}",
            ) from exc
        await record_audit(
            user,
            "runtime.sandbox_job",
            "runtime",
            runtime_id,
            {
                "job_id": job_id,
                "job_name": job_name,
                "command": command[:4],
                "ttl_seconds": ttl,
                "timeout_seconds": timeout,
            },
        )
        return {
            "job_id": job_id,
            "job_name": job_name,
            "ttl_seconds": ttl,
            "timeout_seconds": timeout,
        }

    @app.post("/api/hub/runtimes/{runtime_id}/start")
    async def start_runtime(
        runtime_id: str,
        user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        await require_runtime_access(runtime_id, user)
        # G2: requirement ⊆ capability before scheduling
        record = await run_in_threadpool(
            runtime_service.registry.get,
            runtime_id,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="Runtime not found")
        capability = RuntimeCapability.from_metadata(record.metadata)
        requirement = await run_in_threadpool(_current_requirement)
        verdict = negotiate(capability, requirement)
        if not verdict.ok:
            await record_audit(
                user,
                "runtime.start",
                "runtime",
                runtime_id,
                {
                    "reason": "capability-mismatch",
                    "missing": list(verdict.missing),
                },
                outcome="failure",
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CAPABILITY_MISMATCH",
                    "message": (
                        "Runtime capabilities do not satisfy the "
                        "hub requirements."
                    ),
                    "missing": list(verdict.missing),
                },
            )
        try:
            started = await runtime_service.execute(
                "start",
                runtime_id,
            )
        except RuntimeOperationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeProvisionerUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Runtime not found",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        await record_audit(
            user,
            "runtime.start",
            "runtime",
            runtime_id,
        )
        return await runtime_payload(started)

    @app.post("/api/hub/runtimes/{runtime_id}/rebuild")
    async def rebuild_runtime(
        runtime_id: str,
        user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """Rebuild a Docker runtime with the current global image."""
        await require_runtime_access(runtime_id, user)
        try:
            record = await runtime_service.execute(
                "rebuild",
                runtime_id,
            )
        except RuntimeOperationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeProvisionerUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Runtime not found",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        await record_audit(
            user,
            "runtime.rebuild",
            "runtime",
            runtime_id,
        )
        return await runtime_payload(record)

    @app.post("/api/hub/runtimes/{runtime_id}/stop")
    async def stop_runtime(
        runtime_id: str,
        user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        await require_runtime_access(runtime_id, user)
        try:
            record = await runtime_service.execute(
                "stop",
                runtime_id,
            )
        except RuntimeOperationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Runtime not found",
            ) from exc
        await record_audit(
            user,
            "runtime.stop",
            "runtime",
            runtime_id,
        )
        return await runtime_payload(record)

    @app.post("/api/hub/runtimes/{runtime_id}/disable")
    async def disable_runtime(
        runtime_id: str,
        user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        await require_runtime_access(runtime_id, user)
        try:
            record = await runtime_service.execute(
                "stop",
                runtime_id,
                start_policy=RuntimeStartPolicy.ADMIN_ONLY,
            )
        except RuntimeOperationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Runtime not found",
            ) from exc
        await record_audit(
            user,
            "runtime.disable",
            "runtime",
            runtime_id,
        )
        return await runtime_payload(record)

    @app.delete("/api/hub/runtimes/{runtime_id}", status_code=204)
    async def delete_runtime(
        runtime_id: str,
        user: HubUser = Depends(require_admin),
    ) -> None:
        await require_runtime_access(runtime_id, user)
        try:
            await runtime_service.execute(
                "delete",
                runtime_id,
            )
        except RuntimeOperationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Runtime not found",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await record_audit(
            user,
            "runtime.delete",
            "runtime",
            runtime_id,
        )

    @app.get("/api/hub/admin/overview")
    async def operations_overview(
        _: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        runtime_counts = await run_in_threadpool(
            runtime_service.registry.count_by_state,
        )
        host = await run_in_threadpool(operations.host_metrics)
        recent_events, _ = await run_in_threadpool(
            operations.list_events,
            page=1,
            page_size=5,
        )
        return {
            "runtime_counts": runtime_counts,
            "total_runtimes": sum(runtime_counts.values()),
            "total_users": await run_in_threadpool(hub_auth.user_count),
            "runtime_available": runtime_service.runtime_available(),
            "host": host,
            "recent_events": recent_events,
        }

    @app.get("/api/hub/admin/audit")
    async def list_audit_events(
        _: HubUser = Depends(require_admin),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        query: str | None = Query(default=None, alias="q", max_length=128),
        action: str | None = Query(default=None, max_length=128),
        outcome: str | None = Query(default=None, max_length=32),
        trace_id: str
        | None = Query(
            default=None,
            max_length=32,
            description="EP-2-11: replay one cross-plane trace",
        ),
        quota_dimension: str
        | None = Query(
            default=None,
            max_length=32,
            description="F6: filter quota-scoped audit columns",
        ),
    ) -> dict[str, object]:
        events, total = await run_in_threadpool(
            operations.list_events,
            page=page,
            page_size=page_size,
            query=query,
            action=action,
            outcome=outcome,
            trace_id=trace_id,
            quota_dimension=quota_dimension,
        )
        return _page_payload(events, page, page_size, total)

    def _provider_payload(record: Any) -> dict[str, object]:
        """Masked provider view: api_key is never echoed."""
        return {
            "provider_id": record.provider_id,
            "name": record.name,
            "base_url": record.base_url,
            "models": record.models,
            "default_model": record.default_model,
            "enabled": record.enabled,
            "api_key_set": record.api_key_set,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    @app.post("/api/hub/templates/submit")
    async def member_submit_template(
        request: Request,
        user: HubUser = Depends(require_user),
    ) -> dict[str, object]:
        """D5: a member proposes a template for review.

        Creates/updates the row at ``pending_review``; while a
        proposal is open the same member cannot overwrite it, and it
        stays invisible to the hall until an admin publishes.
        """
        body = await request.json()
        template_id = str(body.get("template_id") or "").strip()
        manifest = body.get("manifest")
        if not template_id or manifest is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_SUBMISSION",
                    "message": "template_id and manifest are required",
                },
            )
        store_templates = app.state.template_store
        existing = await run_in_threadpool(
            store_templates.get_template,
            template_id,
        )
        if existing is not None and existing.get("status") == "pending_review":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "REVIEW_IN_PROGRESS",
                    "message": "This proposal is awaiting review.",
                },
            )
        if existing is not None and existing.get("status") == "published":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "PUBLISHED_IMMUTABLE",
                    "message": (
                        "Published templates cannot be overwritten; "
                        "ask an admin to offine it first."
                    ),
                },
            )
        try:
            template = await run_in_threadpool(
                store_templates.upsert_template,
                template_id,
                manifest,
                created_by=user.username,
                status="pending_review",
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_TEMPLATE", "message": str(exc)},
            ) from None
        await record_audit(
            user,
            "template.submitted",
            "template",
            template_id,
            {"revision": template.get("revision")},
        )
        return {"template": template}

    @app.get("/api/hub/templates/mine")
    async def member_my_templates(
        user: HubUser = Depends(require_user),
    ) -> dict[str, object]:
        """D5: the caller's own proposals (any status)."""
        templates = await run_in_threadpool(
            app.state.template_store.list_templates,
            published_only=False,
        )
        mine = [
            item
            for item in templates
            if item.get("created_by") == user.username
        ]
        return {"templates": mine}

    @app.get("/api/hub/admin/templates/pending")
    async def admin_pending_templates(
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """D5 review queue: everything awaiting a decision."""
        templates = await run_in_threadpool(
            app.state.template_store.list_templates,
            published_only=False,
        )
        return {
            "templates": [
                item
                for item in templates
                if item.get("status") == "pending_review"
            ],
        }

    @app.get("/api/hub/templates")
    async def list_market_templates(
        _user: HubUser = Depends(require_user),
    ) -> dict[str, object]:
        """Hall listing: members see published templates only."""
        templates = await run_in_threadpool(
            app.state.template_store.list_templates,
            published_only=True,
        )
        return {
            "templates": [
                {
                    key: item[key]
                    for key in (
                        "template_id",
                        "name",
                        "description",
                        "revision",
                        "updated_at",
                        "graph_node_count",
                        "skills",
                    )
                }
                for item in templates
            ],
        }

    @app.post("/api/hub/templates/{template_id}/instantiate")
    async def instantiate_template(
        template_id: str,
        user: HubUser = Depends(require_user),
    ) -> dict[str, object]:
        """Instantiate one template into the caller's personal runtime.

        Pushes the manifest graph to the runtime's graph template store
        (internal token, server-to-server) and returns the conversation
        seed so the console can open a chat.
        """
        template = await run_in_threadpool(
            app.state.template_store.get_template,
            template_id,
        )
        if template is None or template["status"] != "published":
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "TEMPLATE_NOT_FOUND",
                    "message": "Template is not published",
                },
            )
        graph = (template.get("manifest") or {}).get("graph")
        # D2: agent-level access gate — group policies can restrict
        # which users/groups may instantiate which templates via the
        # ``agent_template:<id>`` name (deny beats allow; admins pass).
        if user.role != "admin":
            groups = app.state.group_store.group_names_for(user.user_id)
            policies = app.state.group_store.policies_for(
                user_id=user.user_id,
                groups=groups,
                role=user.role,
            )
            allowed_all, ids = allowed_resource_ids(
                policies,
                "agent_template",
            )
            if not allowed_all and template_id not in ids:
                await record_audit(
                    user,
                    "template.instantiate_denied",
                    "template",
                    template_id,
                    {"reason": "resource_policy"},
                )
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "TEMPLATE_FORBIDDEN",
                        "message": "Resource policy denies this template",
                    },
                )
        pushed = False
        if graph:
            pushed = await _push_graph_to_runtime(user, graph)
        await record_audit(
            user,
            "template.instantiated",
            "template",
            template_id,
            {"graph_pushed": pushed, "revision": template["revision"]},
        )
        return {
            "template_id": template_id,
            "name": template["name"],
            "prompt": template.get("prompt", ""),
            "skills": template.get("skills", []),
            "graph_pushed": pushed,
        }

    async def _push_graph_to_runtime(user: HubUser, graph: dict) -> bool:
        """Best-effort graph publish into the user's running runtime."""
        records = await run_in_threadpool(runtime_service.list)
        record = next(
            (
                item
                for item in records
                if item.owner_user_id == user.user_id
                and getattr(item, "state", None) == "running"
            ),
            None,
        )
        if record is None:
            return False
        try:
            token = await run_in_threadpool(
                credential_vault.get_runtime_secret,
                tenant_id=record.tenant_id,
                runtime_id=record.runtime_id,
                name="QWENPAW_RUNTIME_INTERNAL_TOKEN",
            )
        except Exception:  # noqa: BLE001 - instantiation must not 500
            return False
        target = f"http://{record.host}:{record.port}/api/graph/publish"

        async def _post(client, value):
            return await client.post(
                target,
                json={"graph": graph},
                headers={"Authorization": f"Bearer {value}"},
            )

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await _post(client, token)
                if response.status_code == 401:
                    # E8 grace: a freshly rotated token only reaches the
                    # runtime at its next (re)start; retry once with the
                    # previous token so pushes survive the window.
                    previous = await run_in_threadpool(
                        credential_vault.get,
                        tenant_id="__qwenpaw_hub_system__",
                        scope=(
                            f"runtime-control:{record.tenant_id}:"
                            f"{record.runtime_id}"
                        ),
                        name="QWENPAW_RUNTIME_INTERNAL_TOKEN_PREVIOUS",
                    )
                    if previous:
                        response = await _post(client, previous)
                return response.status_code == 200
        except httpx.HTTPError:
            return False

    @app.get("/api/hub/admin/templates")
    async def admin_list_templates(
        _admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Admin marketplace listing (all statuses)."""
        templates = await run_in_threadpool(
            app.state.template_store.list_templates,
        )
        return {"templates": templates}

    @app.post("/api/hub/admin/templates")
    async def admin_upsert_template(
        request: Request,
        _admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Create or update one template (revision bumps)."""
        body = await request.json()
        template_id = str(body.get("template_id") or "").strip()
        manifest = body.get("manifest")
        status = str(body.get("status") or "draft")
        try:
            template = await run_in_threadpool(
                app.state.template_store.upsert_template,
                template_id,
                manifest,
                created_by=_admin.username,
                status=status,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_TEMPLATE", "message": str(exc)},
            ) from None
        await record_audit(
            _admin,
            "template.updated",
            "template",
            template_id,
            {"revision": template.get("revision"), "status": status},
        )
        return {"template": template}

    @app.patch("/api/hub/admin/templates/{template_id}")
    async def admin_set_template_status(
        template_id: str,
        request: Request,
        _admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Publish or offline one template."""
        body = await request.json()
        status = str(body.get("status") or "").strip()
        template = await run_in_threadpool(
            app.state.template_store.set_status,
            template_id,
            status,
        )
        if template is None:
            raise HTTPException(status_code=404, detail="template not found")
        await record_audit(
            _admin,
            "template.status_changed",
            "template",
            template_id,
            {"status": status, "revision": template["revision"]},
        )
        return {"template": template}

    @app.get("/api/hub/prompts")
    async def list_prompts(
        _user: HubUser = Depends(require_user),
    ) -> dict[str, object]:
        """Approved prompt assets for members."""
        assets = await run_in_threadpool(
            app.state.prompt_library.list_assets,
        )
        return {"prompts": assets}

    @app.get("/api/hub/prompts/{asset_id}")
    async def get_prompt(
        asset_id: str,
        _user: HubUser = Depends(require_user),
    ) -> dict[str, object]:
        """Current approved content of one asset."""
        asset = await run_in_threadpool(
            app.state.prompt_library.get_asset,
            asset_id,
        )
        if asset is None or asset["current_version"] == 0:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "PROMPT_NOT_FOUND",
                    "message": "no approved version",
                },
            )
        return {
            "asset_id": asset["asset_id"],
            "name": asset["name"],
            "category": asset["category"],
            "current_version": asset["current_version"],
            "content": asset["content"],
        }

    @app.post("/api/hub/admin/prompts")
    async def propose_prompt(
        request: Request,
        admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Create an asset or propose its next version (pending)."""
        body = await request.json()
        try:
            asset = await run_in_threadpool(
                app.state.prompt_library.propose,
                str(body.get("asset_id") or ""),
                str(body.get("name") or ""),
                str(body.get("content") or ""),
                proposed_by=admin.username,
                category=str(body.get("category") or "general"),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_PROMPT", "message": str(exc)},
            ) from None
        await record_audit(
            admin,
            "prompt.proposed",
            "prompt",
            asset["asset_id"],
            {"version": asset["versions"][0]["version"]},
        )
        return {"prompt": asset}

    @app.get("/api/hub/admin/prompts")
    async def admin_list_prompts(
        _admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """All assets with pending-proposal flags."""
        assets = await run_in_threadpool(
            app.state.prompt_library.list_assets,
            include_pending=True,
        )
        return {"prompts": assets}

    @app.get("/api/hub/admin/prompts/{asset_id}")
    async def admin_get_prompt(
        asset_id: str,
        _admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Asset with its full version history."""
        asset = await run_in_threadpool(
            app.state.prompt_library.get_asset,
            asset_id,
        )
        if asset is None:
            raise HTTPException(status_code=404, detail="not found")
        return {"prompt": asset}

    @app.post("/api/hub/admin/prompts/{asset_id}/review")
    async def review_prompt(
        asset_id: str,
        request: Request,
        admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Approve or reject a pending version."""
        body = await request.json()
        version = int(body.get("version") or 0)
        decision = str(body.get("decision") or "").strip()
        if decision not in ("approved", "rejected"):
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_DECISION", "message": "bad"},
            )
        try:
            asset = await run_in_threadpool(
                app.state.prompt_library.review,
                asset_id,
                version,
                decision,
                reviewed_by=admin.username,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_PROMPT", "message": str(exc)},
            ) from None
        if asset is None:
            raise HTTPException(
                status_code=404,
                detail="version not pending",
            )
        await record_audit(
            admin,
            f"prompt.{decision}",
            "prompt",
            asset_id,
            {"version": version},
        )
        return {"prompt": asset}

    @app.get("/api/hub/admin/keys")
    async def admin_list_keys(
        _admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Key pool metadata (values never listed)."""
        keys = await run_in_threadpool(app.state.key_pool.list_keys)
        return {"keys": keys}

    @app.post("/api/hub/admin/keys")
    async def admin_add_key(
        request: Request,
        admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Register one provider key."""
        body = await request.json()
        try:
            key = await run_in_threadpool(
                app.state.key_pool.add_key,
                str(body.get("provider") or ""),
                str(body.get("key_value") or ""),
                created_by=admin.username,
                key_id=str(body.get("key_id") or ""),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_KEY", "message": str(exc)},
            ) from None
        await record_audit(
            admin,
            "key.added",
            "api_key",
            key["key_id"],
            {"provider": key["provider"]},
        )
        return {"key": key}

    @app.patch("/api/hub/admin/keys/{key_id}")
    async def admin_set_key_status(
        key_id: str,
        request: Request,
        admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Enable or disable one key."""
        body = await request.json()
        status = str(body.get("status") or "").strip()
        try:
            key = await run_in_threadpool(
                app.state.key_pool.set_status,
                key_id,
                status,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_KEY", "message": str(exc)},
            ) from None
        if key is None:
            raise HTTPException(status_code=404, detail="key not found")
        await record_audit(
            admin,
            "key.status_changed",
            "api_key",
            key_id,
            {"status": status},
        )
        return {"key": key}

    @app.post("/api/hub/keys/lease")
    async def lease_key(
        request: Request,
        _user: HubUser = Depends(require_user),
    ) -> dict[str, object]:
        """Lease the next key of a provider (round-robin, counted)."""
        body = await request.json()
        provider = str(body.get("provider") or "")
        try:
            key = await run_in_threadpool(
                app.state.key_pool.lease,
                provider,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_PROVIDER", "message": str(exc)},
            ) from None
        if key is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "NO_ACTIVE_KEY",
                    "message": f"no active key for '{provider}'",
                },
            )
        return {"lease": key}

    @app.put("/api/hub/admin/models/{model_id}/pricing")
    async def admin_put_model_pricing(
        model_id: str,
        payload: dict[str, Any],
        user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """Set unit prices for one model (E7)."""
        try:
            input_price = float(payload.get("input_per_mtok", 0))
            output_price = float(payload.get("output_per_mtok", 0))
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, "prices must be numbers") from exc
        if input_price < 0 or output_price < 0:
            raise HTTPException(422, "prices must be non-negative")
        currency = str(payload.get("currency") or "CNY")[:8]
        revision = await run_in_threadpool(
            app.state.model_extensions.put,
            resource_type="model",
            resource_id=model_id,
            namespace="governance",
            key="pricing",
            value={
                "input_per_mtok": input_price,
                "output_per_mtok": output_price,
                "currency": currency,
            },
        )
        await record_audit(
            user,
            "model.pricing.update",
            "model",
            model_id,
            {"revision": revision, "currency": currency},
        )
        return {"model_id": model_id, "revision": revision}

    @app.get("/api/hub/admin/usage/costs")
    async def admin_usage_costs(
        user: HubUser = Depends(require_admin),
        start: str | None = Query(default=None, max_length=10),
        end: str | None = Query(default=None, max_length=10),
    ) -> dict[str, Any]:
        """Token usage priced per model, user and group (E7)."""
        pricing = await run_in_threadpool(
            app.state.model_extensions.list_by_type,
            resource_type="model",
            namespace="governance",
            key="pricing",
        )
        summary = await run_in_threadpool(
            app.state.usage_store.summary,
            start_date=start,
            end_date=end,
        )
        currencies: dict[str, float] = {}
        unpriced: dict[str, dict[str, int]] = {}
        by_model: list[dict[str, Any]] = []
        for row in summary.get("by_model", []):
            model = str(row.get("model") or "(unknown)")
            prompt = int(row.get("prompt_tokens", 0))
            completion = int(row.get("completion_tokens", 0))
            entry = {
                "model": model,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "requests": int(row.get("call_count", 0)),
            }
            price = pricing.get(model)
            if price is None:
                unpriced[model] = {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                }
            else:
                currency = str(price.get("currency", "CNY"))
                cost = prompt / 1_000_000 * float(
                    price["input_per_mtok"],
                ) + completion / 1_000_000 * float(price["output_per_mtok"])
                entry["cost"] = round(cost, 4)
                entry["currency"] = currency
                currencies[currency] = currencies.get(currency, 0.0) + cost
            by_model.append(entry)
        # group view: tenant personal-<user> -> group names
        tenant_to_groups: dict[str, list[str]] = {}
        for tenant in summary.get("by_user", {}):
            user_id = (
                tenant[len("personal-") :]
                if tenant.startswith("personal-")
                else ""
            )
            if not user_id:
                continue
            names = await run_in_threadpool(
                app.state.group_store.group_names_for,
                user_id,
            )
            if names:
                tenant_to_groups[tenant] = list(names)
        group_costs: dict[str, dict[str, Any]] = {}
        for tenant, totals in summary.get("by_user", {}).items():
            for group in tenant_to_groups.get(tenant, ["(ungrouped)"]):
                bucket = group_costs.setdefault(
                    group,
                    {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "requests": 0,
                    },
                )
                bucket["prompt_tokens"] += int(
                    totals.get("prompt_tokens", 0),
                )
                bucket["completion_tokens"] += int(
                    totals.get("completion_tokens", 0),
                )
                bucket["requests"] += int(totals.get("call_count", 0))
        # E10: agent-level billing — same pricing, grouped by agent
        by_agent: list[dict[str, Any]] = []
        for row in summary.get("by_agent", []):
            agent = str(row.get("agent_id") or "(unattributed)")
            prompt = int(row.get("prompt_tokens", 0))
            completion = int(row.get("completion_tokens", 0))
            entry = {
                "agent_id": agent,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "requests": int(row.get("call_count", 0)),
            }
            # model split unavailable per agent here; price with the
            # blended model mix is intentionally NOT invented — agents
            # get exact costs only via the export (per-row pricing).
            by_agent.append(entry)
        await record_audit(
            user,
            "usage.costs.read",
            "usage",
            "costs",
            {"start": start, "end": end},
        )
        return {
            "by_model": by_model,
            "by_group": [
                dict(group, group=name)
                for name, group in sorted(group_costs.items())
            ],
            "by_agent": by_agent,
            "totals": {
                currency: round(cost, 4)
                for currency, cost in currencies.items()
            },
            "unpriced_models": sorted(unpriced),
            "range": {"start": start, "end": end},
        }

    @app.get("/api/hub/admin/usage/costs/export")
    async def admin_usage_costs_export(
        user: HubUser = Depends(require_admin),
        start: str | None = Query(default=None, max_length=10),
        end: str | None = Query(default=None, max_length=10),
    ) -> Response:
        """Billing export: one CSV row per tenant×agent×provider×model.

        E10: exact per-row pricing (no blended estimates), group
        membership resolved at export time, currencies kept per-row
        because the model price table may mix them.
        """
        import csv
        import io

        pricing = await run_in_threadpool(
            app.state.model_extensions.list_by_type,
            resource_type="model",
            namespace="governance",
            key="pricing",
        )
        rows = await run_in_threadpool(
            app.state.usage_store.detail_rows,
            start_date=start,
            end_date=end,
        )
        group_cache: dict[str, list[str]] = {}

        async def _groups_for(user_id: str) -> list[str]:
            if user_id not in group_cache:
                group_cache[user_id] = list(
                    await run_in_threadpool(
                        app.state.group_store.group_names_for,
                        user_id,
                    ),
                )
            return group_cache[user_id]

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "date",
                "user_id",
                "groups",
                "agent_id",
                "provider_id",
                "model",
                "prompt_tokens",
                "completion_tokens",
                "requests",
                "cost",
                "currency",
            ],
        )
        for row in rows:
            tenant = str(row.get("tenant_id") or "")
            user_id = (
                tenant[len("personal-") :]
                if tenant.startswith("personal-")
                else tenant
            )
            model = str(row.get("model") or "")
            price = pricing.get(model)
            prompt = int(row.get("prompt_tokens") or 0)
            completion = int(row.get("completion_tokens") or 0)
            cost = ""
            currency = ""
            if price is not None:
                cost = round(
                    prompt / 1_000_000 * float(price["input_per_mtok"])
                    + completion / 1_000_000 * float(price["output_per_mtok"]),
                    6,
                )
                currency = str(price.get("currency", "CNY"))
            writer.writerow(
                [
                    row.get("usage_date"),
                    user_id,
                    ";".join(await _groups_for(user_id)) or "(ungrouped)",
                    row.get("agent_id") or "(unattributed)",
                    row.get("provider_id") or "",
                    model,
                    prompt,
                    completion,
                    int(row.get("call_count") or 0),
                    cost,
                    currency,
                ],
            )
        await record_audit(
            user,
            "usage.costs.export",
            "usage",
            "costs-export",
            {"start": start, "end": end, "rows": len(rows)},
        )
        stamp = datetime.datetime.now(
            datetime.timezone.utc,
        ).strftime("%Y%m%dT%H%M%SZ")
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="qwenpaw-costs-{stamp}.csv"'
                ),
            },
        )

    @app.get("/api/hub/models")
    async def user_model_catalog(
        user: HubUser = Depends(require_user),
    ) -> dict[str, Any]:
        """Catalog visible to the caller, policy-filtered (E9).

        Visibility == activatability: the same model:* policies
        gate the E4 activation path, so the catalog never shows a
        model the proxy would refuse to activate.
        """
        records = await run_in_threadpool(
            model_catalog.list_providers,
            include_disabled=False,
        )
        user_groups = await run_in_threadpool(
            app.state.group_store.group_names_for,
            user.user_id,
        )
        user_policies = await run_in_threadpool(
            app.state.group_store.policies_for,
            user_id=user.user_id,
            groups=user_groups,
            role=user.role,
        )
        models: list[dict[str, Any]] = []
        for record in records:
            for model in record.models or []:
                if _model_policies_allow(user_policies, model):
                    models.append(
                        {
                            "provider_id": record.provider_id,
                            "model": model,
                            "default": model == record.default_model,
                        },
                    )
        return {"models": models, "total": len(models)}

    @app.get("/api/hub/models/fallbacks")
    async def models_fallbacks(
        _user: HubUser = Depends(require_user),
    ) -> dict[str, Any]:
        """Fallback chains for all governed models (E5)."""
        return await run_in_threadpool(
            app.state.model_extensions.list_by_type,
            resource_type="model",
            namespace="governance",
            key="fallbacks",
        )

    @app.put("/api/hub/admin/models/{model_id}/fallbacks")
    async def admin_put_model_fallbacks(
        model_id: str,
        payload: dict[str, Any],
        user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """Define the fallback chain for one model (E5)."""
        raw = payload.get("fallbacks")
        if not isinstance(raw, list) or not all(
            isinstance(item, str) and item for item in raw
        ):
            raise HTTPException(422, "fallbacks must be a list of ids")
        if model_id in raw:
            raise HTTPException(422, "fallback chain must not self-loop")
        revision = await run_in_threadpool(
            app.state.model_extensions.put,
            resource_type="model",
            resource_id=model_id,
            namespace="governance",
            key="fallbacks",
            value={"fallbacks": raw},
        )
        await record_audit(
            user,
            "model.fallbacks.update",
            "model",
            model_id,
            {"fallbacks": raw, "revision": revision},
        )
        return {"model_id": model_id, "fallbacks": raw, "revision": revision}

    @app.get("/api/hub/admin/models/{model_id}/fallbacks")
    async def admin_get_model_fallbacks(
        model_id: str,
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """Read one model's fallback chain (E5)."""
        document = await run_in_threadpool(
            app.state.model_extensions.get,
            resource_type="model",
            resource_id=model_id,
            namespace="governance",
            key="fallbacks",
        )
        if document is None:
            return {"model_id": model_id, "fallbacks": []}
        return {
            "model_id": model_id,
            "fallbacks": document["value"]["fallbacks"],
            "revision": document["revision"],
        }

    def _current_requirement() -> CapabilityRequirement:
        document = app.state.model_extensions.get(
            resource_type="hub",
            resource_id="global",
            namespace="governance",
            key="runtime_requirements",
        )
        return CapabilityRequirement.from_document(document)

    @app.get("/api/hub/admin/runtime-requirements")
    async def admin_get_runtime_requirements(
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """Current scheduling requirements (G2)."""
        requirement = await run_in_threadpool(_current_requirement)
        return {
            "min_version": requirement.min_version,
            "sandbox_required": requirement.sandbox_required,
            "tools_required": list(requirement.tools_required),
        }

    @app.put("/api/hub/admin/runtime-requirements")
    async def admin_put_runtime_requirements(
        body: RuntimeRequirementsBody,
        user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """Set scheduling requirements; negotiated at runtime start (G2)."""
        revision = await run_in_threadpool(
            app.state.model_extensions.put,
            resource_type="hub",
            resource_id="global",
            namespace="governance",
            key="runtime_requirements",
            value={
                "min_version": body.min_version,
                "sandbox_required": body.sandbox_required,
                "tools_required": body.tools_required,
            },
        )
        await record_audit(
            user,
            "runtime.requirements.update",
            "hub",
            "runtime_requirements",
            {"revision": revision, **body.model_dump()},
        )
        return {"revision": revision, **body.model_dump()}

    @app.get("/api/hub/admin/siem")
    async def admin_siem_status(
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """Relay health: queue depth, sent/dropped, last error (F9)."""
        return await run_in_threadpool(app.state.siem_relay.stats)

    @app.put("/api/hub/admin/siem")
    async def admin_siem_configure(
        payload: dict[str, Any],
        user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """Configure the SIEM webhook (F9). Empty endpoint disables."""
        endpoint = payload.get("endpoint")
        if endpoint is not None and not isinstance(endpoint, str):
            raise HTTPException(422, "endpoint must be a string")
        if endpoint and not endpoint.startswith(("http://", "https://")):
            raise HTTPException(422, "endpoint must be an http(s) URL")
        secret = str(payload.get("secret") or "")
        batch_size = int(payload.get("batch_size") or 50)
        if batch_size < 1 or batch_size > 1000:
            raise HTTPException(422, "batch_size must be 1..1000")
        flush_interval = float(payload.get("flush_interval") or 5.0)
        if flush_interval < 0.5 or flush_interval > 300:
            raise HTTPException(422, "flush_interval must be 0.5..300")
        await run_in_threadpool(
            app.state.siem_relay.configure,
            endpoint or None,
            secret=secret,
            batch_size=batch_size,
            flush_interval=flush_interval,
        )
        await record_audit(
            user,
            "siem.configure",
            "hub",
            "siem_relay",
            {
                "endpoint": endpoint or None,
                "batch_size": batch_size,
                "flush_interval": flush_interval,
            },
        )
        return await run_in_threadpool(app.state.siem_relay.stats)

    @app.get("/api/hub/admin/audit/verify")
    async def admin_audit_verify(
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """Walk the audit hash chain; report the first break (H2)."""
        return await run_in_threadpool(
            app.state.operations.verify_chain,
        )

    @app.get("/api/hub/admin/audit/export")
    async def admin_audit_export(
        _user: HubUser = Depends(require_admin),
        before: str | None = Query(default=None, max_length=32),
        after: str | None = Query(default=None, max_length=32),
    ) -> Response:
        """Stream audit rows incl. hash columns as JSONL (H3)."""
        rows = await run_in_threadpool(
            lambda: list(
                app.state.operations.iter_events(
                    before=before,
                    after=after,
                ),
            ),
        )
        payload = "".join(
            json.dumps(row, ensure_ascii=False) + "\n" for row in rows
        )
        return Response(
            content=payload,
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": (
                    'attachment; filename="audit-export.jsonl"'
                ),
            },
        )

    @app.post("/api/hub/admin/audit/prune")
    async def admin_audit_prune(
        payload: dict[str, Any],
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """Archive-then-delete rows older than ``before`` (H3).

        The archive JSONL (hash columns included) lands in the hub
        root before any deletion, and the pruned segment's head hash
        is recorded as a chain anchoring point.
        """
        cutoff = str(payload.get("before") or "")
        try:
            datetime.datetime.fromisoformat(cutoff)
        except ValueError as exc:
            raise HTTPException(422, "before must be ISO date") from exc
        result = await run_in_threadpool(
            app.state.operations.prune_before,
            cutoff,
        )
        await record_audit(
            _user,
            "audit.prune",
            "audit",
            cutoff,
            result,
        )
        return result

    @app.get("/api/hub/admin/audit/archives")
    async def admin_audit_archives(
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """Pruned chain segments with anchoring hashes (H3)."""
        return {
            "archives": await run_in_threadpool(
                app.state.operations.list_archives,
            ),
        }

    @app.get("/api/hub/admin/audit/chain-head")
    async def admin_audit_chain_head(
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """Latest chain digest — anchor this externally (runbook)."""
        return await run_in_threadpool(
            app.state.operations.chain_head,
        )

    @app.get("/api/hub/admin/groups")
    async def admin_list_groups(
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """All groups with member counts (EP-2-1)."""
        return {
            "groups": await run_in_threadpool(
                app.state.group_store.list_groups,
            ),
        }

    @app.post("/api/hub/admin/groups")
    async def admin_create_group(
        payload: dict[str, Any],
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        store: GroupPolicyStore = app.state.group_store
        name = str(payload.get("name") or "")
        parent = payload.get("parent_group_id")
        try:
            group_id = await run_in_threadpool(
                store.create_group,
                name,
                parent_group_id=(str(parent) if parent else None),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await record_audit(
            _user,
            "group.created",
            "group",
            group_id,
            {"name": name, "parent_group_id": parent},
        )
        return {"group_id": group_id, "name": name}

    @app.delete("/api/hub/admin/groups/{group_id}")
    async def admin_delete_group(
        group_id: str,
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        deleted = await run_in_threadpool(
            app.state.group_store.delete_group,
            group_id,
        )
        if not deleted:
            raise HTTPException(404, "group not found")
        return {"deleted": True}

    @app.post("/api/hub/admin/groups/{group_id}/members")
    async def admin_add_member(
        group_id: str,
        payload: dict[str, Any],
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        user_id = str(payload.get("user_id") or "")
        if not user_id:
            raise HTTPException(400, "user_id required")
        try:
            await run_in_threadpool(
                app.state.group_store.add_member,
                group_id,
                user_id,
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(404, "group not found") from exc
        return {"added": True}

    @app.delete(
        "/api/hub/admin/groups/{group_id}/members/{user_id}",
    )
    async def admin_remove_member(
        group_id: str,
        user_id: str,
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        await run_in_threadpool(
            app.state.group_store.remove_member,
            group_id,
            user_id,
        )
        return {"removed": True}

    @app.get("/api/hub/admin/policies")
    async def admin_list_policies(
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        return {
            "policies": [
                {
                    "policy_id": policy.policy_id,
                    "subject": policy.subject,
                    "resource": policy.resource,
                    "effect": policy.effect,
                    "expires_at": policy.expires_at,
                }
                for policy in await run_in_threadpool(
                    app.state.group_store.list_policies,
                )
            ],
        }

    @app.post("/api/hub/admin/policies")
    async def admin_create_policy(
        payload: dict[str, Any],
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        store: GroupPolicyStore = app.state.group_store
        try:
            policy_id = await run_in_threadpool(
                store.create_policy,
                subject_kind=str(payload.get("subject_kind") or ""),
                subject_value=str(payload.get("subject_value") or ""),
                resource=str(payload.get("resource") or ""),
                effect=str(payload.get("effect") or ""),
                expires_at=(
                    str(payload.get("expires_at"))
                    if payload.get("expires_at")
                    else None
                ),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await record_audit(
            _user,
            "policy.created",
            "policy",
            policy_id,
            {
                "subject": payload.get("subject_kind"),
                "resource": payload.get("resource"),
                "effect": payload.get("effect"),
                "expires_at": payload.get("expires_at"),
            },
        )
        return {"policy_id": policy_id}

    @app.post("/api/hub/admin/policies/purge-expired")
    async def admin_purge_expired_policies(
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """C8 housekeeping: physically delete expired rows."""
        removed = await run_in_threadpool(
            app.state.group_store.purge_expired_policies,
        )
        await record_audit(
            _user,
            "policy.expired_purged",
            "policy",
            "*",
            {"removed": removed},
        )
        return {"removed": removed}

    @app.delete("/api/hub/admin/policies/{policy_id}")
    async def admin_delete_policy(
        policy_id: str,
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        deleted = await run_in_threadpool(
            app.state.group_store.delete_policy,
            policy_id,
        )
        if not deleted:
            raise HTTPException(404, "policy not found")
        return {"deleted": True}

    @app.get("/api/hub/admin/quota")
    async def admin_quota_status(
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """Quota configuration and per-known-user ratios (EP-2-3)."""
        engine: QuotaEngine = app.state.quota
        rows = []
        for hub_user in await run_in_threadpool(
            app.state.auth_service.list_users,
        ):
            snapshot = _usage_snapshot_for(hub_user.user_id)
            rows.append(engine.status(hub_user.user_id, snapshot))
        return {"quota": rows}

    @app.get("/api/hub/admin/quota/groups")
    async def admin_quota_groups_status(
        _user: HubUser = Depends(require_admin),
    ) -> dict[str, Any]:
        """Group-aggregate quota ratios (G3)."""
        engine: QuotaEngine = app.state.quota
        rows = []
        for group in await run_in_threadpool(
            app.state.group_store.list_groups,
        ):
            name = str(group.get("name") or "")
            if not name:
                continue
            snapshot = engine.group_snapshot(
                name,
                _group_usage_fetcher(name),
            )
            rows.append(engine.group_status(name, snapshot))
        return {"groups": rows}

    @app.get("/api/hub/admin/policy/baseline")
    async def get_policy_baseline(
        _admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Read the organization governance baseline (EP-2-13)."""
        baseline = await run_in_threadpool(
            app.state.policy_catalog.get_baseline,
        )
        return {"baseline": baseline}

    @app.put("/api/hub/admin/policy/baseline")
    async def update_policy_baseline(
        request: Request,
        _admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Create or replace the organization governance baseline.

        Takes effect on each personal runtime's next (re)start — the
        baseline rides the provisioner environment and is applied as a
        rules layer local policy.yaml cannot downgrade.
        """
        body = await request.json()
        rules = body.get("rules")
        enabled = bool(body.get("enabled", True))
        try:
            baseline = await run_in_threadpool(
                app.state.policy_catalog.update_baseline,
                rules,
                updated_by=_admin.username,
                enabled=enabled,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_BASELINE", "message": str(exc)},
            ) from None
        await record_audit(
            _admin,
            "policy.updated",
            "policy",
            "baseline",
            {
                "revision": baseline.get("revision"),
                "rule_count": len(baseline.get("rules") or []),
                "enabled": baseline.get("enabled"),
            },
        )
        return {"baseline": baseline}

    @app.get("/api/hub/admin/models/providers")
    async def list_model_providers(
        _admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """List catalog providers (masked, secrets never echoed)."""
        records = await run_in_threadpool(model_catalog.list_providers)
        return {
            "providers": [_provider_payload(r) for r in records],
            "total": len(records),
        }

    @app.post(
        "/api/hub/admin/models/providers",
        status_code=201,
    )
    async def upsert_model_provider(
        body: dict[str, Any],
        admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Create or replace a catalog provider."""
        try:
            record = await run_in_threadpool(
                model_catalog.upsert_provider,
                provider_id=str(body.get("provider_id") or ""),
                name=str(body.get("name") or body.get("provider_id") or ""),
                base_url=str(body.get("base_url") or ""),
                api_key=body.get("api_key"),
                models=[str(m) for m in body.get("models") or []],
                default_model=body.get("default_model"),
                enabled=bool(body.get("enabled", True)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await record_audit(
            admin,
            "model_catalog.upsert",
            "model_provider",
            record.provider_id,
            detail={
                "base_url": record.base_url,
                "models": len(record.models),
                "enabled": record.enabled,
            },
        )
        return _provider_payload(record)

    @app.patch("/api/hub/admin/models/providers/{provider_id}")
    async def patch_model_provider(
        provider_id: str,
        body: dict[str, Any],
        admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Partial update; omitted api_key keeps the stored secret."""
        existing = model_catalog.get_provider(provider_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="provider not found")
        try:
            record = await run_in_threadpool(
                model_catalog.upsert_provider,
                provider_id=provider_id,
                name=str(body.get("name", existing.name)),
                base_url=str(body.get("base_url", existing.base_url)),
                api_key=body.get("api_key"),
                models=list(
                    body.get("models", existing.models),
                ),
                default_model=body.get(
                    "default_model",
                    existing.default_model,
                ),
                enabled=bool(body.get("enabled", existing.enabled)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await record_audit(
            admin,
            "model_catalog.patch",
            "model_provider",
            provider_id,
            detail={"enabled": record.enabled},
        )
        return _provider_payload(record)

    @app.delete("/api/hub/admin/models/providers/{provider_id}")
    async def delete_model_provider(
        provider_id: str,
        admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Remove a catalog provider (runtimes sync on next restart)."""
        deleted = await run_in_threadpool(
            model_catalog.delete_provider,
            provider_id,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="provider not found")
        await record_audit(
            admin,
            "model_catalog.delete",
            "model_provider",
            provider_id,
            detail={},
        )
        return {"deleted": provider_id}

    @app.post("/api/hub/admin/models/providers/{provider_id}/rotate-key")
    async def rotate_provider_key(
        provider_id: str,
        request: Request,
        admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Rotate one provider API key with a preflight probe (E8).

        The new key is verified against GET {base_url}/models
        before anything is stored; a failed probe keeps the old key
        (fail-closed rotation) and reports the upstream answer.
        """
        record = model_catalog.get_provider(provider_id)
        if record is None:
            raise HTTPException(status_code=404, detail="provider not found")
        body = await request.json()
        new_key = str(body.get("api_key") or "")
        if not new_key:
            raise HTTPException(
                status_code=422,
                detail={"code": "KEY_REQUIRED"},
            )
        base_url = record.base_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    f"{base_url}/models",
                    headers={"Authorization": f"Bearer {new_key}"},
                )
            ok = response.status_code < 500
            message = f"upstream responded HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            ok = False
            message = f"connection failed: {type(exc).__name__}"
        if not ok:
            await record_audit(
                admin,
                "model_catalog.key_rotate_rejected",
                "model_provider",
                provider_id,
                detail={"probe": message},
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ROTATE_PREFLIGHT_FAILED",
                    "message": message,
                },
            )
        updated = await run_in_threadpool(
            model_catalog.upsert_provider,
            provider_id=provider_id,
            name=record.name,
            base_url=record.base_url,
            api_key=new_key,
            models=record.models,
            default_model=record.default_model,
            enabled=record.enabled,
        )
        await record_audit(
            admin,
            "model_catalog.key_rotated",
            "model_provider",
            provider_id,
            detail={"probe": message},
        )
        return _provider_payload(updated)

    @app.post("/api/hub/admin/runtimes/{runtime_id}/rotate-token")
    async def rotate_runtime_token(
        runtime_id: str,
        admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Rotate one runtime's internal boundary token (E8).

        The new token takes effect when the runtime next (re)starts
        (env is injected at provision time). Until then hub→runtime
        pushes fall back to the previous token inside a 24h grace
        window, so an in-flight runtime is never locked out.
        """
        records = await run_in_threadpool(runtime_service.list)
        record = next(
            (r for r in records if r.runtime_id == runtime_id),
            None,
        )
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "RUNTIME_NOT_FOUND"},
            )
        new_token = secrets.token_urlsafe(32)
        previous = await run_in_threadpool(
            credential_vault.get_runtime_secret,
            tenant_id=record.tenant_id,
            runtime_id=record.runtime_id,
            name="QWENPAW_RUNTIME_INTERNAL_TOKEN",
        )
        scope = f"runtime-control:{record.tenant_id}:{record.runtime_id}"
        system_tenant = "__qwenpaw_hub_system__"
        if previous:
            await run_in_threadpool(
                credential_vault.put,
                tenant_id=system_tenant,
                scope=scope,
                name="QWENPAW_RUNTIME_INTERNAL_TOKEN_PREVIOUS",
                value=previous,
                trusted=True,
            )
        await run_in_threadpool(
            credential_vault.put,
            tenant_id=system_tenant,
            scope=scope,
            name="QWENPAW_RUNTIME_INTERNAL_TOKEN",
            value=new_token,
            trusted=True,
        )
        await record_audit(
            admin,
            "runtime.token.rotated",
            "runtime",
            runtime_id,
            detail={"grace_hours": 24},
        )
        return {
            "runtime_id": runtime_id,
            "applies_on": "next runtime (re)start",
            "grace_hours": 24,
        }

    @app.post("/api/hub/admin/models/providers/{provider_id}/test")
    async def test_model_provider(
        provider_id: str,
        admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Server-side connectivity probe (sanitized, no key echo)."""
        record = model_catalog.get_provider(provider_id)
        if record is None:
            raise HTTPException(status_code=404, detail="provider not found")
        base_url = record.base_url.rstrip("/")
        try:
            api_key = model_catalog.get_api_key(provider_id)
        except KeyError:
            api_key = ""
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    f"{base_url}/models",
                    headers=headers,
                )
            ok = response.status_code < 500
            message = (
                f"upstream responded HTTP {response.status_code}"
                if ok
                else f"upstream error HTTP {response.status_code}"
            )
        except httpx.HTTPError as exc:
            ok = False
            message = f"connection failed: {type(exc).__name__}"
        await record_audit(
            admin,
            "model_catalog.test",
            "model_provider",
            provider_id,
            detail={"reachable": ok},
        )
        return {"success": ok, "message": message}

    @app.get("/api/hub/admin/runtimes/{runtime_id}/health")
    async def admin_runtime_health(
        runtime_id: str,
        _admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Config-level runtime health detail (F8).

        K8s runtimes: pod phase / restarts / scheduled requests+limits
        / node (no metrics-server needed). Live usage stays on the
        Prometheus plane (EP-2-4). Other provisioners report
        ``supported: false`` with the observed state only.
        """
        records = await run_in_threadpool(runtime_service.list)
        record = next(
            (r for r in records if r.runtime_id == runtime_id),
            None,
        )
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "RUNTIME_NOT_FOUND"},
            )
        base: dict[str, object] = {
            "runtime_id": runtime_id,
            "state": str(getattr(record.state, "value", record.state)),
            "provisioner": record.provisioner,
            "owner_user_id": record.owner_user_id,
        }
        provisioner = runtime_service.provisioners.get(record.provisioner)
        pod_health = getattr(provisioner, "pod_health", None)
        if pod_health is None:
            base["supported"] = False
            return base
        detail = await run_in_threadpool(pod_health, record)
        base["supported"] = True
        base["pod"] = detail or {"present": False}
        return base

    @app.get("/api/hub/admin/runtimes/logs")
    async def admin_runtime_logs_index(
        _admin: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Runtimes with retained log tails (F7)."""
        ids = await run_in_threadpool(
            app.state.runtime_log_store.list_runtime_ids,
        )
        records = await run_in_threadpool(runtime_service.list)
        owners = {r.runtime_id: r.owner_user_id for r in records}
        return {
            "runtimes": [
                {
                    "runtime_id": runtime_id,
                    "owner_user_id": owners.get(runtime_id),
                }
                for runtime_id in sorted(ids)
            ],
        }

    @app.get("/api/hub/admin/runtimes/{runtime_id}/logs")
    async def admin_runtime_logs(
        runtime_id: str,
        _admin: HubUser = Depends(require_admin),
        snapshots: int = Query(default=1, ge=1, le=48),
    ) -> dict[str, object]:
        """Retained log tail snapshots for one runtime (F7).

        Newer snapshots come first; ``content`` is the raw tail text
        captured at ``captured_at``. Browsing is per-runtime — the hub
        deliberately keeps a bounded tail window, not a log pipeline.
        """
        rows = await run_in_threadpool(
            app.state.runtime_log_store.latest,
            runtime_id,
            snapshots=snapshots,
        )
        if not rows and not any(
            r.runtime_id == runtime_id
            for r in await run_in_threadpool(runtime_service.list)
        ):
            raise HTTPException(
                status_code=404,
                detail={"code": "RUNTIME_NOT_FOUND"},
            )
        return {"runtime_id": runtime_id, "snapshots": rows}

    @app.get("/api/hub/admin/usage/summary")
    async def usage_summary(
        _: HubUser = Depends(require_admin),
        start_date: str | None = Query(default=None),
        end_date: str | None = Query(default=None),
        tenant_id: str | None = Query(default=None),
        model: str | None = Query(default=None),
    ) -> dict[str, object]:
        """Aggregated LLM usage by user / model / date (EP-1-4)."""
        return await run_in_threadpool(
            usage_store.summary,
            start_date=start_date,
            end_date=end_date,
            tenant_id=tenant_id,
            model=model,
        )

    @app.post("/api/hub/admin/usage/collect")
    async def usage_collect(
        _: HubUser = Depends(require_admin),
    ) -> dict[str, object]:
        """Trigger one collection pass immediately (admin/debug)."""
        collected = await usage_collector.collect_once()
        return {
            "collected_rows": collected,
            "last_pass_at": usage_collector.last_pass_at,
            "last_error": usage_collector.last_error,
        }

    @app.get(
        "/api/hub/oauth/callback/{runtime_id}/{callback_route:path}",
        include_in_schema=False,
    )
    async def oauth_callback_relay(
        runtime_id: str,
        callback_route: str,
        request: Request,
    ) -> Response:
        callback_path = runtime_oauth_callback_path(callback_route)
        if callback_path is None:
            raise HTTPException(
                status_code=404,
                detail="OAuth callback route is invalid",
            )
        try:
            record = await run_in_threadpool(
                runtime_service.status,
                runtime_id,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="OAuth callback runtime is unavailable",
            ) from exc
        if record.state != RuntimeState.RUNNING:
            raise HTTPException(
                status_code=503,
                detail="OAuth callback runtime is not running",
            )
        target = runtime_url(
            record,
            scheme="http",
            path=callback_path,
            query=request.url.query.encode("utf-8"),
        )
        internal_token = await run_in_threadpool(
            credential_vault.get_runtime_secret,
            tenant_id=record.tenant_id,
            runtime_id=record.runtime_id,
            name="QWENPAW_RUNTIME_INTERNAL_TOKEN",
        )
        if internal_token is None:
            raise HTTPException(
                status_code=503,
                detail="Personal runtime boundary token is unavailable",
            )
        try:
            async with httpx.AsyncClient(
                transport=proxy_transport,
            ) as client:
                upstream = await client.get(
                    target,
                    headers={
                        "X-QwenPaw-Runtime-Token": internal_token,
                    },
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Personal QwenPaw is unavailable: {exc}",
            ) from exc
        excluded_headers = {
            "connection",
            "content-length",
            "transfer-encoding",
        }
        response_headers = {
            name: value
            for name, value in upstream.headers.items()
            if name.lower() not in excluded_headers
        }
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
        )

    @app.delete("/api/hub/pawapps/sessions", status_code=204)
    async def clear_pawapp_sessions(request: Request) -> Response:
        response = Response(status_code=204)
        clear_sessions(request, response)
        return response

    @app.post("/api/hub/pawapps/{app_id}/session")
    async def prepare_pawapp_session(
        app_id: str,
        request: Request,
        user: HubUser = Depends(require_user),
    ) -> Response:
        validate_app_id(app_id)
        record = await ensure_personal_runtime(user)
        internal_token = await run_in_threadpool(
            credential_vault.get_runtime_secret,
            tenant_id=record.tenant_id,
            runtime_id=record.runtime_id,
            name="QWENPAW_RUNTIME_INTERNAL_TOKEN",
        )
        if not internal_token:
            raise HTTPException(
                status_code=503,
                detail="Runtime boundary token unavailable",
            )
        async with httpx.AsyncClient(
            transport=proxy_transport,
            trust_env=False,
        ) as client:
            upstream = await client.get(
                runtime_url(
                    record,
                    scheme="http",
                    path=f"/api/pawapps/{app_id}",
                ),
                headers={"X-QwenPaw-Runtime-Token": internal_token},
                timeout=10,
            )
        if upstream.status_code != 200:
            raise HTTPException(
                status_code=upstream.status_code,
                detail="PawApp is unavailable",
            )
        if upstream.json().get("id") != app_id:
            raise HTTPException(status_code=502, detail="Invalid PawApp")
        response = JSONResponse({"expires_in": SESSION_SECONDS})
        issue_session(
            hub_auth,
            user,
            record,
            app_id,
            response,
            secure=request.url.scheme == "https",
            prefixes=upstream.json().get("browser_prefixes", []),
        )
        return response

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    # pylint: disable=too-many-branches
    async def personal_runtime_proxy(
        path: str,
        request: Request,
        user: HubUser = Depends(require_personal_runtime_user),
    ) -> Response:
        # EP-2-11 governance bridging: one trace id per proxied request,
        # echoed downstream (runtime audit) and on the response.
        trace_id = trace_id_from_headers(request.headers)
        # upstream #7779: the model gateway owns model-plane routes
        require_model_route(path)
        # upstream #7833: PawApp browser sessions are bound to the
        # runtime they were issued for; other callers pass through.
        record = await ensure_personal_runtime(user)
        require_session_runtime(request, record)
        user_groups = await run_in_threadpool(
            app.state.group_store.group_names_for,
            user.user_id,
        )
        user_policies = await run_in_threadpool(
            app.state.group_store.policies_for,
            user_id=user.user_id,
            groups=user_groups,
            role=user.role,
        )
        decision = app.state.acl.decide(
            user.role,
            request.method,
            request.url.path,
            policies=user_policies,
        )
        if not decision.allowed:
            await record_audit(
                user,
                "acl.denied",
                "api",
                request.url.path,
                {"reason": decision.reason, "method": request.method},
                trace_id=trace_id,
            )
            app.state.metrics.inc(
                "qwenpaw_hub_requests_total",
                role=user.role,
                decision="denied",
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "ACL_DENIED",
                    "message": "This API is restricted to administrators.",
                    "reason": decision.reason,
                },
            )
        # EP-2-3: quota gate — soft threshold warns (audit + response
        # header), hard threshold refuses before any forwarding.
        quota_engine: QuotaEngine = app.state.quota
        quota_snapshot = quota_engine.usage_snapshot(
            user.user_id,
            lambda: _usage_snapshot_for(user.user_id),
        )
        quota_decision = quota_engine.check(user.user_id, quota_snapshot)
        if not quota_decision.allowed:
            await record_audit(
                user,
                "quota.exceeded",
                "api",
                request.url.path,
                {
                    "dimension": quota_decision.dimension,
                    "used": quota_decision.used,
                    "limit": quota_decision.limit,
                    "method": request.method,
                },
                outcome="denied",
                remote_address=(
                    request.client.host if request.client else None
                ),
                quota_dimension=quota_decision.dimension,
                quota_used=quota_decision.used,
                quota_limit=quota_decision.limit,
            )
            app.state.metrics.inc(
                "qwenpaw_hub_requests_total",
                role=user.role,
                decision="denied_quota",
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "QUOTA_EXCEEDED",
                    "message": (
                        "Daily usage quota exceeded; contact your "
                        "administrator for an increase."
                    ),
                    "dimension": quota_decision.dimension,
                    "used": quota_decision.used,
                    "limit": quota_decision.limit,
                },
            )

        # G3: aggregate group quota — the whole group shares one
        # daily ceiling (checked after the per-user gate, cached 30s).
        quota_engine_groups: QuotaEngine = app.state.quota
        for group_name in user_groups:
            group_snapshot = quota_engine_groups.group_snapshot(
                group_name,
                _group_usage_fetcher(group_name),
            )
            group_decision = quota_engine_groups.check_group(
                group_name,
                group_snapshot,
            )
            if not group_decision.allowed:
                await record_audit(
                    user,
                    "quota.group_exceeded",
                    "api",
                    request.url.path,
                    {
                        "group": group_name,
                        "dimension": group_decision.dimension,
                        "used": group_decision.used,
                        "limit": group_decision.limit,
                        "method": request.method,
                    },
                    outcome="denied",
                    remote_address=(
                        request.client.host if request.client else None
                    ),
                    quota_dimension=group_decision.dimension,
                    quota_used=group_decision.used,
                    quota_limit=group_decision.limit,
                )
                app.state.metrics.inc(
                    "qwenpaw_hub_requests_total",
                    role=user.role,
                    decision="denied_quota",
                )
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "GROUP_QUOTA_EXCEEDED",
                        "message": (
                            "This group's shared daily quota is "
                            "exhausted; contact your administrator."
                        ),
                        "group": group_name,
                        "dimension": group_decision.dimension,
                        "used": group_decision.used,
                        "limit": group_decision.limit,
                    },
                )

        # E6: rate/concurrency gate; released on every exit path.
        rate_decision = app.state.rate_limiter.check(user.user_id)
        if not rate_decision.allowed:
            app.state.metrics.inc(
                "qwenpaw_hub_rate_limited_total",
                reason=rate_decision.reason,
                role=user.role,
            )
            await record_audit(
                user,
                "ratelimit.exceeded",
                "api",
                request.url.path,
                {
                    "reason": rate_decision.reason,
                    "method": request.method,
                },
                outcome="denied",
                remote_address=(
                    request.client.host if request.client else None
                ),
            )
            headers = {}
            if rate_decision.retry_after:
                headers["Retry-After"] = str(rate_decision.retry_after)
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "RATE_LIMITED",
                    "message": "Too many requests; slow down.",
                    "reason": rate_decision.reason,
                },
                headers=headers,
            )
        try:
            # EP-2-12: approval resolutions are buffered so the hub can
            # mirror them into its audit ledger after the upstream answers.
            approval_body: bytes | None = None
            if request.method == "POST" and re.match(
                r"^/api/approval/(?:approve|deny)$",
                request.url.path,
            ):
                approval_body = await request.body()
            # EP-1-3 governance refinement: switching the active model is
            # usage, not configuration (validated against the catalog).
            try:
                activation_body = await _enforce_model_activation_catalog(
                    app,
                    user,
                    request,
                )
            except ModelNotInCatalogError:
                await record_audit(
                    user,
                    "model.switch_denied",
                    "model",
                    request.url.path,
                    {"role": user.role},
                    trace_id=trace_id,
                )
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "MODEL_NOT_IN_CATALOG",
                        "message": (
                            "Only models from the administrator "
                            "catalog can be activated."
                        ),
                    },
                ) from None
            target = runtime_url(
                record,
                scheme="http",
                path=f"/api/{path}",
                query=request.url.query.encode("utf-8"),
            )
            proxy_config = app.state.hub_config.control_plane.proxy
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = 0
                if declared_size > proxy_config.max_request_size_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Request body exceeds the configured "
                            f"{proxy_config.max_request_size_mb} MiB limit"
                        ),
                    )
            internal_token = await run_in_threadpool(
                credential_vault.get_runtime_secret,
                tenant_id=record.tenant_id,
                runtime_id=record.runtime_id,
                name="QWENPAW_RUNTIME_INTERNAL_TOKEN",
            )
            if internal_token is None:
                raise HTTPException(
                    status_code=503,
                    detail="Personal runtime boundary token is unavailable",
                )

            excluded_request_headers = {
                "authorization",
                "cookie",
                PAWAPP_SCOPE_HEADER.lower(),
                "connection",
                "content-length",
                "host",
                HUB_OAUTH_CALLBACK_URL_HEADER.lower(),
                # re-minted below in canonical casing; skipping the inbound
                # copy avoids a duplicated comma-joined header value
                TRACE_HEADER.lower(),
            }
            headers = {
                name: value
                for name, value in request.headers.items()
                if name.lower() not in excluded_request_headers
            }
            headers["X-QwenPaw-Runtime-Token"] = internal_token
            session = getattr(request.state, "pawapp_session", None)
            if session is not None:
                headers[PAWAPP_SCOPE_HEADER] = quote(
                    str(session["app"]),
                    safe="",
                )
            headers[TRACE_HEADER] = trace_id
            # F5: W3C tracecontext passthrough — drop malformed
            # inbound values, forward only well-formed traceparent
            # so the runtime's OTel SDK (when present) joins spans
            inbound_traceparent = traceparent_from_headers(
                request.headers,
            )
            if inbound_traceparent:
                headers[TRACEPARENT_HEADER] = inbound_traceparent
            else:
                headers.pop(TRACEPARENT_HEADER, None)
            callback_route = oauth_callback_route(request.method, path)
            if callback_route:
                public_base_url = (
                    app.state.hub_config.control_plane.public_base_url
                    or str(request.base_url).rstrip("/")
                )
                headers[HUB_OAUTH_CALLBACK_URL_HEADER] = (
                    f"{public_base_url}/api/hub/oauth/callback/"
                    f"{record.runtime_id}/{callback_route}"
                )
            timeout = httpx.Timeout(
                connect=proxy_config.connect_timeout_seconds,
                read=None,
                write=proxy_config.request_idle_timeout_seconds,
                pool=proxy_config.connect_timeout_seconds,
            )
            client = httpx.AsyncClient(
                timeout=timeout,
                transport=proxy_transport,
            )
            request_complete = asyncio.Event()
            try:
                request_content = (
                    activation_body
                    if activation_body is not None
                    else approval_body
                    if approval_body is not None
                    else limited_request_stream(
                        request.stream(),
                        max_bytes=proxy_config.max_request_size_bytes,
                        idle_timeout_seconds=(
                            proxy_config.request_idle_timeout_seconds
                        ),
                        completion_event=request_complete,
                    )
                )
                upstream_request = client.build_request(
                    request.method,
                    target,
                    headers=headers,
                    content=request_content,
                )
                upstream = await send_with_response_header_timeout(
                    client,
                    upstream_request,
                    request_complete=request_complete,
                    timeout_seconds=(
                        proxy_config.response_header_timeout_seconds
                    ),
                )
            except ClientDisconnect:
                await client.aclose()
                # The caller is gone; end the proxy without an ASGI error.
                return Response(status_code=499)
            except ProxyRequestTooLargeError as exc:
                await client.aclose()
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Request body exceeds the configured "
                        f"{proxy_config.max_request_size_mb} MiB limit"
                    ),
                ) from exc
            except ProxyRequestIdleTimeoutError as exc:
                await client.aclose()
                raise HTTPException(
                    status_code=408,
                    detail="Request body upload timed out",
                ) from exc
            except TimeoutError as exc:
                await client.aclose()
                raise HTTPException(
                    status_code=504,
                    detail="Personal runtime response headers timed out",
                ) from exc
            except httpx.TimeoutException as exc:
                await client.aclose()
                raise HTTPException(
                    status_code=504,
                    detail="Personal runtime proxy request timed out",
                ) from exc
            except httpx.HTTPError as exc:
                await client.aclose()
                raise HTTPException(
                    status_code=502,
                    detail=f"Personal QwenPaw is unavailable: {exc}",
                ) from exc
            except BaseException:
                await client.aclose()
                raise

            # EP-2-12: mirror the approval resolution into the hub audit
            # ledger (who answered which request, with which outcome).
            if approval_body is not None:
                decision_action = (
                    "approve"
                    if request.url.path.endswith(
                        "/approve",
                    )
                    else "deny"
                )
                try:
                    parsed_body = json.loads(approval_body)
                except ValueError:
                    parsed_body = {}
                await record_audit(
                    user,
                    "approval.resolved",
                    "approval",
                    str(parsed_body.get("request_id") or request.url.path),
                    {
                        "action": decision_action,
                        "session_id": parsed_body.get("session_id"),
                        "reason": parsed_body.get("reason"),
                        "upstream_status": upstream.status_code,
                    },
                    trace_id=trace_id,
                )

            # EP-1-3 visibility governance: non-admins only ever see the
            # admin-opened catalog — built-in cloud providers (free tiers
            # included) are hidden server-side, not just in the UI.
            if (
                request.method == "GET"
                and request.url.path == "/api/models"
                and user.role != "admin"
                and upstream.status_code == 200
            ):
                allowed_ids = await run_in_threadpool(
                    _catalog_provider_ids,
                    app.state.catalog_store,
                )
                if upstream.is_stream_consumed:
                    # pre-loaded body (e.g. test transports built with
                    # json=/content=); real network responses stream lazily
                    raw_body = upstream.content
                else:
                    raw_body = await upstream.aread()
                await upstream.aclose()
                await client.aclose()
                return Response(
                    content=_filter_models_payload(raw_body, allowed_ids),
                    status_code=200,
                    media_type="application/json",
                    headers={TRACE_HEADER: trace_id},
                )
            excluded_response_headers = {
                "connection",
                "keep-alive",
                "proxy-authenticate",
                "proxy-authorization",
                "te",
                "trailers",
                "transfer-encoding",
                "upgrade",
            }
            response_headers = {
                name: value
                for name, value in upstream.headers.items()
                if name.lower() not in excluded_response_headers
            }
            response_headers[TRACE_HEADER] = trace_id
            # upstream #7833: personal runtime URLs are reused when the
            # browser changes account.
            response_headers["cache-control"] = "private, no-store"
            app.state.metrics.inc(
                "qwenpaw_hub_requests_total",
                role=user.role,
                decision="allowed",
            )
            if quota_decision.soft_hit:
                # EP-2-3: console banner reads this header (UI copy ticket)
                response_headers["X-QwenPaw-Quota-Warning"] = (
                    f"{quota_decision.dimension} "
                    f"{int(quota_decision.ratio * 100)}%"
                )
                app.state.metrics.inc(
                    "qwenpaw_hub_quota_soft_total",
                    dimension=quota_decision.dimension,
                )

            async def stream_upstream() -> AsyncIterator[bytes]:
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    await upstream.aclose()
                    await client.aclose()

            return StreamingResponse(
                stream_upstream(),
                status_code=upstream.status_code,
                headers=response_headers,
            )

        finally:
            # E6: release the in-flight slot on every exit path.
            app.state.rate_limiter.release(user.user_id)

    @app.websocket("/api/{path:path}")
    async def personal_runtime_websocket_proxy(
        websocket: WebSocket,
        path: str,
    ) -> None:
        authorization = websocket.headers.get("authorization", "")
        prefix = "Bearer "
        token = (
            authorization[len(prefix) :]
            if authorization.startswith(prefix)
            else ""
        )
        if not token:
            await websocket.close(code=4401)
            return
        user = await run_in_threadpool(hub_auth.verify_token, token)
        if user is None:
            await websocket.close(code=4401)
            return
        ws_decision = app.state.acl.decide(
            user.role,
            "WS",
            websocket.url.path,
        )
        if not ws_decision.allowed:
            await record_audit(
                user,
                "acl.denied",
                "api",
                websocket.url.path,
                {"reason": ws_decision.reason, "method": "WS"},
                trace_id=trace_id_from_headers(websocket.headers),
            )
            await websocket.close(code=1008)  # policy violation
            return
        try:
            record = await ensure_personal_runtime(user)
            target = runtime_url(
                record,
                scheme="ws",
                path=f"/api/{path}",
                query=websocket.url.query.encode("utf-8"),
            )
            proxy_config = app.state.hub_config.control_plane.proxy
            internal_token = await run_in_threadpool(
                credential_vault.get_runtime_secret,
                tenant_id=record.tenant_id,
                runtime_id=record.runtime_id,
                name="QWENPAW_RUNTIME_INTERNAL_TOKEN",
            )
            if internal_token is None:
                await websocket.close(code=1013)
                return
            await websocket_proxy.relay_websocket(
                websocket,
                str(target),
                headers={
                    "X-QwenPaw-Runtime-Token": internal_token,
                    TRACE_HEADER: (trace_id_from_headers(websocket.headers)),
                },
                max_size=(proxy_config.websocket_max_message_size_bytes),
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logging.exception("Personal runtime WebSocket proxy failed")
            try:
                await websocket.close(code=1013)
            except RuntimeError:
                return

    static_dir = resolve_console_static_dir()
    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount(
            "/assets",
            CompressedStaticFiles(directory=assets_dir),
            name="assets",
        )

    @app.get("/{path:path}", include_in_schema=False)
    async def hub_console(path: str) -> Response:
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        requested, index_file = await run_in_threadpool(
            resolve_console_response,
            static_dir,
            path,
        )
        if requested is not None:
            return FileResponse(requested)
        if index_file is not None:
            return FileResponse(
                index_file,
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                },
            )
        return JSONResponse(
            {
                "message": (
                    "QwenPaw Hub is running, but Console assets are "
                    "unavailable. "
                    "Run `npm ci && npm run build` in the console directory."
                ),
            },
        )

    return app


def _runtime_payload(
    service: RuntimeService,
    record: Any,
    *,
    owner: HubUser | None,
) -> dict[str, Any]:
    payload = record.to_dict()
    payload["owner_username"] = owner.username if owner else None
    payload["owner_role"] = owner.role if owner else None
    payload["endpoint"] = (
        f"http://{record.host}:{record.port}" if record.port else ""
    )
    payload["security_level"] = service.security_level(record.provisioner)
    # G2: capability projection for operators/consumers
    capability = RuntimeCapability.from_metadata(record.metadata)
    payload["capabilities"] = capability.to_metadata()["capabilities"]
    return payload


def _page_payload(
    items: list[Any],
    page: int,
    page_size: int,
    total: int,
) -> dict[str, object]:
    """Return the shared Hub pagination envelope."""
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


def run_hub_app(
    *,
    host: str,
    port: int,
    log_level: str,
    config_path: Path | None = None,
    force_public: bool = False,
) -> None:
    """Run the QwenPaw Hub control plane with safe public-bind defaults."""
    public_bind = not is_loopback_host(host)
    if public_bind and not force_public:
        raise ValueError(
            "QwenPaw Hub refuses a non-loopback host by default. "
            "Use --force-public after initializing an administrator.",
        )
    root_dir = get_hub_root()
    hub_config = HubConfigStore(
        root_dir / "control.db",
    ).resolve(config_path, available_provisioners={"local", "docker", "k8s"})
    if public_bind:
        database_path = root_dir / "control.db"
        credential_vault = TenantCredentialVault(
            database_path,
            root_dir / "secrets" / ".vault_key",
        )
        hub_auth = HubAuthService(database_path, credential_vault)
        if not hub_auth.has_enabled_admin():
            raise ValueError(
                "Public Hub binding requires an initialized, enabled "
                "administrator. Start on loopback first and create the "
                "administrator account.",
            )
        if not hub_config.control_plane.public_base_url:
            raise ValueError(
                "Public Hub binding requires "
                "control_plane.public_base_url in the Hub config.",
            )
        warning = (
            "QwenPaw Hub is accepting network connections at "
            f"{host}:{port}. --force-public does not provide TLS. "
            "Use a trusted network or a TLS reverse proxy."
        )
        logging.getLogger(__name__).warning("%s", warning)
    uvicorn.run(
        create_hub_app(
            hub_config=hub_config,
            root_dir=root_dir,
            public_bind=public_bind,
        ),
        host=host,
        port=port,
        workers=1,
        log_level=log_level,
        timeout_graceful_shutdown=10,
    )
