# -*- coding: utf-8 -*-
"""EP-2-19: template marketplace tests (store, routes, instantiation)."""

from __future__ import annotations

# pylint: disable=protected-access

from pathlib import Path

import httpx
import pytest
from httpx import MockTransport

from qwenpaw.hub.templates import TemplateStore

from tests.unit.hub.test_control_app import (
    _ProxyStream,
    _client,
    _create_user,
    _headers,
    _register,
)

_MANIFEST = {
    "name": "Research helper",
    "description": "Branchy research with a human checkpoint",
    "prompt": "Start by proposing a plan.",
    "skills": ["search"],
    "graph": {
        "id": "research-flow",
        "name": "Research flow",
        "entry": "gate",
        "nodes": [
            {"id": "gate", "kind": "gate", "params": {}},
            {"id": "go", "kind": "agent", "params": {"prompt": "run"}},
        ],
        "edges": [
            {"from": "gate", "to": "go", "when": "approve"},
        ],
    },
}


# ---------------------------------------------------------------- store


def test_store_upsert_and_revision(tmp_path: Path) -> None:
    store = TemplateStore(tmp_path / "control.db")
    first = store.upsert_template(
        "research",
        _MANIFEST,
        created_by="admin",
        status="published",
    )
    assert first["revision"] == 1
    assert first["status"] == "published"
    assert first["graph_node_count"] == 2
    assert first["skills"] == ["search"]

    second = store.upsert_template(
        "research",
        {**_MANIFEST, "name": "Research helper v2"},
        created_by="admin",
    )
    assert second["revision"] == 2
    assert second["name"] == "Research helper v2"


def test_store_status_gates_listing(tmp_path: Path) -> None:
    store = TemplateStore(tmp_path / "control.db")
    store.upsert_template("a", {"name": "A"}, created_by="admin")
    store.upsert_template(
        "b",
        {"name": "B"},
        created_by="admin",
        status="published",
    )
    visible = [
        item["template_id"]
        for item in store.list_templates(
            published_only=True,
        )
    ]
    assert visible == ["b"]

    offlined = store.set_status("b", "offline")
    assert offlined["status"] == "offline"
    assert store.list_templates(published_only=True) == []


def test_store_rejects_bad_manifests(tmp_path: Path) -> None:
    store = TemplateStore(tmp_path / "control.db")
    for bad in ("nope", {"name": ""}, {"name": "x", "skills": [1]}):
        with pytest.raises(ValueError):
            store.upsert_template(
                "bad",
                bad,  # type: ignore[arg-type]
                created_by="admin",
            )


# --------------------------------------------------------------- routes


def _transport() -> MockTransport:
    return MockTransport(
        lambda request: httpx.Response(200, stream=_ProxyStream()),
    )


def test_admin_upsert_and_member_listing(tmp_path: Path) -> None:
    with _client(tmp_path, _transport()) as client:
        admin_token = _register(client, "owner")

        upsert = client.post(
            "/api/hub/admin/templates",
            headers=_headers(admin_token),
            json={
                "template_id": "research",
                "manifest": _MANIFEST,
                "status": "published",
            },
        )
        assert upsert.status_code == 200
        assert upsert.json()["template"]["revision"] == 1

        _, member_token = _create_user(client, "member")

        # draft stays invisible to members
        client.post(
            "/api/hub/admin/templates",
            headers=_headers(admin_token),
            json={
                "template_id": "secret",
                "manifest": {"name": "Secret"},
            },
        )
        visible = client.get(
            "/api/hub/templates",
            headers=_headers(member_token),
        ).json()["templates"]
        assert [item["template_id"] for item in visible] == ["research"]
        assert visible[0]["graph_node_count"] == 2

        # admin sees everything
        admin_list = client.get(
            "/api/hub/admin/templates",
            headers=_headers(admin_token),
        ).json()["templates"]
        assert {item["template_id"] for item in admin_list} == {
            "research",
            "secret",
        }


def test_offline_removes_from_hall(tmp_path: Path) -> None:
    with _client(tmp_path, _transport()) as client:
        admin_token = _register(client, "owner")
        _, member_token = _create_user(client, "member")
        client.post(
            "/api/hub/admin/templates",
            headers=_headers(admin_token),
            json={
                "template_id": "research",
                "manifest": _MANIFEST,
                "status": "published",
            },
        )
        patched = client.patch(
            "/api/hub/admin/templates/research",
            headers=_headers(admin_token),
            json={"status": "offline"},
        )
        assert patched.status_code == 200

        visible = client.get(
            "/api/hub/templates",
            headers=_headers(member_token),
        ).json()["templates"]
        assert visible == []
        events, total = client.app.state.operations.list_events(
            page=1,
            page_size=10,
            action="template.status_changed",
        )
        assert total == 1
        assert events[0]["detail"]["status"] == "offline"


def test_instantiate_unpublished_404(tmp_path: Path) -> None:
    with _client(tmp_path, _transport()) as client:
        admin_token = _register(client, "owner")
        _, member_token = _create_user(client, "member")
        client.post(
            "/api/hub/admin/templates",
            headers=_headers(admin_token),
            json={"template_id": "draft", "manifest": {"name": "D"}},
        )
        response = client.post(
            "/api/hub/templates/draft/instantiate",
            headers=_headers(member_token),
        )
        assert response.status_code == 404


def test_member_cannot_admin_templates(tmp_path: Path) -> None:
    with _client(tmp_path, _transport()) as client:
        _register(client, "owner")
        _, member_token = _create_user(client, "member")
        response = client.post(
            "/api/hub/admin/templates",
            headers=_headers(member_token),
            json={"template_id": "x", "manifest": {"name": "X"}},
        )
        assert response.status_code == 403
