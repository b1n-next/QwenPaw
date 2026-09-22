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


def escape_percent_literals(sql: str) -> str:
    """Double literal ``%`` for psycopg's client-side binding.

    Runs BEFORE ``?`` → ``%s`` translation so the placeholders we
    synthesize stay single-percent.
    """
    # psycopg's client-side parser counts % everywhere — including
    # inside SQL string literals — so escape unconditionally; the %s
    # placeholders are synthesized AFTER this pass.
    return sql.replace("%", "%%")


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
        f"{match.group(2).strip()} ON CONFLICT DO NOTHING"
    )


_JSON_CHECK_RE = re.compile(
    r"CHECK\s*\(\s*json_valid\s*\([^)]*\)\s*\)",
    re.IGNORECASE,
)


_NOCASE_RE = re.compile(r"\s+COLLATE\s+NOCASE", re.IGNORECASE)


_SQLITE_FN_MAP: list[tuple[re.Pattern[str], str]] = [
    # SQLite SQL-embedded functions → PG spellings (28 §P1)
    (re.compile(r"datetime\(\s*'now'\s*\)", re.IGNORECASE), "now()::text"),
    (
        re.compile(r"hex\(\s*randomblob\(\s*(\d+)\s*\)\s*\)", re.IGNORECASE),
        r"encode(gen_random_bytes(\1), 'hex')",
    ),
    (re.compile(r"\bsubstr\b\s*\(", re.IGNORECASE), "substring("),
]


def _map_sqlite_functions(statement: str) -> str:
    """Rewrite SQLite SQL functions to their PG equivalents."""
    for pattern, replacement in _SQLITE_FN_MAP:
        statement = pattern.sub(replacement, statement)
    return statement


def _strip_collate_nocase(statement: str) -> str:
    """Drop SQLite's ``COLLATE NOCASE`` on PG.

    Case-insensitive uniqueness moves to the app layer (writes
    normalize via ``strip()``; lookups use ILIKE — 28 §P1 per-store).
    """
    return _NOCASE_RE.sub("", statement)


def _strip_json_valid_checks(statement: str) -> str:
    """Drop SQLite's ``CHECK(json_valid(col))`` on PG.

    JSON validity is enforced by the writers (``json.dumps``); PG
    deployments may tighten columns to ``jsonb`` later (28 §P1).
    Commas before the removed CHECK are cleaned up so the DDL stays
    parseable.
    """
    without = _JSON_CHECK_RE.sub("", statement)
    # collapse ",  ," or ", )" leftovers from the removal
    without = re.sub(r",\s*,", ",", without)
    without = re.sub(r",\s*\)", ")", without)
    return without


def dialect_statement(sql: str, *, postgres: bool) -> str:
    """One statement through the dialect pipeline for a driver."""
    if not postgres:
        return sql
    statement = rewrite_insert_or_ignore(sql)
    if statement.strip().upper().startswith("PRAGMA"):
        return ""
    if statement.strip().upper().startswith("BEGIN IMMEDIATE"):
        statement = "BEGIN"
    statement = _strip_json_valid_checks(statement)
    statement = _strip_collate_nocase(statement)
    statement = _map_sqlite_functions(statement)
    statement = escape_percent_literals(statement)
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


class PgRow(tuple):
    """A result row shaped like ``sqlite3.Row``.

    Supports positional ``row[0]`` and named ``row["col"]`` access
    plus ``keys()`` (so ``dict(row)`` keeps working) — every store
    written against sqlite3 keeps running unchanged on PG.
    """

    def __new__(cls, values: list, columns: list[str]) -> "PgRow":
        instance = super().__new__(cls, values)
        instance.columns = columns
        return instance

    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, str):
            try:
                return tuple.__getitem__(
                    self,
                    self.columns.index(item),
                )
            except ValueError as exc:
                raise KeyError(item) from exc
        return tuple.__getitem__(self, item)

    def keys(self) -> list[str]:
        return list(self.columns)


def _pg_row_factory(cursor: Any) -> Any:
    columns = [d.name for d in cursor.description or []]

    def make(values: list) -> PgRow:
        return PgRow(values, columns)

    return make


class PgConnection:
    """A psycopg connection dressed as the sqlite3 surface the Hub
    stores rely on: ``execute()``, ``commit()``, ``rollback()``,
    ``close()``, context-manager semantics, and ``row["col"]`` access
    via ``dict_row``."""

    def __init__(self, url: str) -> None:
        import psycopg  # optional extra: qwenpaw[postgres]

        self._conn = psycopg.connect(url, row_factory=_pg_row_factory)

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
    "escape_percent_literals",
    "PgConnection",
    "configured_db_url",
    "dialect_statement",
    "is_postgres_url",
    "open_hub_connection",
    "rewrite_insert_or_ignore",
    "split_script",
    "translate_placeholders",
]
