# -*- coding: utf-8 -*-
"""Phase 3 (G-P14 closing): mobile H5 approval page tests."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from qwenpaw.app._app import app

# The exact API calls the H5 page issues (kept in lockstep with the
# inline JS in routers/mobile.py).
_PAGE_LIST_CALL = "/api/approval/list"
_PAGE_RESOLVE_PREFIX = "/api/approval/"


def test_page_served_as_html() -> None:
    with TestClient(app) as client:
        response = client.get("/mobile/approvals")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_page_is_dependency_free_and_mobile_shaped() -> None:
    with TestClient(app) as client:
        page = client.get("/mobile/approvals").text
    # viewport meta for phones
    assert 'name="viewport"' in page
    # no external asset: everything inline (no src=/href= to files)
    assert "<script src=" not in page
    assert 'rel="stylesheet"' not in page
    # approve/deny actions present
    assert "批准" in page and "驳回" in page


def test_page_targets_existing_approval_api() -> None:
    with TestClient(app) as client:
        page = client.get("/mobile/approvals").text
    assert _PAGE_LIST_CALL in page
    assert _PAGE_RESOLVE_PREFIX + '" + decision' in page
    # the resolve payload keys match ApprovalActionRequest
    assert "request_id" in page
    assert "session_id" in page


def test_page_persists_token_locally() -> None:
    with TestClient(app) as client:
        page = client.get("/mobile/approvals").text
    assert "localStorage" in page
    assert "Bearer " + '" + token' in page


def test_approval_list_api_shape_for_page() -> None:
    """The list endpoint answers with the array key the page reads."""
    with TestClient(app) as client:
        response = client.get("/api/approval/list")
    assert response.status_code == 200
    assert "pending_approvals" in response.json()


def test_resolve_unknown_request_404_for_page() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/approval/approve",
            json={"request_id": "ghost", "session_id": "s"},
        )
    assert response.status_code == 404


def test_page_root_session_id_used_for_resolve() -> None:
    """Cards must carry root_session_id — approve() verifies it."""
    with TestClient(app) as client:
        page = client.get("/mobile/approvals").text
    match = re.search(r"root_session_id", page)
    assert match is not None
