# -*- coding: utf-8 -*-
"""Tests for the hub proxy ACL engine (enterprise layer, Phase 0).

Covers the EP-0-1 grouping decisions, fail-closed defaults, path
normalization (traversal defense), and the acl.json overlay.
"""

import json
import time
from pathlib import Path

import pytest

from qwenpaw.hub.acl import AclEngine, permissions_payload


def _user(engine: AclEngine, method: str, path: str) -> bool:
    return engine.decide("user", method, path).allowed


# ── role semantics ──────────────────────────────────────────────────────────


def test_admin_bypasses_acl() -> None:
    engine = AclEngine()
    assert engine.decide("admin", "GET", "/api/config/channels").allowed


def test_unknown_role_evaluates_as_user_rules() -> None:
    engine = AclEngine()
    # Rules are role-agnostic below admin: unknown roles keep the chat
    # plane but never the admin plane.
    assert engine.decide("ghost", "GET", "/api/console/push-messages").allowed
    assert not engine.decide("ghost", "GET", "/api/config").allowed


# ── chat plane ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/console/chat"),
        ("POST", "/api/console/chat/stop"),
        ("POST", "/api/console/upload"),
        ("GET", "/api/console/push-messages"),
        ("GET", "/api/console/inbox/events"),
        ("WS", "/api/console/inbox/events"),
    ],
)
def test_chat_plane_allowed(method: str, path: str) -> None:
    assert _user(AclEngine(), method, path)


def test_console_debug_denied() -> None:
    assert not _user(AclEngine(), "GET", "/api/console/debug/backend-logs")


# ── agents: reads + app-scoped chat ────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/agents"),
        ("GET", "/api/agents/main"),
        ("GET", "/api/agents/memory/backends"),
        ("GET", "/api/agent-status"),
        # PawApp scoped chat plane (agent_scoped composite router)
        ("POST", "/api/agents/qwenpaw-data/console/chat"),
        ("GET", "/api/agents/qa-data/chats"),
        ("GET", "/api/agents/qa-data/agent-status"),
    ],
)
def test_agent_reads_and_scoped_chat_allowed(
    method: str,
    path: str,
) -> None:
    assert _user(AclEngine(), method, path)


@pytest.mark.parametrize(
    "method,path",
    [
        ("PUT", "/api/agents/order"),
        ("PATCH", "/api/agents/main/pin"),
        ("PATCH", "/api/agents/main/backend-settings"),
        ("DELETE", "/api/agents/main"),
        # scoped admin subtree, even read-only
        ("GET", "/api/agents/main/workspace/files"),
        ("GET", "/api/agents/main/config"),
        ("GET", "/api/agents/main/plugins"),
        ("GET", "/api/agents/main/mcp/tools"),
        ("POST", "/api/agents/main/cron/jobs"),
    ],
)
def test_agent_admin_surface_denied(method: str, path: str) -> None:
    assert not _user(AclEngine(), method, path)


# ── per-user preferences ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/settings/language"),
        ("PUT", "/api/settings/language"),
        ("GET", "/api/settings/upload-limit"),
    ],
)
def test_personal_settings_allowed(method: str, path: str) -> None:
    assert _user(AclEngine(), method, path)


def test_offload_policy_is_admin_plane() -> None:
    assert not _user(AclEngine(), "GET", "/api/settings/offload-policy")


# ── admin plane defaults (fail-closed) ─────────────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/config/channels"),
        ("POST", "/api/plugins/install"),
        ("GET", "/api/files/preview/agent.md"),
        ("GET", "/api/workspace/files"),
        ("POST", "/api/messages/send"),
        ("GET", "/api/portability/imports/sources"),  # pawport import
        ("POST", "/api/portability/imports/jobs"),
        ("GET", "/api/models"),  # providers
        ("WS", "/api/voice/ws"),
        ("GET", "/api/envs"),
        ("GET", "/api/backups/jobs/active"),
        ("POST", "/api/skills/ai/optimize/stream"),
        ("GET", "/api/agent-stats"),
        ("DELETE", "/api/pawapps/qa-data"),
        ("GET", "/api/unknown-router"),
    ],
)
def test_admin_plane_denied(method: str, path: str) -> None:
    assert not _user(AclEngine(), method, path)


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/pawapps"),
        ("GET", "/api/pawapps/qa-data/settings"),
        ("GET", "/api/pawapps/qa-data/static/ui/index.html"),
        ("GET", "/api/market/providers"),
        ("POST", "/api/market/search"),
        ("GET", "/api/frontend_plugin/qwenpaw-data/files/x.html"),
        ("GET", "/api/approval/list"),
        ("POST", "/api/approval/approve"),
        ("GET", "/api/token-usage"),
        ("GET", "/api/token-usage/details"),
        ("GET", "/api/tool-calls/session-1"),
        ("POST", "/api/tool-calls/session-1/tc-1/cancel"),
        ("GET", "/api/healthz"),
    ],
)
def test_user_plane_allowed(method: str, path: str) -> None:
    assert _user(AclEngine(), method, path)


