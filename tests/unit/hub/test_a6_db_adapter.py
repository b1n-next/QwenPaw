# -*- coding: utf-8 -*-
"""A6 P0: dialect adapter unit tests (pure functions + sqlite path)."""

from __future__ import annotations

from qwenpaw.hub.db_adapter import (
    dialect_statement,
    is_postgres_url,
    open_hub_connection,
    rewrite_insert_or_ignore,
    split_script,
    translate_placeholders,
)


def test_placeholder_translation_preserves_literals() -> None:
    sql = "SELECT * FROM t WHERE name = ? AND note = 'what?' LIMIT 10"
    out = translate_placeholders(sql)
    assert out.count("%s") == 1
    assert "'what?'" in out
    assert out.index("'what?'") < len(out)


def test_placeholder_translation_skips_quoted_and_numeric() -> None:
    sql = "UPDATE t SET a = ?, json = " + '\'{"x": ?}\', b = "col?"'
    out = translate_placeholders(sql)
    # only the real parameter becomes %s; string + quoted ident stay
    assert out.count("%s") == 1
    assert "'{\"x\": ?}'" in out
    assert '"col?"' in out


def test_insert_or_ignore_rewrite() -> None:
    sql = "INSERT OR IGNORE INTO hub_schema(key, value) VALUES (?, ?)"
    out = rewrite_insert_or_ignore(sql)
    assert "OR IGNORE" not in out
    assert "ON CONFLICT DO NOTHING" in out
    assert out.count("?") == 2


def test_dialect_pipeline_postgres() -> None:
    assert (
        dialect_statement(
            "INSERT OR IGNORE INTO t(a) VALUES (?)",
            postgres=True,
        )
        == "INSERT INTO t(a) ON CONFLICT DO NOTHING VALUES (%s)"
    )
    assert dialect_statement("PRAGMA journal_mode = WAL", postgres=True) == ""
    assert dialect_statement("BEGIN IMMEDIATE", postgres=True) == "BEGIN"
    assert (
        dialect_statement(
            "SELECT * FROM t WHERE id = ?",
            postgres=True,
        )
        == "SELECT * FROM t WHERE id = %s"
    )


def test_dialect_pipeline_sqlite_passthrough() -> None:
    sql = "INSERT OR IGNORE INTO t(a) VALUES (?)"
    assert dialect_statement(sql, postgres=False) == sql


def test_script_split_quote_aware() -> None:
    script = (
        "CREATE TABLE a (x TEXT);\n"
        "INSERT INTO a VALUES ('semi;colon');\n"
        "-- trailing comment\n"
        "CREATE TABLE b (y INT)"
    )
    parts = split_script(script)
    assert len(parts) == 3
    assert "'semi;colon'" in parts[1]


def test_url_detection() -> None:
    assert is_postgres_url("postgresql://u:p@h/db")
    assert is_postgres_url("postgres://u:p@h/db")
    assert not is_postgres_url("sqlite:////tmp/x.db")
    assert not is_postgres_url("")


def test_open_connection_sqlite_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("QWENPAW_HUB_DB_URL", raising=False)
    db = tmp_path / "t.db"
    with open_hub_connection(db) as connection:
        connection.execute(
            "CREATE TABLE demo (id INTEGER PRIMARY KEY, name TEXT)",
        )
        connection.execute("INSERT INTO demo VALUES (1, 'a')")
    with open_hub_connection(db) as connection:
        row = connection.execute(
            "SELECT name FROM demo WHERE id = ?",
            (1,),
        ).fetchone()
        assert row["name"] == "a"


def test_open_connection_pg_env_switch(
    tmp_path,
    monkeypatch,
) -> None:
    # pointing at an unreachable PG proves the URL is honoured (and
    # that the sqlite path is never touched when set)
    monkeypatch.setenv(
        "QWENPAW_HUB_DB_URL",
        "postgresql://nobody:nopass@127.0.0.1:1/nowhere",
    )
    raised = False
    try:
        with open_hub_connection(tmp_path / "unused.db"):
            pass  # unreachable host: construction must fail
    except BaseException:
        # psycopg OperationalError (driver present, host down) or
        # ModuleNotFoundError (no optional extra installed) — either
        # way the URL branch ran and the fallback never did.
        raised = True
    assert raised
    assert not (tmp_path / "unused.db").exists()
