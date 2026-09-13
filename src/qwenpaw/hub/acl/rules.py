# -*- coding: utf-8 -*-
"""Ordered ACL rule table for hub proxy enforcement (EP-0-1/EP-0-2).

Semantics
---------
- Rules are evaluated **in order** for non-admin roles; the first match wins.
- ``methods`` limits a rule to specific HTTP methods; ``None`` matches all
  (including the ``WS`` pseudo-method used for websocket upgrades).
- No rule matched → **deny** (fail-closed). The ``admin`` role bypasses ACL.
- Paths are normalized (percent-decoded, dot-segments resolved, duplicate
  slashes collapsed) before matching; malformed paths are denied.

Grouping rationale (chat plane vs admin plane) is documented in
``docs/enterprise/03-design-console-permission.md`` §3 appendix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import FrozenSet, Optional

_WS = "WS"
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Agent-scoped admin subtrees mounted by routers/agent_scoped.py under
# /api/agents/{agentId}/...  Chat-related subrouters (console, chats,
# agent-status) are intentionally absent: they are the PawApp chat plane.
_AGENT_SCOPED_ADMIN = (
    r"^/api/agents/[^/]+/"
    r"(?:workspace|config|plugins|checkpoints|git|mcp|mcp_oauth|"
    r"skills|tools|cron|project-directory|portability)"
    r"(?:/|$)"
)


@dataclass(frozen=True)
class RuleSpec:
    """One ordered ACL rule."""

    effect: str  # "allow" | "deny"
    pattern: str  # regex matched against the normalized full path
    methods: Optional[FrozenSet[str]] = None  # None = any method incl. WS
    name: str = ""  # stable id used in audit events

    def matches(self, method: str, path: str) -> bool:
        if self.methods is not None and method not in self.methods:
            return False
        return re.match(self.pattern, path) is not None


def _deny(
    pattern: str,
    name: str,
    methods: Optional[FrozenSet[str]] = None,
) -> RuleSpec:
    return RuleSpec("deny", pattern, methods, name)


def _allow(
    pattern: str,
    name: str,
    methods: Optional[FrozenSet[str]] = None,
) -> RuleSpec:
    return RuleSpec("allow", pattern, methods, name)


#: Ordered defaults for the ``user`` role. Append-only mindset: inserting a
#: new rule requires re-reading every earlier rule for shadowing.
DEFAULT_RULES: tuple[RuleSpec, ...] = (
    # 1. Agent-scoped admin subtrees (workspace/config/plugins/...) — deny
    #    even for GET, before any broad agents allow below.
    _deny(_AGENT_SCOPED_ADMIN, "agents.scoped-admin"),
    # 2. Runtime debug surface leaks backend internals.
    _deny(r"^/api/console/debug(?:/|$)", "console.debug"),
    # 3. Agent-scoped chat plane (PawApp sessions live under
    #    /api/agents/{agentId}/console|chats|chat|agent-status) — must
    #    precede the top-level agents *write* deny below.
    _allow(
        r"^/api/agents/[^/]+/(?:console|chats|chat|agent-status)(?:/|$)",
        "agents.scoped-chat",
    ),
    # 4. Top-level agent *writes* (create/order/pin/backend-settings).
    _deny(r"^/api/agents(?:/|$)", "agents.write", _WRITE_METHODS),
    # 5. App lifecycle writes (DELETE pawapp) — GETs allowed below.
    _deny(r"^/api/pawapps(?:/|$)", "pawapps.write", _WRITE_METHODS),
    # 6. Plain health/version/auth (hub intercepts most of these before
    #    the proxy; kept for direct-runtime parity).
    _allow(r"^/api/healthz$", "healthz"),
    _allow(r"^/api/version$", "version"),
    _allow(r"^/api/auth(?:/|$)", "auth"),
    # 7. Chat plane: main chat, upload, push messages, inbox events.
    _allow(r"^/api/console(?:/|$)", "console.chat"),
    # 8. Agent reads: list/detail/memory-backends (scoped chat allowed in #3).
    _allow(r"^/api/agents(?:/|$)", "agents.read"),
    _allow(r"^/api/agent-status(?:/|$)", "agent-status.read"),
    # 9. Approval inbox is a per-user chat-adjacent flow.
    _allow(r"^/api/approval(?:/|$)", "approval"),
    # 10. Personal usage stats (read-only).
    _allow(
        r"^/api/token-usage(?:/|$)",
        "token-usage.read",
        frozenset({"GET"}),
    ),
    # 11. In-chat tool call inspection and user-triggered cancels.
    _allow(r"^/api/tool-calls(?:/|$)", "tool-calls"),
    # 12. Installed apps: list/detail/settings/static (writes denied in #5).
    _allow(r"^/api/pawapps(?:/|$)", "pawapps.read"),
    # 13. Marketplace browsing + search (install goes through /api/plugins,
    #     which stays admin-plane and is therefore denied by default).
    _allow(r"^/api/market(?:/|$)", "market.browse"),
    # 14. Plugin static assets are public at the runtime anyway.
    _allow(r"^/api/frontend_plugin(?:/|$)", "frontend_plugin"),
    # 15. Per-user console preferences that the runtime already exposes
    #     publicly (see app/auth.py _PUBLIC_PATHS).
    _allow(
        r"^/api/settings/(?:language|upload-limit)$",
        "settings.personal",
    ),
    # 16. Read-only model catalog (EP-1-3): the chat composer lists
    #     models via GET /api/models; provider writes stay admin-plane
    #     and are denied by the fail-closed default.
    _allow(
        r"^/api/models(?:/|$)",
        "models.read",
        frozenset({"GET"}),
    ),
    # 16b. Switching the active model is usage, not configuration:
    # members may activate any model that exists in their runtime
    # (the hub proxy validates non-admin activations against the
    # admin-maintained catalog before forwarding).
    _allow(
        r"^/api/models/active$",
        "models.activate",
        frozenset({"PUT"}),
    ),
    # 17. Chat session list/CRUD is the user's own data plane (the
    #     console polls GET /api/chats for the sidebar history; create/
    #     rename/delete/batch actions target the user's own sessions,
    #     enforced by the runtime's per-user scoping).
    _allow(r"^/api/chats(?:/|$)", "chats"),
    # 18. Coding-mode toggle is the user's own composer state.
    _allow(r"^/api/coding-mode$", "coding-mode"),
    # 19. The whole /api/workspace plane is the user's own runtime
    #     working directory (files workspace page: tree/files/memory/
    #     uploads/system-prompt-files; chat page: project-directory/
    #     running-config/transcription provider). No credentials or
    #     admin configuration live here.
    _allow(r"^/api/workspace(?:/|$)", "workspace.personal"),
    # 18b. Chat message attachment previews are the user's own
    #      generated files.
    _allow(r"^/api/files/preview(?:/|$)", "files.preview", frozenset({"GET"})),
    # 18c. The user's own token usage page.
    _allow(
        r"^/api/token-usage(?:/|$)",
        "token-usage.read",
        frozenset({"GET"}),
    ),
    # 18d. User's own scheduled jobs.
    _allow(r"^/api/cron(?:/|$)", "cron"),
    # 18e. Tool list feeds the chat composer's tool cards.
    _allow(r"^/api/tools(?:/|$)", "tools.read", frozenset({"GET"})),
    # 18f. Harness providers listing (read-only; model/provider
    #      writes stay admin-governed).
    _allow(r"^/api/harnesses(?:/|$)", "harnesses.read", frozenset({"GET"})),
    # 18g. Slash-command check.
    _allow(r"^/api/commands(?:/|$)", "commands"),
    # 18h. Runtime agent liveness for the user's own runtime; the
    #      admin/shutdown subtree stays denied below.
    _deny(r"^/api/agent/(?:admin|shutdown)(?:/|$)", "agent.admin"),
    _allow(r"^/api/agent(?:/|$)", "agent.read", frozenset({"GET"})),
    # 20. Loop modes drive the composer's loop picker (GET list).
    _allow(r"^/api/loops(?:/|$)", "loops", frozenset({"GET"})),
    # 21. Skills are local runtime capabilities: listing, refreshing
    #     and enabling/disabling them is user-plane. The AI-optimizer
    #     subtree burns LLM tokens and stays admin-governed.
    _deny(r"^/api/skills/ai(?:/|$)", "skills.ai.admin"),
    _allow(r"^/api/skills(?:/|$)", "skills"),
    # Everything else (config, envs, providers, files, backups,
    # plugins, loops, harnesses, mcp, skills, tools, voice, messages,
    # portability/imports, local-models, agent-stats, schemas, ...) is
    # denied by the engine's fail-closed default.
)

__all__ = ["DEFAULT_RULES", "RuleSpec", "WS_METHOD"]
WS_METHOD: str = _WS