# ── normalization / traversal ──────────────────────────────────────────────


def test_dot_segment_traversal_resolved_before_matching() -> None:
    engine = AclEngine()
    # /api/config/../console/chat normalizes onto the allowed chat path.
    assert _user(engine, "POST", "/api/config/../console/chat")
    # /api/console/../config normalizes onto a denied path.
    assert not _user(engine, "GET", "/api/console/../config")


def test_encoded_traversal_is_decoded() -> None:
    engine = AclEngine()
    assert not _user(engine, "GET", "/api/console/%2e%2e/config")


def test_root_escape_and_malformed_paths_denied() -> None:
    engine = AclEngine()
    assert not _user(engine, "GET", "/api/../../etc/passwd")
    assert not _user(engine, "GET", "/api/con\x00sole/chat")
    assert not _user(engine, "GET", "/api/console/chat" + "x" * 3000)


def test_duplicate_slashes_collapsed() -> None:
    engine = AclEngine()
    assert _user(engine, "POST", "/api//console///chat")
    assert not _user(engine, "GET", "/api//config//channels")


# ── decision payload ───────────────────────────────────────────────────────


def test_decision_carries_stable_reason() -> None:
    engine = AclEngine()
    denied = engine.decide("user", "GET", "/api/config")
    assert not denied.allowed
    assert denied.reason == "default-deny"
    allowed = engine.decide("user", "POST", "/api/console/chat")
    assert allowed.reason == "console.chat"


# ── acl.json overlay ───────────────────────────────────────────────────────


def test_overlay_deny_shadows_default_allow(tmp_path: Path) -> None:
    config = tmp_path / "acl.json"
    config.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "no-market",
                        "effect": "deny",
                        "pattern": "^/api/market(?:/|$)",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    engine = AclEngine(config_path=config)
    assert not _user(engine, "GET", "/api/market/providers")
    # untouched defaults keep working
    assert _user(engine, "POST", "/api/console/chat")


def test_overlay_allow_opens_admin_plane(tmp_path: Path) -> None:
    config = tmp_path / "acl.json"
    config.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "harnesses-readonly",
                        "effect": "allow",
                        "pattern": "^/api/harnesses",
                        "methods": ["GET"],
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    engine = AclEngine(config_path=config)
    assert _user(engine, "GET", "/api/harnesses")
    assert not _user(engine, "POST", "/api/harnesses/x/login")


def test_broken_overlay_falls_back_to_defaults(tmp_path: Path) -> None:
    config = tmp_path / "acl.json"
    config.write_text("{not json", encoding="utf-8")
    engine = AclEngine(config_path=config)
    assert _user(engine, "POST", "/api/console/chat")
    assert not _user(engine, "GET", "/api/config")


def test_overlay_hot_reload(tmp_path: Path) -> None:
    config = tmp_path / "acl.json"
    config.write_text("{}", encoding="utf-8")
    engine = AclEngine(config_path=config)
    assert _user(engine, "GET", "/api/market/providers")

    config.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "no-market",
                        "effect": "deny",
                        "pattern": "^/api/market",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    # mtime_ns granularity: nudge explicitly to be robust on coarse clocks.
    future = time.time() + 5
    import os

    os.utime(config, (future, future))
    assert not _user(engine, "GET", "/api/market/providers")


# ── console menu payload ───────────────────────────────────────────────────


def test_permissions_payload_user_vs_admin() -> None:
    user_payload = permissions_payload("user")
    assert user_payload["role"] == "user"
    assert "core.import" in user_payload["denied_routes"]
    assert "settings" in user_payload["denied_groups"]

    admin_payload = permissions_payload("admin")
    assert admin_payload["denied_routes"] == []
    assert admin_payload["denied_groups"] == []
