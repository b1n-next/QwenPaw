# -*- coding: utf-8 -*-
"""EP-2-3: quota engine tests (soft/hard thresholds, cache, reload)."""

from __future__ import annotations

import json
from pathlib import Path

from qwenpaw.hub.quota import QuotaEngine, UsageSnapshot


def _write_config(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_no_config_allows_everything(tmp_path: Path) -> None:
    engine = QuotaEngine(tmp_path / "absent.json")
    decision = engine.check(
        "u",
        UsageSnapshot(tokens=10**9, requests=10**9),
    )
    assert decision.allowed and not decision.soft_hit


def test_under_limit_allows(tmp_path: Path) -> None:
    config = tmp_path / "quota.json"
    _write_config(
        config,
        {"default": {"daily_tokens": 100, "daily_requests": 10}},
    )
    engine = QuotaEngine(config)
    decision = engine.check("u", UsageSnapshot(tokens=50, requests=5))
    assert decision.allowed and not decision.soft_hit


def test_soft_threshold_hits_with_reason(tmp_path: Path) -> None:
    config = tmp_path / "quota.json"
    _write_config(config, {"default": {"daily_tokens": 100}})
    engine = QuotaEngine(config)
    decision = engine.check("u", UsageSnapshot(tokens=80))
    assert decision.allowed and decision.soft_hit
    assert "80%" in decision.reason
    # just below the soft line stays silent
    quiet = engine.check("u", UsageSnapshot(tokens=79))
    assert quiet.allowed and not quiet.soft_hit


def test_hard_threshold_blocks_with_detail(tmp_path: Path) -> None:
    config = tmp_path / "quota.json"
    _write_config(config, {"default": {"daily_tokens": 100}})
    engine = QuotaEngine(config)
    decision = engine.check("u", UsageSnapshot(tokens=101))
    assert not decision.allowed
    assert decision.dimension == "daily_tokens"
    assert decision.used == 101 and decision.limit == 100
    assert "101/100" in decision.reason


def test_user_override_wins(tmp_path: Path) -> None:
    config = tmp_path / "quota.json"
    _write_config(
        config,
        {
            "default": {"daily_tokens": 100},
            "users": {"vip": {"daily_tokens": 1000}},
        },
    )
    engine = QuotaEngine(config)
    assert engine.check("vip", UsageSnapshot(tokens=500)).allowed
    assert not engine.check("pleb", UsageSnapshot(tokens=500)).allowed


def test_requests_dimension_independent(tmp_path: Path) -> None:
    config = tmp_path / "quota.json"
    _write_config(
        config,
        {"default": {"daily_tokens": 10**9, "daily_requests": 10}},
    )
    engine = QuotaEngine(config)
    blocked = engine.check("u", UsageSnapshot(tokens=1, requests=11))
    assert not blocked.allowed and blocked.dimension == "daily_requests"


def test_custom_soft_ratio(tmp_path: Path) -> None:
    config = tmp_path / "quota.json"
    _write_config(
        config,
        {"default": {"daily_tokens": 100}, "soft_threshold": 0.5},
    )
    engine = QuotaEngine(config)
    assert engine.check("u", UsageSnapshot(tokens=60)).soft_hit


def test_overlay_hot_reload(tmp_path: Path) -> None:
    config = tmp_path / "quota.json"
    _write_config(config, {"default": {"daily_tokens": 100}})
    engine = QuotaEngine(config, clock=lambda: 1.0)
    assert not engine.check("u", UsageSnapshot(tokens=150)).allowed
    # raise the limit on disk; engine picks it up without restart
    _write_config(config, {"default": {"daily_tokens": 1000}})
    engine._reload_if_due(force=True)  # pylint: disable=protected-access
    assert engine.check("u", UsageSnapshot(tokens=150)).allowed


def test_snapshot_cache_calls_fetcher_once_per_window(
    tmp_path: Path,
) -> None:
    config = tmp_path / "quota.json"
    _write_config(config, {"default": {"daily_tokens": 100}})
    tick = [0.0]
    engine = QuotaEngine(config, clock=lambda: tick[0])
    calls = []

    def fetcher() -> UsageSnapshot:
        calls.append(tick[0])
        return UsageSnapshot(tokens=1, requests=1)

    engine.usage_snapshot("u", fetcher)
    tick[0] = 10.0
    engine.usage_snapshot("u", fetcher)
    assert len(calls) == 1  # inside the 30s window
    tick[0] = 31.0
    engine.usage_snapshot("u", fetcher)
    assert len(calls) == 2  # window expired


def test_corrupt_overlay_keeps_previous(tmp_path: Path) -> None:
    config = tmp_path / "quota.json"
    _write_config(config, {"default": {"daily_tokens": 100}})
    engine = QuotaEngine(config)
    config.write_text("{not json", encoding="utf-8")
    engine._reload_if_due(force=True)  # pylint: disable=protected-access
    decision = engine.check("u", UsageSnapshot(tokens=150))
    assert not decision.allowed  # previous limits survive


def test_status_shape(tmp_path: Path) -> None:
    config = tmp_path / "quota.json"
    _write_config(config, {"default": {"daily_tokens": 200}})
    engine = QuotaEngine(config)
    status = engine.status("u", UsageSnapshot(tokens=100, requests=7))
    assert status["user_id"] == "u"
    assert status["soft_threshold"] == 0.8
    by_dim = {row["dimension"]: row for row in status["dimensions"]}
    assert by_dim["daily_tokens"]["ratio"] == 0.5
    assert by_dim["daily_requests"]["limit"] is None
