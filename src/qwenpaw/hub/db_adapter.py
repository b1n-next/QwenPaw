# -*- coding: utf-8 -*-
"""A6 P0: dual-dialect connection adapter for the Hub stores.

All Hub stores funnel through one connection factory, so the
SQLite→PostgreSQL migration (docs/enterprise/28) switches drivers in
exactly one place. Default behaviour — no ``QWENPAW_HUB_DB_URL`` — is
byte-for-byte the legacy SQLite path; stores keep writing portable
``?`` placeholders and ``sqlite3.Row``-style ``row["col"]`` access.

Dialect surface (28 §P0):

* placeholder translation ``?`` → ``%s`` on the PG path
* ``PRAGMA`` / WAL setup is SQLite-only and skipped on PG
* ``BEGIN IMMEDIATE`` maps to ``BEGIN`` (write serialization moves to
  PG's default isolation; advisory locks are the P2 upgrade path)
* ``INSERT OR IGNORE`` / ``ON CONFLICT`` are rewritten from a
  SQLite-flavoured statement to the PG-compatible spelling
* ``executescript`` is emulated on PG by statement-splitting

The adapter is deliberately boring: no ORM, no async — a thin shim
over the two drivers with the dialect differences named and tested.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

DB_URL_ENV = "QWENPAW_HUB_DB_URL"

_PLACEHOLDER_RE = re.compile(r"\?(?!\d)")


def configured_db_url() -> Optional[str]:
    """The ops-configured external database URL, if any (28 §P0)."""
    raw = os.environ.get(DB_URL_ENV, "").strip()
    return raw or None


def is_postgres_url(url: str) -> bool:
    return url.startswith(("postgresql://", "postgres://"))


def translate_placeholders(sql: str) -> str:
    """Rewrite ``?`` placeholders to ``%s`` for psycopg (PG path).

    Numeric literals like ``?5`` are left alone; string literals
    containing ``?`` are preserved by scanning quotes.
    """
    out: list[str] = []
    in_single = False
    in_double = False
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "'" and not in_double:
            in_single = not in_single
            out.append(char)
        elif char == '"' and not in_single:
            in_double = not in_double
            out.append(char)
        elif (
            char == "?"
            and not in_single
            and not in_double
            and not (index + 1 < length and sql[index + 1].isdigit())
        ):
            out.append("%s")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def rewrite_insert_or_ignore(sql: str) -> str:
    """``INSERT OR IGNORE INTO t`` → ``INSERT INTO t ON CONFLICT DO
    NOTHING`` (PG has no OR-modifier)."""
    pattern = re.compile(
        r"INSERT\s+OR\s+IGNORE\s+INTO\s+(.+?)\s+(VALUES.*)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(sql)
    if not match:
        return sql
    return (
        f"INSERT INTO {match.group(1).strip()} "
        f"ON CONFLICT DO NOTHING {match.group(2).strip()}"
    )


def dialect_statement(sql: str, *, postgres: bool) -> str:
    """One statement through the dialect pipeline for a driver."""
    if not postgres:
        return sql
    statement = rewrite_insert_or_ignore(sql)
    if statement.strip().upper().startswith("PRAGMA"):
        return ""
    if statement.strip().upper().startswith("BEGIN IMMEDIATE"):
        statement = "BEGIN"
    return translate_placeholders(statement)


def split_script(script: str) -> list[str]:
    """Split a ``;``-separated script into executable statements.

    Quote-aware (string literals may contain ``;``); comments
    (``--`` line comments) are dropped per-statement tail.
    """
    statements: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    index = 0
    length = len(script)
    while index < length:
        char = script[index]
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == ";" and not in_single and not in_double:
            piece = "".join(current).strip()
            if piece:
                statements.append(piece)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


class PgConnection:
    """A psycopg connection dressed as the sqlite3 surface the Hub
    stores rely on: ``execute()``, ``commit()``, ``rollback()``,
    ``close()``, context-manager semantics, and ``row["col"]`` access
    via ``dict_row``."""

    def __init__(self, url: str) -> None:
        import psycopg  # optional extra: qwenpaw[postgres]

        self._conn = psycopg.connect(url, row_factory=psycopg.rows.dict_row)

    # -- sqlite3-shaped API -----------------------------------------

    def execute(self, sql: str, parameters: Any = ()) -> Any:
        statement = dialect_statement(sql, postgres=True)
        if not statement:
            return _NoCursor()
        if isinstance(parameters, (list, tuple)):
            params: tuple = tuple(parameters)
        else:
            params = (parameters,)
        return self._conn.cursor().execute(statement, params)

    def executescript(self, script: str) -> None:
        for statement in split_script(script):
            rewritten = dialect_statement(statement, postgres=True)
            if rewritten:
                self._conn.cursor().execute(rewritten, ())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PgConnection":
        return self

    def __exit__(self, *exc: Any) -> None:
        if exc and exc[0] is not None:
            self.rollback()
        else:
            self.commit()
        self.close()

    @property
    def in_transaction(self) -> bool:
        return self._conn.info.transaction_status != 0


class _NoCursor:
    """Stand-in for skipped statements (PRAGMA on PG)."""

    def fetchall(self) -> list:  # pragma: no cover - trivial
        return []

    def fetchone(self) -> None:  # pragma: no cover - trivial
        return None

    @property
    def rowcount(self) -> int:
        return 0


@contextmanager
def open_hub_connection(
    database_path: Optional[Path] = None,
) -> Iterator[Any]:
    """Dialect-dispatching connection factory (28 §P0).

    ``QWENPAW_HUB_DB_URL`` set to a postgres URL → PG connection;
    anything else (or unset) → the legacy SQLite path, unchanged.
    """
    url = configured_db_url()
    if url and is_postgres_url(url):
        connection = PgConnection(url)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return
    if database_path is None:  # pragma: no cover - guard
        raise ValueError(
            "SQLite path required when no postgres URL is configured",
        )
    legacy = sqlite3.connect(database_path, timeout=5)
    legacy.row_factory = sqlite3.Row
    legacy.execute("PRAGMA foreign_keys = ON")
    legacy.execute("PRAGMA busy_timeout = 5000")
    try:
        yield legacy
        legacy.commit()
    except BaseException:
        legacy.rollback()
        raise
    finally:
        legacy.close()


__all__ = [
    "DB_URL_ENV",
    "PgConnection",
    "configured_db_url",
    "dialect_statement",
    "is_postgres_url",
    "open_hub_connection",
    "rewrite_insert_or_ignore",
    "split_script",
    "translate_placeholders",
]
