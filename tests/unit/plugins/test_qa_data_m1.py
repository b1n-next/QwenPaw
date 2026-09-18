# -*- coding: utf-8 -*-
"""J4/EP-2-6 M1: qa-data PawApp — guard, introspect, tools, routes."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

_APP_DIR = Path(__file__).resolve().parents[3] / "plugins" / "apps" / "qa-data"
_BACKEND_DIR = _APP_DIR / "backend"


def _load(name: str):
    """Load one backend module the way the plugin loader does.

    The host plugin loader registers the entry's directory on
    ``sys.path`` (see plugins/loader.py `_load_backend_module`), so
    bare imports like ``from guard import ...`` resolve there.
    """
    backend_str = str(_BACKEND_DIR)
    if backend_str not in sys.path:
        sys.path.insert(0, backend_str)
    spec = importlib.util.spec_from_file_location(
        f"qa_data_{name}",
        _BACKEND_DIR / f"{name}.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"qa_data_{name}"] = module
    spec.loader.exec_module(module)
    return module


guard = _load("guard")
introspect = _load("introspect")


@pytest.fixture(name="demo_db")
def _demo_db(tmp_path: Path) -> str:
    path = tmp_path / "demo.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT,
            region TEXT
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER,
            amount REAL,
            created_at TEXT
        );
        INSERT INTO customers VALUES (1, 'acme', 'north');
        INSERT INTO customers VALUES (2, 'globex', 'south');
        INSERT INTO orders VALUES (1, 1, 100.0, '2026-01-01');
        INSERT INTO orders VALUES (2, 1, 250.0, '2026-02-01');
        INSERT INTO orders VALUES (3, 2, 75.5, '2026-03-01');
        """,
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{path}"


# ── guard ────────────────────────────────────────────────────────────


def test_guard_accepts_select_and_injects_limit() -> None:
    result = guard.check_readonly_sql(
        "SELECT * FROM orders",
        force_limit=100,
    )
    assert "LIMIT" in result.sql.upper()
    assert result.limit_applied is True


def test_guard_keeps_existing_limit() -> None:
    result = guard.check_readonly_sql(
        "SELECT count(*) FROM orders LIMIT 5",
    )
    assert result.limit_applied is False


def test_guard_rejects_writes_and_multi() -> None:
    for bad in (
        "DELETE FROM orders",
        "UPDATE orders SET amount = 0",
        "INSERT INTO orders VALUES (9, 1, 1, 'x')",
        "SELECT 1; DROP TABLE orders",
        "WITH x AS (SELECT 1) INSERT INTO orders SELECT * FROM x",
    ):
        with pytest.raises(guard.GuardError):
            guard.check_readonly_sql(bad)


def test_guard_caps_requested_rows() -> None:
    result = guard.check_readonly_sql(
        "SELECT * FROM orders",
        force_limit=10000,
    )
    assert "LIMIT 500" in result.sql


# ── introspect ───────────────────────────────────────────────────────


def test_introspect_tables_and_search(demo_db: str) -> None:
    cache = introspect.SchemaCache(database_url=demo_db)
    tables = cache.tables()
    names = {t.name for t in tables}
    assert {"customers", "orders"} <= names
    hits = cache.search_schema("订单 金额 order amount")
    assert hits, "query tokens should match orders table"
    assert hits[0]["table"] == "orders"
    cols = {c["name"] for c in hits[0]["columns"]}
    assert "amount" in cols


def test_introspect_degrades_when_source_missing(tmp_path: Path) -> None:
    cache = introspect.SchemaCache(
        database_url=f"sqlite:///{tmp_path / 'missing.sqlite'}",
    )
    assert cache.tables() == []
    assert cache.search_schema("anything") == []


# ── app tools + routes ──────────────────────────────────────────────


def main_reload_schema(main) -> None:
    """Reach the app module's schema cache (test seam)."""

    def _refresh():
        main.__dict__["_schema"].refresh()

    _refresh()


def _return_ctx(ctx):
    """Dependency override callable (named for pylint clarity)."""

    def _provide():
        return ctx

    return _provide


@pytest.fixture(name="client")
def _client(demo_db: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("QADATA_DATABASE_URL", demo_db)
    main = _load("main")
    main_reload_schema(main)

    # fake ctx for routes; fake chat for /ask
    class _Ctx:
        async def chat(  # pylint: disable=unused-argument
            self,
            prompt: str,
        ) -> str:
            rows = main.run_readonly_sql(
                "SELECT region, SUM(amount) AS total FROM orders"
                " JOIN customers ON customer_id = customers.id"
                " GROUP BY region",
            )
            data = json.loads(rows)
            lines = ["| region | total |", "| --- | --- |"]
            for row in data["rows"]:
                lines.append(f"| {row[0]} | {row[1]} |")
            return (
                "北区交易额见下表。\n```sql\n"
                "SELECT region, SUM(amount) AS total FROM orders"
                " JOIN customers ON customer_id = customers.id"
                " GROUP BY region\n```\n" + "\n".join(lines)
            )

    api = FastAPI()
    api.include_router(main.router)
    api.dependency_overrides[main.get_ctx] = _return_ctx(_Ctx())
    return TestClient(api), main


def test_tables_route(client) -> None:
    http, _main = client
    body = http.get("/api/qa-data/tables").json()
    names = {t["name"] for t in body["tables"]}
    assert "orders" in names


def test_ask_route_returns_sql_and_table(client) -> None:
    http, _main = client
    body = http.post(
        "/api/qa-data/ask",
        json={"question": "按区域统计订单总额"},
    ).json()
    assert "SELECT" in body["answer"]
    assert "```sql" in body["answer"]
    assert "| north |" in body["answer"]


def test_run_readonly_sql_tool_end_to_end(client) -> None:
    _http, main = client
    payload = json.loads(
        main.run_readonly_sql("SELECT amount FROM orders"),
    )
    assert payload["columns"] == ["amount"]
    assert len(payload["rows"]) == 3
    assert payload["truncated"] is False


def test_tool_rejects_delete(client) -> None:
    _http, main = client
    payload = json.loads(
        main.run_readonly_sql("DELETE FROM orders"),
    )
    assert payload["error"] == "GUARD_REJECTED"
    # data intact
    payload2 = json.loads(
        main.run_readonly_sql("SELECT count(*) FROM orders"),
    )
    assert payload2["rows"][0][0] == "3"
