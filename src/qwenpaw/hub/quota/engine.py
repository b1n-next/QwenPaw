# -*- coding: utf-8 -*-
"""Quota engine with soft/hard thresholds (Phase 2, EP-2-3).

Per-user daily quotas over two dimensions — tokens and requests —
evaluated at the hub proxy BEFORE forwarding (07 §3). Configuration
is a hot-reloadable overlay at <hub root>/quota.json mirroring the
ACL overlay pattern: default limits, per-user overrides, and one
soft-threshold ratio (default 0.8).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_DIMENSIONS = ("daily_tokens", "daily_requests")
_CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class QuotaDecision:
    """Outcome of one quota check."""

    allowed: bool
    dimension: str = ""
    used: int = 0
    limit: int = 0
    ratio: float = 0.0
    soft_hit: bool = False

    @property
    def reason(self) -> str:
        """Human-readable reason for denials and warnings."""
        if not self.allowed:
            return (
                f"daily {self.dimension} quota exceeded: "
                f"{self.used}/{self.limit}"
            )
        if self.soft_hit:
            percent = int(self.ratio * 100)
            return f"daily {self.dimension} quota at {percent}%"
        return ""


@dataclass(frozen=True)
class UsageSnapshot:
    """Current-day usage counters for one user (tenant)."""

    tokens: int = 0
    requests: int = 0


class QuotaEngine:
    """Hot-reloadable quota limits with a decision cache."""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config_path = config_path
        self._clock = clock
        self._lock = threading.Lock()
        self._loaded_at: float = -(1 << 30)
        self._mtime: float = -1.0
        self._config: Dict[str, Any] = {}
        self._cache: Dict[str, tuple[float, UsageSnapshot]] = {}
        self._reload_if_due(force=True)

    # -------------------------------------------------- config

    def _reload_if_due(self, *, force: bool = False) -> None:
        try:
            mtime = (
                self._config_path.stat().st_mtime
                if self._config_path is not None
                else -1.0
            )
        except OSError:
            mtime = -1.0
        now = self._clock()
        if not force and mtime == self._mtime and now - self._loaded_at < 5:
            return
        config: Dict[str, Any] = {}
        if self._config_path is not None and mtime > 0:
            try:
                payload = json.loads(
                    self._config_path.read_text(encoding="utf-8"),
                )
                if isinstance(payload, dict):
                    config = payload
            except (OSError, ValueError) as exc:
                logger.warning(
                    "quota overlay %s unreadable (%s); keeping previous",
                    self._config_path,
                    exc,
                )
                return
        with self._lock:
            self._config = config
            self._mtime = mtime
            self._loaded_at = now
            self._cache.clear()

    def _limits_for(self, user_id: str) -> Dict[str, Optional[int]]:
        defaults = self._config.get("default", {})
        users = self._config.get("users", {})
        override = users.get(user_id, {}) if isinstance(users, dict) else {}
        if not isinstance(override, dict):
            override = {}
        if not isinstance(defaults, dict):
            defaults = {}
        limits: Dict[str, Optional[int]] = {}
        for dimension in _DIMENSIONS:
            raw = override.get(dimension, defaults.get(dimension))
            try:
                limits[dimension] = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                limits[dimension] = None
        return limits

    def _soft_ratio(self) -> float:
        raw = self._config.get("soft_threshold", 0.8)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.8
        return value if 0 < value <= 1 else 0.8

    # -------------------------------------------------- checks

    @staticmethod
    def _snapshot_value(
        snapshot: UsageSnapshot,
        dimension: str,
    ) -> int:
        if dimension == "daily_tokens":
            return snapshot.tokens
        return snapshot.requests

    def check(
        self,
        user_id: str,
        snapshot: UsageSnapshot,
    ) -> QuotaDecision:
        """Evaluate the user's quotas against one usage snapshot.

        Pure computation over the snapshot — the expensive part
        (the usage SQL) is cached separately via usage_snapshot().
        """
        self._reload_if_due()
        return self._evaluate(user_id, snapshot)

    def usage_snapshot(
        self,
        user_id: str,
        fetcher: Callable[[], UsageSnapshot],
    ) -> UsageSnapshot:
        """30s-cached usage snapshot per user (07 §3: avoid per-request
        aggregation queries; the softening window is acceptable for
        both warning and cutoff semantics)."""
        stamp = self._clock()
        cached = self._cache.get(user_id)
        if cached is not None and stamp - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        snapshot = fetcher()
        with self._lock:
            self._cache[user_id] = (stamp, snapshot)
        return snapshot

    def _evaluate(
        self,
        user_id: str,
        snapshot: UsageSnapshot,
    ) -> QuotaDecision:
        limits = self._limits_for(user_id)
        soft = self._soft_ratio()
        worst_soft: Optional[QuotaDecision] = None
        for dimension in _DIMENSIONS:
            limit = limits[dimension]
            if not limit or limit <= 0:
                continue
            used = self._snapshot_value(snapshot, dimension)
            ratio = used / limit
            if ratio >= 1.0:
                return QuotaDecision(
                    allowed=False,
                    dimension=dimension,
                    used=used,
                    limit=limit,
                    ratio=ratio,
                    soft_hit=True,
                )
            if ratio >= soft:
                candidate = QuotaDecision(
                    allowed=True,
                    dimension=dimension,
                    used=used,
                    limit=limit,
                    ratio=ratio,
                    soft_hit=True,
                )
                if worst_soft is None or candidate.ratio > worst_soft.ratio:
                    worst_soft = candidate
        if worst_soft is not None:
            return worst_soft
        return QuotaDecision(allowed=True)

    # -------------------------------------------------- status

    def status(
        self,
        user_id: str,
        snapshot: UsageSnapshot,
    ) -> Dict[str, Any]:
        """Ratios per dimension for admin display."""
        limits = self._limits_for(user_id)
        rows = []
        for dimension in _DIMENSIONS:
            limit = limits[dimension]
            used = self._snapshot_value(snapshot, dimension)
            rows.append(
                {
                    "dimension": dimension,
                    "used": used,
                    "limit": limit,
                    "ratio": (used / limit) if limit else None,
                },
            )
        return {
            "user_id": user_id,
            "soft_threshold": self._soft_ratio(),
            "dimensions": rows,
        }


__all__ = ["QuotaDecision", "QuotaEngine", "UsageSnapshot"]
