# -*- coding: utf-8 -*-
"""EP-2-13: hub policy baseline catalog tests (hub side)."""

from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw.hub.policy_catalog import (
    PolicyCatalogStore,
    canonical_rule_digest,
)

from tests.unit.hub.test_control_app import (
    _ProxyStream,
    _client,
    _create_user,
    _headers,
    _register,
)

_RULES = [
    {"match": "Bash(curl *)", "action": "ask", "reason": "exfiltration"},
    {"match": "Write(/etc/**)", "action": "deny", "reason": "system"},
]


def _transport() -> object:
    import httpx
    from httpx import MockTransport

    return MockTransport(
        lambda request: httpx.Response(200, stream=_ProxyStream()),
    )


# ---------------------------------------------------------------- store


def test_store_roundtrip_revision_and_digest(tmp_path: Path) -> None:
    store = PolicyCatalogStore(tmp_path / "control.db")
    first = store.update_baseline(_RULES, updated_by="owner")
    assert first["revision"] == 1
    assert first["enabled"] is True
    assert first["sha256"] == canonical_rule_digest(first["rules"])
    assert first["updated_by"] == "owner"

    second = store.update_baseline(_RULES[:1], updated_by="owner")
    assert second["revision"] == 2
    assert len(second["rules"]) == 1


def test_store_disabled_payload_is_none(tmp_path: Path) -> None:
    store = PolicyCatalogStore(tmp_path / "control.db")
    store.update_baseline(_RULES, updated_by="owner", enabled=False)
    assert store.baseline_payload() is None

    store.update_baseline(_RULES, updated_by="owner", enabled=True)
    payload = store.baseline_payload()
    assert payload is not None
    assert payload["revision"] == 2
    assert payload["sha256"] == canonical_rule_digest(payload["rules"])


def test_store_empty_returns_none(tmp_path: Path) -> None:
    store = PolicyCatalogStore(tmp_path / "control.db")
    assert store.get_baseline() is None
    assert store.baseline_payload() is None


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-list",
        [{"match": "", "action": "deny"}],
        [{"match": "Bash(*)", "action": "explode"}],
        [{"no_match": True, "action": "deny"}],
        [123],
    ],
)
def test_store_rejects_malformed_rules(tmp_path: Path, bad) -> None:
    store = PolicyCatalogStore(tmp_path / "control.db")
    with pytest.raises(ValueError):
        store.update_baseline(bad, updated_by="owner")


# ------------------------------------------------------------- routes


def test_admin_baseline_roundtrip_and_audit(tmp_path: Path) -> None:
    with _client(tmp_path, _transport()) as client:
        admin_token = _register(client, "owner")

        assert (
            client.get(
                "/api/hub/admin/policy/baseline",
                headers=_headers(admin_token),
            ).json()["baseline"]
            is None
        )

        response = client.put(
            "/api/hub/admin/policy/baseline",
            headers=_headers(admin_token),
            json={"rules": _RULES},
        )
        assert response.status_code == 200
        baseline = response.json()["baseline"]
        assert baseline["revision"] == 1
        assert baseline["enabled"] is True

        fetched = client.get(
            "/api/hub/admin/policy/baseline",
            headers=_headers(admin_token),
        ).json()["baseline"]
        assert fetched["sha256"] == baseline["sha256"]

        events, total = client.app.state.operations.list_events(
            page=1,
            page_size=10,
            action="policy.updated",
        )
        assert total == 1
        assert events[0]["detail"]["rule_count"] == 2


def test_baseline_validation_error_is_422(tmp_path: Path) -> None:
    with _client(tmp_path, _transport()) as client:
        admin_token = _register(client, "owner")
        response = client.put(
            "/api/hub/admin/policy/baseline",
            headers=_headers(admin_token),
            json={"rules": [{"match": "x", "action": "maybe"}]},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "INVALID_BASELINE"


def test_baseline_admin_only_member_denied(tmp_path: Path) -> None:
    with _client(tmp_path, _transport()) as client:
        _register(client, "owner")
        _, member_token = _create_user(client, "member")

        response = client.put(
            "/api/hub/admin/policy/baseline",
            headers=_headers(member_token),
            json={"rules": _RULES},
        )
        assert response.status_code == 403
