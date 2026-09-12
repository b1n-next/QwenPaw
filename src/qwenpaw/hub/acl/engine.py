# -*- coding: utf-8 -*-
"""ACL decision engine for the hub personal-runtime proxy (EP-0-2).

The engine is deliberately stdlib-only so it can be unit-tested without
FastAPI and embedded anywhere. Evaluation contract:

- ``admin`` role → always allowed (reason ``role-admin``).
- other roles → ordered rules; first match wins; no match → deny
  (fail-closed). Malformed/unnormalizable paths → deny.
- Optional ``acl.json`` overlay is evaluated **before** the built-in
  defaults so operators can carve exceptions without code changes. A
  broken overlay file is logged and ignored — never fail-open.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import unquote

from .rules import DEFAULT_RULES, RuleSpec

logger = logging.getLogger(__name__)

_ALLOWED_EFFECTS = frozenset({"allow", "deny"})
_VALID_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "WS"},
)
# Conservative length cap: the longest legit console route is ~120 chars.
_MAX_PATH_LENGTH = 2048
_FORBIDDEN_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Decision:
    """Outcome of one ACL evaluation."""

    allowed: bool
    reason: str  # stable token for audit events
    role: str
    method: str
    path: str  # normalized path that was matched


class AclEngine:
    """Thread-safe, hot-reloadable rule evaluator."""

    def __init__(
        self,
        rules: Tuple[RuleSpec, ...] = DEFAULT_RULES,
        *,
        config_path: Optional[Path] = None,
    ) -> None:
        self._defaults = tuple(rules)
        self._config_path = (
            Path(config_path) if config_path is not None else None
        )
        self._lock = threading.Lock()
        self._overlay: Tuple[RuleSpec, ...] = ()
        self._config_mtime_ns: Optional[int] = None
        self._rules: Tuple[RuleSpec, ...] = self._defaults
        self._reload_if_stale(force=True)

    # ── public API ────────────────────────────────────────────────────────

    def decide(self, role: str, method: str, raw_path: str) -> Decision:
        """Decide whether *role* may call *method raw_path*."""
        normalized = self._normalize(raw_path)
        if role == "admin":
            return Decision(
                allowed=True,
                reason="role-admin",
                role=role,
                method=method.upper(),
                path=normalized or raw_path,
            )
        if normalized is None:
            return Decision(
                allowed=False,
                reason="path-malformed",
                role=role,
                method=method.upper(),
                path=raw_path[:_MAX_PATH_LENGTH],
            )
        method = method.upper()
        self._reload_if_stale()
        with self._lock:
            rules = self._rules
        for rule in rules:
            if rule.matches(method, normalized):
                return Decision(
                    allowed=rule.effect == "allow",
                    reason=rule.name or f"overlay-{rule.effect}",
                    role=role,
                    method=method,
                    path=normalized,
                )
        return Decision(
            allowed=False,
            reason="default-deny",
            role=role,
            method=method,
            path=normalized,
        )

    # ── internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _normalize(raw_path: str) -> Optional[str]:
        """Return a normalized absolute path, or None when malformed.

        Mirrors the traversal defenses a reverse proxy needs: decode
        percent-escapes once, reject control characters, resolve dot
        segments, and collapse duplicate slashes.
        """
        if not raw_path or len(raw_path) > _MAX_PATH_LENGTH:
            return None
        decoded = unquote(raw_path)
        if _FORBIDDEN_CHARS.search(decoded):
            return None
        if not decoded.startswith("/"):
            decoded = "/" + decoded
        segments: List[str] = []
        for segment in decoded.split("/"):
            if segment in ("", "."):
                continue
            if segment == "..":
                if not segments:
                    return None  # escaping the root: treat as malformed
                segments.pop()
            else:
                segments.append(segment)
        return "/" + "/".join(segments)

    def _reload_if_stale(self, *, force: bool = False) -> None:
        if self._config_path is None:
            return
        try:
            mtime_ns = self._config_path.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if not force and mtime_ns == self._config_mtime_ns:
            return
        overlay = self._load_overlay()
        with self._lock:
            self._config_mtime_ns = mtime_ns
            self._overlay = overlay
            # Overlay rules shadow the defaults (evaluated first).
            self._rules = tuple(overlay) + self._defaults

    def _load_overlay(self) -> Tuple[RuleSpec, ...]:
        if self._config_path is None:
            return ()
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        except (OSError, ValueError) as exc:
            logger.warning(
                "Ignoring unreadable ACL config %s: %s",
                self._config_path,
                exc,
            )
            return ()
        if not isinstance(raw, dict):
            logger.warning(
                "Ignoring non-object ACL config %s",
                self._config_path,
            )
            return ()
        parsed: List[RuleSpec] = []
        for index, entry in enumerate(raw.get("rules", [])):
            if not isinstance(entry, dict):
                continue
            effect = str(entry.get("effect", "")).lower()
            pattern = entry.get("pattern")
            if effect not in _ALLOWED_EFFECTS or not isinstance(pattern, str):
                logger.warning(
                    "Skipping invalid ACL rule #%d in %s",
                    index,
                    self._config_path,
                )
                continue
            methods = entry.get("methods")
            method_set = None
            if methods is not None:
                if not isinstance(methods, list):
                    continue
                cleaned = {
                    str(m).upper()
                    for m in methods
                    if str(m).upper() in _VALID_METHODS
                }
                if not cleaned:
                    continue
                method_set = frozenset(cleaned)
            try:
                re.compile(pattern)
            except re.error as exc:
                logger.warning(
                    "Skipping uncompilable ACL pattern %r: %s",
                    pattern,
                    exc,
                )
                continue
            parsed.append(
                RuleSpec(
                    effect=effect,
                    pattern=pattern,
                    methods=method_set,
                    name=str(entry.get("name", f"overlay-{index}")),
                ),
            )
        return tuple(parsed)

    @classmethod
    def from_env(
        cls,
        *,
        config_dir: Optional[Path] = None,
        env: Optional[Any] = None,
    ) -> "AclEngine":
        """Build the engine (config path from env or *config_dir*)."""
        environ = env if env is not None else os.environ
        explicit = environ.get("QWENPAW_HUB_ACL_CONFIG", "").strip()
        if explicit:
            return cls(config_path=Path(explicit).expanduser())
        if config_dir is not None:
            return cls(config_path=Path(config_dir) / "acl.json")
        return cls()


__all__ = ["AclEngine", "Decision"]
