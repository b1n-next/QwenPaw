# -*- coding: utf-8 -*-
"""Per-user rate limiting at the hub proxy (E6, EP-series gap).

Sliding-window request-rate plus in-flight concurrency caps per
user, configured through a hot-reloadable ``ratelimit.json``
overlay (same shape and reload semantics as quota.json). All
state is in-memory: the hub is single-replica by design, so the
window and the in-flight gauge live and die with the process.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class RateDecision:
    """Outcome of one rate check."""

    allowed: bool
    reason: str = ""  # "" | "rate" | "concurrency"
    retry_after: int = 0  # seconds, for 429 responses


class RateLimiter:
    """Hot-reloadable per-user rate and concurrency caps."""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        *,
        clock=time.monotonic,
    ) -> None:
        self._config_path = config_path
        self._clock = clock
        self._lock = threading.Lock()
        self._config: Dict[str, object] = {}
        self._mtime: float = -1.0
        self._checked_at: float = -(1 << 30)
        self._hits: Dict[str, list[float]] = {}
        self._inflight: Dict[str, int] = {}
        self._reload(force=True)

    # -------------------------------------------------- config

    def reload(self, *, force: bool = False) -> None:
        """Re-read the overlay (public: admin/tests force a refresh)."""
        self._reload(force=force)

    def _reload(self, *, force: bool = False) -> None:
        try:
            mtime = (
                self._config_path.stat().st_mtime
                if self._config_path is not None
                else -1.0
            )
        except OSError:
            mtime = -1.0
        now = self._clock()
        if not force and mtime == self._mtime and now - self._checked_at < 5:
            return
        config: Dict[str, object] = {}
        if self._config_path is not None and mtime > 0:
            try:
                payload = json.loads(
                    self._config_path.read_text(encoding="utf-8"),
                )
                if isinstance(payload, dict):
                    config = payload
            except (OSError, ValueError) as exc:
                logger.warning(
                    "ratelimit overlay %s unreadable (%s); keeping previous",
                    self._config_path,
                    exc,
                )
                return
        with self._lock:
            self._config = config
            self._mtime = mtime
            self._checked_at = now

    def _limits_for(self, user_id: str) -> Tuple[int, int]:
        defaults = self._config.get("default", {})
        users = self._config.get("users", {})
        override = users.get(user_id, {}) if isinstance(users, dict) else {}
        if not isinstance(override, dict):
            override = {}
        if not isinstance(defaults, dict):
            defaults = {}

        def _pick(key: str) -> int:
            raw = override.get(key, defaults.get(key))
            try:
                value = int(raw) if raw is not None else 0
            except (TypeError, ValueError):
                value = 0
            return max(0, value)

        return _pick("requests_per_minute"), _pick("concurrent")

    # -------------------------------------------------- checks

    def check(self, user_id: str) -> RateDecision:
        """Admit one request: rate window + concurrency gauge."""
        self._reload()
        rpm, concurrent = self._limits_for(user_id)
        now = self._clock()
        with self._lock:
            if rpm > 0:
                hits = self._hits.setdefault(user_id, [])
                cutoff = now - _WINDOW_SECONDS
                while hits and hits[0] <= cutoff:
                    hits.pop(0)
                if len(hits) >= rpm:
                    retry = max(1, int(_WINDOW_SECONDS - (now - hits[0])))
                    return RateDecision(
                        allowed=False,
                        reason="rate",
                        retry_after=retry,
                    )
            if concurrent > 0:
                if self._inflight.get(user_id, 0) >= concurrent:
                    return RateDecision(
                        allowed=False,
                        reason="concurrency",
                    )
            if rpm > 0:
                self._hits.setdefault(user_id, []).append(now)
            self._inflight[user_id] = self._inflight.get(user_id, 0) + 1
        return RateDecision(allowed=True)

    def release(self, user_id: str) -> None:
        """Mark one in-flight request done (call on every exit path)."""
        with self._lock:
            current = self._inflight.get(user_id, 0) - 1
            if current > 0:
                self._inflight[user_id] = current
            else:
                self._inflight.pop(user_id, None)

    def inflight(self, user_id: str) -> int:
        """Current in-flight gauge (display/tests)."""
        with self._lock:
            return self._inflight.get(user_id, 0)


__all__ = ["RateDecision", "RateLimiter"]
