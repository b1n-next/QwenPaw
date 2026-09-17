# -*- coding: utf-8 -*-
"""Runtime↔hub capability negotiation (G2, 02 §7).

A runtime reports its capability set when registering; the hub
holds administrator-set requirements; ``requirement ⊆ capability``
gates scheduling (runtime start). The sandbox shape deliberately
reuses ``SandboxCapability``'s fields so probes flow through
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class RuntimeCapability:
    """What one runtime advertises (registration payload)."""

    version: str = "0.0.0"
    sandbox_supported: bool = False
    sandbox_mode: str = "none"
    tools: tuple[str, ...] = ()
    extras: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_metadata(cls, metadata: Dict[str, Any]) -> "RuntimeCapability":
        """Parse capabilities from a runtime's metadata dict."""
        payload = metadata.get("capabilities") or {}
        if not isinstance(payload, dict):
            payload = {}
        sandbox = payload.get("sandbox") or {}
        if not isinstance(sandbox, dict):
            sandbox = {}
        tools = payload.get("tools") or []
        if not isinstance(tools, list):
            tools = []
        return cls(
            version=str(payload.get("version") or "0.0.0"),
            sandbox_supported=bool(sandbox.get("supported")),
            sandbox_mode=str(sandbox.get("mode") or "none"),
            tools=tuple(str(item) for item in tools),
        )

    def to_metadata(self) -> Dict[str, Any]:
        """Serialize back into runtime metadata."""
        return {
            "capabilities": {
                "version": self.version,
                "sandbox": {
                    "supported": self.sandbox_supported,
                    "mode": self.sandbox_mode,
                },
                "tools": list(self.tools),
            },
        }


@dataclass(frozen=True)
class CapabilityRequirement:
    """What the hub demands before scheduling a runtime (G2)."""

    min_version: str = "0.0.0"
    sandbox_required: bool = False
    tools_required: tuple[str, ...] = ()

    @classmethod
    def from_document(cls, document: Any) -> "CapabilityRequirement":
        """Parse the stored requirement extension document."""
        if not document:
            return cls()
        value = (
            document.get("value", document)
            if isinstance(document, dict)
            else {}
        )
        if not isinstance(value, dict):
            value = {}
        tools = value.get("tools_required") or []
        if not isinstance(tools, list):
            tools = []
        try:
            sandbox_required = bool(value.get("sandbox_required", False))
        except (TypeError, ValueError):
            sandbox_required = False
        return cls(
            min_version=str(value.get("min_version") or "0.0.0"),
            sandbox_required=sandbox_required,
            tools_required=tuple(str(item) for item in tools),
        )


def _version_tuple(raw: str) -> tuple[int, ...]:
    """Best-effort dotted-version ordering ('2.10' > '2.9')."""
    parts: List[int] = []
    for chunk in str(raw).split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


@dataclass(frozen=True)
class NegotiationResult:
    """Outcome of requirement ⊆ capability."""

    ok: bool
    missing: tuple[str, ...] = ()

    @property
    def detail(self) -> dict[str, Any]:
        return {"ok": self.ok, "missing": list(self.missing)}


def negotiate(
    capability: RuntimeCapability,
    requirement: CapabilityRequirement,
) -> NegotiationResult:
    """Check ``requirement ⊆ capability`` for scheduling (G2)."""
    missing: List[str] = []
    if _version_tuple(capability.version) < _version_tuple(
        requirement.min_version,
    ):
        missing.append(
            f"version>={requirement.min_version} "
            f"(have {capability.version})",
        )
    if requirement.sandbox_required and not capability.sandbox_supported:
        missing.append("sandbox")
    have = set(capability.tools)
    for tool in requirement.tools_required:
        if tool not in have:
            missing.append(f"tool:{tool}")
    return NegotiationResult(
        ok=not missing,
        missing=tuple(missing),
    )


__all__ = [
    "CapabilityRequirement",
    "NegotiationResult",
    "RuntimeCapability",
    "negotiate",
]
