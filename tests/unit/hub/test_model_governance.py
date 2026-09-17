# -*- coding: utf-8 -*-
"""E6 rate/concurrency limits + E5 model fallback chains."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw.hub.control_app import create_hub_app
from qwenpaw.hub.ratelimit import RateLimiter


# ------------------------------------------------- limiter unit


def _config(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_rate_window_blocks_and_slides(tmp_path: Path) -> None:
    config = tmp_path / "rl.json"
    _config(config, {"default": {"requests_per_minute": 2}})
    tick = [0.0]
    limiter = RateLimiter(config, clock=lambda: tick[0])
    assert limiter.check("u").allowed
    assert limiter.check("u").allowed
    blocked = limiter.check("u")
    assert not blocked.allowed
    assert blocked.reason == "rate"
    assert blocked.retry_after >= 1
    tick[0] = 61.0
    assert limiter.check("u").allowed


def test_concurrency_gauge_releases(tmp_path: Path) -> None:
    config = tmp_path / "rl.json"
    _config(config, {"default": {"concurrent": 1}})
    tick = [0.0]
    limiter = RateLimiter(config, clock=lambda: tick[0])
    assert limiter.check("u").allowed
    blocked = limiter.check("u")
    assert not blocked.allowed
    assert blocked.reason == "concurrency"
    limiter.release("u")
    assert limiter.inflight("u") == 0
    assert limiter.check("u").allowed


def test_user_override_and_hot_reload(tmp_path: Path) -> None:
    config = tmp_path / "rl.json"
    _config(
        config,
        {
            "default": {"requests_per_minute": 5},
            "users": {"vip": {"requests_per_minute": 1}},
        },
    )
    tick = [0.0]
    limiter = RateLimiter(config, clock=lambda: tick[0])
    assert limiter.check("vip").allowed
    assert not limiter.check("vip").allowed
    assert limiter.check("other").allowed  # default budget separate
    _config(config, {"default": {}, "users": {}})
    limiter.reload(force=True)
    assert limiter.check("vip").allowed  # unlimited now


def test_corrupt_overlay_keeps_previous(tmp_path: Path) -> None:
    config = tmp_path / "rl.json"
    _config(config, {"default": {"requests_per_minute": 1}})
    tick = [0.0]
    limiter = RateLimiter(config, clock=lambda: tick[0])
    assert limiter.check("u").allowed
    assert not limiter.check("u").allowed
    config.write_text("{not json", encoding="utf-8")
    limiter.reload(force=True)
    # corrupt file rejected -> previous limits still enforced
    assert not limiter.check("u").allowed


# ------------------------------------------------- proxy integration


def _setup(tmp_path: Path, limits: dict) -> TestClient:
    (tmp_path / "ratelimit.json").write_text(
        json.dumps({"default": limits}),
        encoding="utf-8",
    )
    return TestClient(create_hub_app(root_dir=tmp_path, public_bind=False))


def test_proxy_429_with_retry_after(tmp_path: Path) -> None:
    with _setup(tmp_path, {"requests_per_minute": 1}) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        first = client.get("/api/config", headers=headers)
        assert first.status_code in (200, 404, 502)  # runtime-dependent
        second = client.get("/api/config", headers=headers)
        assert second.status_code == 429
        assert second.json()["detail"]["code"] == "RATE_LIMITED"
        assert int(second.headers["Retry-After"]) >= 1
        # audit trail
        entries = client.get(
            "/api/hub/admin/audit",
            headers=headers,
        ).json()["items"]
        assert any(
            entry["action"] == "ratelimit.exceeded" for entry in entries
        )


def test_admin_endpoints_not_limited_for_ops(tmp_path: Path) -> None:
    with _setup(tmp_path, {"requests_per_minute": 1}) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        # consume the one proxied request
        client.get("/api/config", headers=headers)
        assert client.get("/api/config", headers=headers).status_code == 429
        # admin plane is NOT behind the proxy gate
        for _ in range(3):
            assert (
                client.get(
                    "/api/hub/admin/audit/verify",
                    headers=headers,
                ).status_code
                == 200
            )


# ------------------------------------------------- E5 fallback chains


def test_fallback_crud_and_projection(tmp_path: Path) -> None:
    with TestClient(
        create_hub_app(root_dir=tmp_path, public_bind=False),
    ) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        client.post(
            "/api/hub/admin/users",
            json={"username": "member", "password": "pw-123456"},
            headers=headers,
        )
        member = client.app.state.auth_service.authenticate(
            "member",
            "pw-123456",
        )[1]
        # validation
        assert (
            client.put(
                "/api/hub/admin/models/m1/fallbacks",
                json={"fallbacks": "nope"},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.put(
                "/api/hub/admin/models/m1/fallbacks",
                json={"fallbacks": ["m1"]},
                headers=headers,
            ).status_code
            == 422
        )
        # write + read
        put = client.put(
            "/api/hub/admin/models/m1/fallbacks",
            json={"fallbacks": ["m2", "m3"]},
            headers=headers,
        )
        assert put.status_code == 200
        assert put.json()["revision"] == 1
        got = client.get(
            "/api/hub/admin/models/m1/fallbacks",
            headers=headers,
        )
        assert got.json()["fallbacks"] == ["m2", "m3"]
        # empty default
        empty = client.get(
            "/api/hub/admin/models/unknown/fallbacks",
            headers=headers,
        )
        assert empty.json()["fallbacks"] == []
        # user-plane projection
        projection = client.get(
            "/api/hub/models/fallbacks",
            headers={"Authorization": f"Bearer {member}"},
        )
        assert projection.status_code == 200
        assert projection.json()["m1"]["fallbacks"] == ["m2", "m3"]
        # audited
        entries = client.get(
            "/api/hub/admin/audit",
            headers=headers,
        ).json()["items"]
        assert any(
            entry["action"] == "model.fallbacks.update" for entry in entries
        )
        # member cannot write
        denied = client.put(
            "/api/hub/admin/models/m1/fallbacks",
            json={"fallbacks": []},
            headers={"Authorization": f"Bearer {member}"},
        )
        assert denied.status_code == 403
