# -*- coding: utf-8 -*-
"""Third-party agent catalog and adapter factories."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .base import HarnessAdapter, MissingDependencyAdapter
from .codex.adapter import CodexAdapter
from .events import (
    HarnessApprovalPreset,
    HarnessCapabilities,
    HarnessCommand,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderCatalogItem:
    """Static provider metadata."""

    id: str
    name: str
    coming_soon: bool
    capabilities: HarnessCapabilities


PROVIDER_CATALOG = (
    ProviderCatalogItem(
        "codex",
        "Codex",
        False,
        HarnessCapabilities(
            authentication=True,
            model_selection=True,
            reasoning_effort=True,
            reasoning_stream=True,
            tool_stream=True,
            session_resume=True,
            attachments=True,
            qwenpaw_skills_projection=True,
            qwenpaw_mcp_projection=True,
            provider_skills_discovery=True,
            provider_mcp_discovery=True,
            mcp_tool_allowlist=True,
            commands=[
                HarnessCommand(
                    name="compact",
                    description="Compact the current Codex thread",
                ),
                HarnessCommand(
                    name="review",
                    description="Review uncommitted workspace changes",
                ),
                HarnessCommand(
                    name="skills",
                    description="List skills available to Codex",
                ),
                HarnessCommand(
                    name="status",
                    description="Show Codex account and session status",
                ),
            ],
            approval_presets=[
                HarnessApprovalPreset(
                    id="ask",
                    name="Ask before changes",
                    description=(
                        "Allow workspace changes and ask before elevated "
                        "actions."
                    ),
                    settings={
                        "sandbox": "workspace-write",
                        "approval_policy": "on-request",
                    },
                ),
                HarnessApprovalPreset(
                    id="read-only",
                    name="Read only",
                    description="Inspect files without changing them.",
                    settings={
                        "sandbox": "read-only",
                        "approval_policy": "on-request",
                    },
                ),
                HarnessApprovalPreset(
                    id="workspace",
                    name="Workspace access",
                    description=(
                        "Allow workspace changes without confirmation."
                    ),
                    settings={
                        "sandbox": "workspace-write",
                        "approval_policy": "never",
                    },
                ),
                HarnessApprovalPreset(
                    id="full-access",
                    name="Full access",
                    description=(
                        "Allow unrestricted local execution without "
                        "confirmation."
                    ),
                    settings={
                        "sandbox": "danger-full-access",
                        "approval_policy": "never",
                    },
                ),
            ],
        ),
    ),
    ProviderCatalogItem(
        "claude",
        "Claude Code",
        True,
        HarnessCapabilities(),
    ),
    ProviderCatalogItem(
        "qoder",
        "Qoder",
        False,
        HarnessCapabilities(
            authentication=True,
            model_selection=True,
            reasoning_effort=True,
            reasoning_stream=True,
            tool_stream=True,
            session_resume=True,
            attachments=True,
            qwenpaw_skills_projection=True,
            qwenpaw_mcp_projection=True,
            provider_skills_discovery=True,
            provider_mcp_discovery=False,
            mcp_tool_allowlist=True,
            commands=[
                HarnessCommand(
                    name="compact",
                    description="Compact the current Qoder session",
                ),
            ],
            approval_presets=[
                HarnessApprovalPreset(
                    id="ask",
                    name="Ask before actions",
                    description=(
                        "Ask before file changes and command execution."
                    ),
                    settings={"permission_mode": "default"},
                ),
                HarnessApprovalPreset(
                    id="accept-edits",
                    name="Accept edits",
                    description=(
                        "Allow file edits while keeping other safeguards."
                    ),
                    settings={"permission_mode": "acceptEdits"},
                ),
                HarnessApprovalPreset(
                    id="plan",
                    name="Plan only",
                    description="Analyze and plan without changing files.",
                    settings={"permission_mode": "plan"},
                ),
                HarnessApprovalPreset(
                    id="auto",
                    name="Automatic",
                    description=(
                        "Let Qoder decide which safe actions can run."
                    ),
                    settings={"permission_mode": "auto"},
                ),
                HarnessApprovalPreset(
                    id="full-access",
                    name="Full access",
                    description=(
                        "Skip permission checks in a trusted workspace."
                    ),
                    settings={"permission_mode": "bypassPermissions"},
                ),
            ],
        ),
    ),
)


# ---------------------------------------------------------------------------
# EP-2-16: plugin-registered harness providers.
#
# Third-party code can add agent harnesses without forking QwenPaw:
# a plugin calls ``PluginApi.register_harness_provider`` (plugins/api.py),
# which lands a catalog item plus an adapter factory here. Builtin ids
# are reserved; plugins may re-register their own id (hot reload).
# ---------------------------------------------------------------------------


@dataclass
class PluginHarnessRegistration:
    """One dynamically registered harness provider."""

    item: ProviderCatalogItem
    factory: Callable[[Path, dict[str, Any]], HarnessAdapter]
    config_fields: tuple[str, ...] = ("binary", "env", "args")


_plugin_providers: dict[str, PluginHarnessRegistration] = {}
_plugin_lock = threading.Lock()

_BUILTIN_IDS = frozenset(item.id for item in PROVIDER_CATALOG)


def register_plugin_provider(
    item: ProviderCatalogItem,
    factory: Callable[[Path, dict[str, Any]], HarnessAdapter],
    config_fields: tuple[str, ...] = ("binary", "env", "args"),
) -> None:
    """Register (or replace) one plugin-provided harness provider."""
    if item.id in _BUILTIN_IDS:
        raise ValueError(
            f"Harness provider id '{item.id}' is reserved by a builtin "
            "provider",
        )
    with _plugin_lock:
        _plugin_providers[item.id] = PluginHarnessRegistration(
            item=item,
            factory=factory,
            config_fields=tuple(config_fields),
        )
    logger.info(
        "Plugin harness provider registered: %s (%s)",
        item.id,
        item.name,
    )


def unregister_plugin_provider(provider_id: str) -> None:
    """Drop one plugin-provided harness provider (plugin uninstall)."""
    with _plugin_lock:
        _plugin_providers.pop(provider_id, None)


def list_provider_items() -> tuple[ProviderCatalogItem, ...]:
    """Builtin catalog followed by plugin registrations."""
    with _plugin_lock:
        plugin_items = tuple(reg.item for reg in _plugin_providers.values())
    return PROVIDER_CATALOG + plugin_items


def get_provider(provider_id: str) -> ProviderCatalogItem:
    """Return catalog metadata for one backend."""
    for item in list_provider_items():
        if item.id == provider_id:
            return item
    raise ValueError(f"Unknown third-party agent backend: {provider_id}")


def adapter_config_key(
    provider_id: str,
    settings: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    """Return settings that require recreating one provider adapter."""
    values = settings or {}
    if provider_id in {"codex", "qoder"}:
        return (str(values.get("binary") or "").strip(),)
    with _plugin_lock:
        registration = _plugin_providers.get(provider_id)
    if registration is not None:
        return tuple(
            _hashable(values.get(name)) for name in registration.config_fields
        )
    return ()


def _hashable(value: Any) -> Any:
    """Make JSON-ish settings values tuple-comparable."""
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(v) for v in value)
    return value


def create_adapter(
    provider_id: str,
    state_dir: Path,
    settings: dict[str, Any] | None = None,
) -> HarnessAdapter:
    """Create one supported provider adapter."""
    if provider_id == "codex":
        binary = str((settings or {}).get("binary") or "").strip() or None
        return CodexAdapter(state_dir=state_dir, binary=binary)
    if provider_id == "qoder":
        binary = str((settings or {}).get("binary") or "").strip() or None
        try:
            from .qoder.adapter import QoderAdapter
        except ModuleNotFoundError as exc:
            if exc.name != "qoder_agent_sdk":
                raise
            return MissingDependencyAdapter("qoder", "Qoder")
        return QoderAdapter(state_dir=state_dir, binary=binary)
    with _plugin_lock:
        registration = _plugin_providers.get(provider_id)
    if registration is not None:
        return registration.factory(
            state_dir,
            dict(settings or {}),
        )
    raise ValueError(f"Unsupported third-party agent backend: {provider_id}")


__all__ = [
    "PROVIDER_CATALOG",
    "PluginHarnessRegistration",
    "adapter_config_key",
    "create_adapter",
    "get_provider",
    "list_provider_items",
    "register_plugin_provider",
    "unregister_plugin_provider",
]
