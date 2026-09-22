# -*- coding: utf-8 -*-
"""A6 P1: live-Postgres smoke (skipped unless QWENPAW_PG_TEST_URL set).

Run with a throwaway container:
    docker run -d --name qwenpaw-pg -e POSTGRES_PASSWORD=qwenpaw \
        -e POSTGRES_DB=hub -p 5433:5432 postgres:17
    QWENPAW_PG_TEST_URL=postgresql://postgres:qwenpaw@127.0.0.1:5433/hub \
        pytest tests/unit/hub/test_a6_pg_smoke.py
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

pg_url = os.environ.get("QWENPAW_PG_TEST_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not pg_url,
    reason="live PG smoke: set QWENPAW_PG_TEST_URL to run",
)


@pytest.fixture(name="pg_env")
def _pg_env(monkeypatch):
    monkeypatch.setenv("QWENPAW_HUB_DB_URL", pg_url)
    yield


def _reset_database() -> None:
    """Drop and recreate the public schema for a clean run."""
    import psycopg

    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def test_pg_full_hub_smoke(pg_env) -> None:  # pylint: disable=unused-argument
    from fastapi.testclient import TestClient

    from qwenpaw.hub.control_app import create_hub_app

    _reset_database()
    app = create_hub_app(root_dir=Path("."), public_bind=False)
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "pgsmoke", "password": "pw-123456"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        # C6 hierarchy over HTTP (parent passthrough)
        dept = client.post(
            "/api/hub/admin/groups",
            headers=headers,
            json={"name": "dept"},
        ).json()["group_id"]
        team = client.post(
            "/api/hub/admin/groups",
            headers=headers,
            json={"name": "team", "parent_group_id": dept},
        ).json()["group_id"]
        uid = client.get("/api/hub/me", headers=headers).json()["user_id"]
        client.post(
            f"/api/hub/admin/groups/{team}/members",
            headers=headers,
            json={"user_id": uid},
        )
        store = app.state.group_store
        assert store.descendant_member_ids("dept") == [uid]
        # D5 review flow persists
        submit = client.post(
            "/api/hub/templates/submit",
            headers=headers,
            json={
                "template_id": "t1",
                "manifest": {
                    "name": "T",
                    "description": "d",
                    "prompt": "p",
                    "graph": {"nodes": [], "edges": []},
                    "skills": [],
                },
            },
        )
        assert submit.status_code == 200
        assert submit.json()["template"]["status"] == "pending_review"
        # audit + hash chain under ctid ordering
        audit = client.get(
            "/api/hub/admin/audit",
            headers=headers,
        ).json()
        assert audit["total"] >= 1
        assert app.state.operations.verify_chain()["valid"] is True


def test_pg_json_and_dialect_roundtrip(  # pylint: disable=unused-argument
    pg_env,
) -> None:
    from qwenpaw.hub.database import initialize_hub_database

    with tempfile.TemporaryDirectory() as d:
        initialize_hub_database(Path(d) / "control.db")
    # idempotent second init (schema already exists in PG)
    with tempfile.TemporaryDirectory() as d:
        initialize_hub_database(Path(d) / "control.db")
