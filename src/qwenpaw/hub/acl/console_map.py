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
    return {
        "role": role,
        "denied_groups": sorted(groups),
        "denied_routes": sorted(routes),
        # EP-1-3: user role gets a read-only model catalog — models
        # may be switched (usage) but not added or reconfigured; the
        # hub proxy validates activations against the catalog.
        "model_readonly": role != "admin",
    }


__all__ = ["permissions_payload"]
