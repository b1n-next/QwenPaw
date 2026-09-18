# -*- coding: utf-8 -*-
"""Per-user/group resource access policies (D2/D3, 02 matrix).

The group policy store (EP-2-1) already carries arbitrary
``name``-keyed entries. This module interprets the resource-shaped
subset — ``skill:<id>``, ``mcp:<id>``, ``channel:<id>``,
``agent_template:<id>`` — into an allow/deny verdict with the same
semantics as the menu (B5) and model (E9) planes:

* no policy of this kind at all  -> everything allowed (fail-open to
  the pre-policy behavior, matching how B5/E9 treat absent rules);
* any policy present             -> allow-list wins, deny beats allow
  within the kind;
* policy names are exact-match ids for now (no globbing) — the store
  already deduplicates per subject order (user > group).
"""

from __future__ import annotations

from typing import Any, Iterable

RESOURCE_KINDS = ("skill", "mcp", "channel", "agent_template")


def _policy_name(policy: Any) -> str:
    """Accept GroupPolicyStore rows (``.resource``) or plain dicts."""
    resource = getattr(policy, "resource", None)
    if resource is None and isinstance(policy, dict):
        resource = policy.get("resource") or policy.get("name")
    return str(resource or "")


def allowed_resource_ids(
    policies: Iterable[dict[str, Any]],
    kind: str,
) -> tuple[bool, set[str]]:
    """Return (allowed_all, ids) for one resource kind.

    ``allowed_all`` is True when no policy mentions the kind — the
    caller should skip filtering entirely. Otherwise ``ids`` is the
    final allow-set (deny already removed).
    """
    if kind not in RESOURCE_KINDS:
        raise ValueError(f"unknown resource kind: {kind}")
    prefix = f"{kind}:"
    allowed: set[str] = set()
    denied: set[str] = set()
    seen_any = False
    for policy in policies:
        name = _policy_name(policy)
        if not name.startswith(prefix):
            continue
        seen_any = True
        resource_id = name[len(prefix) :]
        if not resource_id:
            continue
        effect_value = getattr(policy, "effect", None)
        if effect_value is None and isinstance(policy, dict):
            effect_value = policy.get("effect")
        effect = str(effect_value or "allow").lower()
        if effect == "deny":
            denied.add(resource_id)
        else:
            allowed.add(resource_id)
    if not seen_any:
        return True, set()
    return False, allowed - denied


def resource_baseline(
    policies: Iterable[dict[str, Any]],
    kinds: tuple[str, ...] = (
        "skill",
        "mcp",
        "channel",
    ),
) -> dict[str, Any] | None:
    """Provisioner-facing allow-list payload (None = no filtering).

    Shape (one key per kind, runtime-consumable):

        {"skills": [...], "mcp_servers": [...], "channels": [...]}

    Kinds map to the runtime-side config section names; ``None`` means
    no policy touches any kind, so no baseline env is injected at all.
    """
    payload: dict[str, Any] = {}
    section_names = {
        "skill": "skills",
        "mcp": "mcp_servers",
        "channel": "channels",
        "agent_template": "agent_templates",
    }
    for kind in kinds:
        allowed_all, ids = allowed_resource_ids(policies, kind)
        if allowed_all:
            continue
        payload[section_names[kind]] = sorted(ids)
    return payload or None
