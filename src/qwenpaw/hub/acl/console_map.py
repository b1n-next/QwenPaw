# -*- coding: utf-8 -*-
"""Console menu-route mapping served to the browser (EP-0-4).

The hub tells the console which built-in menu ids the current role may not
see. Ids mirror ``console/src/layouts/registry/builtinRoutes.tsx`` and the
group headers from ``builtinMenu.ts``; the console-side filter drops both
the listed routes and any group left without visible children.

This mapping is UX only — the security boundary is the proxy ACL
(``rules.py``). A stale id here can hide or show a menu entry but can
never open an API path the engine denies.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet

#: Menu groups hidden per role (informational for the console UI).
#: Groups stay visible for users — each group still contains at least
#: one user-plane route (cron/files/skills/token-usage); the truly
#: admin-only groups would be left empty and auto-collapse anyway.
_DENIED_GROUPS: Dict[str, FrozenSet[str]] = {
    "user": frozenset(),
}

#: Built-in menu route ids hidden per role.
_DENIED_ROUTES: Dict[str, FrozenSet[str]] = {
    "user": frozenset(
        {
            # control group: channel credentials + session monitoring
            # stay admin-governed; cron-jobs is the user's own plane.
            "core.channels",
            "core.sessions",
            "core.heartbeat",
            # workspace group: files + checkpoints are the user's own
            # working directory; ACP config and stats stay admin.
            "core.workspace",
            "core.acp",
            "core.agent-config",
            "core.agent-stats",
            # agent group: skills/skill-pool/tools are user-plane
            # capabilities; MCP servers carry credentials.
            "core.mcp",
            # settings group: everything except the personal
            # token-usage page.
            "core.settings-center",
            "core.models",
            "core.environments",
            "core.offload-policy",
            "core.security",
            "core.voice-transcription",
            "core.debug",
            "core.backups",
            "core.agents",
            # pawport whole-workspace import (top-level entry)
            "core.import",
        },
    ),
}


def permissions_payload(role: str) -> Dict[str, Any]:
    """Build the ``/api/hub/me/permissions`` response for *role*."""
    groups = _DENIED_GROUPS.get(role, frozenset())
    routes = _DENIED_ROUTES.get(role, frozenset())
    return _payload(role, groups, routes)


def effective_permissions(
    role: str,
    policies: Any = (),
) -> Dict[str, Any]:
    """Policy-aware payload (B5): ``menu:<group>`` and
    ``route:<route-id>`` policies override the static role table.

    Evaluation per resource follows 05 §2: user > group > role
    (the store pre-orders subjects), deny beats allow at equal
    target, and anything without a policy hit falls back to the
    static table. Menu visibility is UX only — the proxy ACL
    stays the boundary.
    """
    groups = set(_DENIED_GROUPS.get(role, frozenset()))
    routes = set(_DENIED_ROUTES.get(role, frozenset()))

    menu_decisions: Dict[str, str] = {}
    route_decisions: Dict[str, str] = {}
    for policy in policies:
        kind, _, value = policy.resource.partition(":")
        if kind == "menu" and value:
            _record_deny_wins(menu_decisions, value, policy.effect)
        elif kind == "route" and value:
            _record_deny_wins(route_decisions, value, policy.effect)

    for target, effect in menu_decisions.items():
        if effect == "deny":
            groups.add(target)
        else:
            groups.discard(target)
    for target, effect in route_decisions.items():
        if effect == "deny":
            routes.add(target)
        else:
            routes.discard(target)
    return _payload(role, frozenset(groups), frozenset(routes))


def _record_deny_wins(
    decisions: Dict[str, str],
    target: str,
    effect: str,
) -> None:
    """Deny beats allow at equal target (mirrors AclEngine, EP-2-1)."""
    if effect == "deny" or target not in decisions:
        decisions[target] = effect


def _payload(
    role: str,
    groups: FrozenSet[str],
    routes: FrozenSet[str],
) -> Dict[str, Any]:
    return {
        "role": role,
        "denied_groups": sorted(groups),
        "denied_routes": sorted(routes),
        # EP-1-3: user role gets a read-only model catalog — models
        # may be switched (usage) but not added or reconfigured; the
        # hub proxy validates activations against the catalog.
        "model_readonly": role != "admin",
    }


__all__ = ["effective_permissions", "permissions_payload"]
