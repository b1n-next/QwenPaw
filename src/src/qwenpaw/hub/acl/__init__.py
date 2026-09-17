# -*- coding: utf-8 -*-
"""Hub proxy ACL (enterprise layer, Phase 0).

Public surface:
- :class:`AclEngine` / :class:`Decision` — ordered-rule evaluator
  (fail-closed, admin bypass, acl.json overlay with hot reload).
- :data:`DEFAULT_RULES` — the EP-0-1 chat-plane/admin-plane grouping.
- :func:`permissions_payload` — console menu deny-list per role.
"""

from __future__ import annotations

from .engine import AclEngine, Decision
from .rules import DEFAULT_RULES, RuleSpec
from .console_map import permissions_payload

__all__ = [
    "AclEngine",
    "Decision",
    "DEFAULT_RULES",
    "RuleSpec",
    "permissions_payload",
]
