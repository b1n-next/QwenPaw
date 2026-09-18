# -*- coding: utf-8 -*-
"""Runtime-side resource allow-list baseline (D2/D3, 02 matrix).

Reads ``QWENPAW_RESOURCE_BASELINE_JSON`` (injected by the hub per owner
at provision time; see ``qwenpaw.hub.acl.resource_policies``) and keeps
a process-wide allow-set for skills, MCP servers, and channels.

Semantics (mirror of the hub-side computation):
- absent env / absent kind key → that kind is unrestricted (the hub
  only injects kinds an admin policy actually touches);
- present key → only the listed ids are allowed; ``resource_allowed``
  is the single gate helper subsystems consult.

Failures are logged and swallowed with fail-closed semantics *only for
kinds whose payload parsed*: a malformed payload for a kind removes
nothing (hub is source of truth; a corrupted push must not silently
widen access, but must also not brick startup — empty allow-set would
brick, so we treat malformed as "unrestricted" and log loudly, since
the hub recomputes on next restart).

Startup wiring: ``_app.py`` calls ``apply_resource_baseline_from_env``
next to ``apply_model_bootstrap``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable

logger = logging.getLogger(__name__)

BASELINE_ENV = "QWENPAW_RESOURCE_BASELINE_JSON"

# baseline payload key -> runtime gate kind
_KINDS = {
    "skills": "skill",
    "mcp_servers": "mcp",
    "channels": "channel",
    "agent_templates": "agent_template",
}

# kind -> allowed ids (empty dict value = unrestricted)
_allow_sets: dict[str, set[str]] = {}


def parse_resource_baseline(raw: str | None) -> dict[str, set[str]]:
    """Parse the payload into {kind: ids}; unknown keys are ignored.

    Returns {} when nothing applies (unrestricted runtime).
    """
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.error(
            "resource_baseline: malformed %s payload; "
            "running unrestricted this session",
            BASELINE_ENV,
        )
        return {}
    if not isinstance(payload, dict):
        logger.error("resource_baseline: payload is not an object")
        return {}
    parsed: dict[str, set[str]] = {}
    for key, ids in payload.items():
        kind = _KINDS.get(str(key))
        if kind is None:
            continue
        if not isinstance(ids, list) or not all(
            isinstance(item, str) for item in ids
        ):
            logger.error(
                "resource_baseline: key %s must be a list of ids",
                key,
            )
            continue
        parsed[kind] = set(ids)
    return parsed


def apply_resource_baseline(
    parsed: dict[str, set[str]],
) -> None:
    """Replace the process-wide allow-sets (startup + tests)."""
    _allow_sets.clear()
    _allow_sets.update(parsed)


def apply_resource_baseline_from_env(
    environ: dict[str, str] | None = None,
) -> dict[str, set[str]]:
    """Load and apply the baseline from the environment (idempotent)."""
    env = environ if environ is not None else os.environ
    parsed = parse_resource_baseline(env.get(BASELINE_ENV, ""))
    apply_resource_baseline(parsed)
    if parsed:
        logger.info(
            "resource_baseline applied: %s",
            {kind: len(ids) for kind, ids in parsed.items()},
        )
    return parsed


def resource_allowed(kind: str, resource_id: str) -> bool:
    """Single gate helper: True when the kind is unrestricted or the
    id is explicitly allowed."""
    allowed = _allow_sets.get(kind)
    if allowed is None:
        return True
    return resource_id in allowed


def restricted_kinds() -> tuple[str, ...]:
    """Kinds currently under policy (for diagnostics/console badges)."""
    return tuple(sorted(_allow_sets))


def baseline_snapshot() -> dict[str, Any]:
    """Copy of the live allow-sets (console diagnostics)."""
    return {kind: sorted(ids) for kind, ids in _allow_sets.items()}


def filter_ids(kind: str, ids: Iterable[str]) -> list[str]:
    """Convenience filter used by listing surfaces."""
    return [item for item in ids if resource_allowed(kind, item)]
