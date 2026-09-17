# -*- coding: utf-8 -*-
"""EP-2-24: prompt library + key pool tests (stores and routes)."""

from __future__ import annotations

# pylint: disable=protected-access

from pathlib import Path

import pytest

from qwenpaw.hub.key_pool import KeyPool
from qwenpaw.hub.prompt_library import PromptLibrary

from tests.unit.hub.test_control_app import (
    _client,
    _create_user,
    _headers,
    _register,
)
from tests.unit.hub.test_templates import _transport


# ------------------------------------------------------ prompt library


def test_propose_review_publishes_version(tmp_path: Path) -> None:
    library = PromptLibrary(tmp_path / "control.db")
    library.propose(
        "greet",
        "Greeter",
        "You are polite.",
        proposed_by="admin-a",
    )
    # pending — members do not see it
    assert not library.list_assets()

    asset = library.review(
        "greet",
        1,
        "approved",
        reviewed_by="admin-b",
    )
    assert asset["current_version"] == 1
    assert asset["content"] == "You are polite."
    assert library.list_assets()[0]["asset_id"] == "greet"

    # second proposal keeps v1 live until approved
    library.propose(
        "greet",
        "Greeter v2",
        "Be extra polite.",
        proposed_by="admin-a",
    )
    asset = library.get_asset("greet")
    assert asset["current_version"] == 1
    assert asset["content"] == "You are polite."
    assert library.list_assets(include_pending=True)[0]["has_pending"]

    library.review("greet", 2, "approved", reviewed_by="admin-b")
    asset = library.get_asset("greet")
    assert asset["current_version"] == 2
    assert asset["content"] == "Be extra polite."


def test_review_reject_keeps_current(tmp_path: Path) -> None:
    library = PromptLibrary(tmp_path / "control.db")
    library.propose("a", "A", "v1", proposed_by="x")
    library.review("a", 1, "approved", reviewed_by="y")
    library.propose("a", "A", "v2-bad", proposed_by="x")
    library.review("a", 2, "rejected", reviewed_by="y")
    asset = library.get_asset("a")
    assert asset["current_version"] == 1
    assert asset["content"] == "v1"

    # already reviewed versions cannot be re-reviewed
    assert library.review("a", 2, "approved", reviewed_by="y") is None


def test_propose_validates(tmp_path: Path) -> None:
    library = PromptLibrary(tmp_path / "control.db")
    with pytest.raises(ValueError):
        library.propose("", "n", "c", proposed_by="x")
    with pytest.raises(ValueError):
        library.propose("id", " ", "c", proposed_by="x")
    with pytest.raises(ValueError):
        library.propose("id", "n", " ", proposed_by="x")


# ------------------------------------------------------------- key pool


def test_lease_round_robin_and_disable(tmp_path: Path) -> None:
    pool = KeyPool(tmp_path / "control.db")
    pool.add_key("openai", "sk-1", key_id="k1")
    pool.add_key("openai", "sk-2", key_id="k2")

    order = [pool.lease("openai")["key_id"] for _ in range(4)]
    assert order == ["k1", "k2", "k1", "k2"]
    assert pool.lease("openai")["key_value"] == "sk-1"

    pool.set_status("k1", "disabled")
    assert [pool.lease("openai")["key_id"] for _ in range(2)] == [
        "k2",
        "k2",
    ]
    keys = pool.list_keys(provider="openai")
    by_id = {key["key_id"]: key for key in keys}
    assert by_id["k1"]["status"] == "disabled"
    assert by_id["k2"]["use_count"] >= 4
    # list never leaks values
    assert all("key_value" not in key for key in keys)


def test_lease_missing_provider(tmp_path: Path) -> None:
    pool = KeyPool(tmp_path / "control.db")
    assert pool.lease("ghost") is None
    with pytest.raises(ValueError):
        pool.lease("  ")


def test_add_key_validates(tmp_path: Path) -> None:
    pool = KeyPool(tmp_path / "control.db")
    with pytest.raises(ValueError):
        pool.add_key("", "v")
    with pytest.raises(ValueError):
        pool.add_key("p", " ")
    pool.add_key("p", "v", key_id="dup")
    with pytest.raises(ValueError):
        pool.add_key("p", "v2", key_id="dup")


# --------------------------------------------------------------- routes


def test_prompt_routes_approval_flow(tmp_path: Path) -> None:
    with _client(tmp_path, _transport()) as client:
        admin_token = _register(client, "owner")
        _, member_token = _create_user(client, "member")

        proposed = client.post(
            "/api/hub/admin/prompts",
            headers=_headers(admin_token),
            json={
                "asset_id": "greet",
                "name": "Greeter",
                "content": "You are polite.",
            },
        )
        assert proposed.status_code == 200

        # member: nothing approved yet
        empty = client.get(
            "/api/hub/prompts",
            headers=_headers(member_token),
        ).json()["prompts"]
        assert empty == []

        version = proposed.json()["prompt"]["versions"][0]["version"]
        approved = client.post(
            "/api/hub/admin/prompts/greet/review",
            headers=_headers(admin_token),
            json={"version": version, "decision": "approved"},
        )
        assert approved.status_code == 200

        visible = client.get(
            "/api/hub/prompts",
            headers=_headers(member_token),
        ).json()["prompts"]
        assert [item["asset_id"] for item in visible] == ["greet"]

        full = client.get(
            "/api/hub/prompts/greet",
            headers=_headers(member_token),
        ).json()
        assert full["content"] == "You are polite."

        events, total = client.app.state.operations.list_events(
            page=1,
            page_size=10,
            action="prompt.approved",
        )
        assert total == 1
        assert events[0]["detail"]["version"] == version


def test_key_routes_lease_flow(tmp_path: Path) -> None:
    with _client(tmp_path, _transport()) as client:
        admin_token = _register(client, "owner")
        _, member_token = _create_user(client, "member")

        for value in ("sk-1", "sk-2"):
            added = client.post(
                "/api/hub/admin/keys",
                headers=_headers(admin_token),
                json={"provider": "openai", "key_value": value},
            )
            assert added.status_code == 200

        first = client.post(
            "/api/hub/keys/lease",
            headers=_headers(member_token),
            json={"provider": "openai"},
        ).json()["lease"]
        second = client.post(
            "/api/hub/keys/lease",
            headers=_headers(member_token),
            json={"provider": "openai"},
        ).json()["lease"]
        assert first["key_id"] != second["key_id"]

        disabled = client.patch(
            f"/api/hub/admin/keys/{first['key_id']}",
            headers=_headers(admin_token),
            json={"status": "disabled"},
        )
        assert disabled.status_code == 200

        listed = client.get(
            "/api/hub/admin/keys",
            headers=_headers(admin_token),
        ).json()["keys"]
        assert all("key_value" not in key for key in listed)

        missing = client.post(
            "/api/hub/keys/lease",
            headers=_headers(member_token),
            json={"provider": "ghost"},
        )
        assert missing.status_code == 404

        forbidden = client.post(
            "/api/hub/admin/keys",
            headers=_headers(member_token),
            json={"provider": "x", "key_value": "y"},
        )
        assert forbidden.status_code == 403
