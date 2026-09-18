# -*- coding: utf-8 -*-
"""Schema introspection + lightweight keyword search for qa-data (J4).

M1 scope: cache tables/columns/types/comments in memory; search scores
by token overlap (BM25-lite). Semantic layer and vectors arrive with
M2/M3 (08 §5).
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import create_engine, inspect

DEFAULT_DATABASE_URL = "sqlite:////tmp/qa-data-demo.sqlite"

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+")
_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "in", "on", "for", "and", "or", "to", "is"},
)


@dataclass
class ColumnInfo:
    """One introspected column."""

    name: str
    type: str
    comment: str = ""
    semantic: str = ""


@dataclass
class TableInfo:
    """One introspected table with scoring helpers."""

    name: str
    columns: list[ColumnInfo] = field(default_factory=list)
    comment: str = ""
    semantic: str = ""

    def text(self) -> str:
        parts = [self.name, self.comment, self.semantic]
        parts.extend(
            f"{c.name} {c.comment} {c.semantic}" for c in self.columns
        )
        return " ".join(parts)


class SchemaCache:
    """In-memory schema cache with TTL + explicit refresh."""

    def __init__(self, database_url: str | None = None, ttl: int = 600):
        self._url = database_url or os.environ.get(
            "QADATA_DATABASE_URL",
            DEFAULT_DATABASE_URL,
        )
        self._ttl = int(ttl)
        self._tables: list[TableInfo] = []
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    @property
    def database_url(self) -> str:
        return self._url

    def refresh(self) -> int:
        """Reload schema from the source; returns table count."""
        engine = create_engine(self._url, future=True)
        try:
            inspector = inspect(engine)
            tables: list[TableInfo] = []
            for name in sorted(inspector.get_table_names()):
                info = TableInfo(name=name)
                try:
                    table_comment = inspector.get_table_comment(name)
                except NotImplementedError:
                    table_comment = {}
                info.comment = str((table_comment or {}).get("text") or "")
                for column in inspector.get_columns(name):
                    info.columns.append(
                        ColumnInfo(
                            name=str(column.get("name")),
                            type=str(column.get("type")),
                        ),
                    )
                tables.append(info)
        finally:
            engine.dispose()
        with self._lock:
            self._tables = tables
            self._loaded_at = time.time()
        return len(tables)

    def tables(self) -> list[TableInfo]:
        """Return cached tables, refreshing first when stale/empty."""
        with self._lock:
            fresh = (
                time.time() - self._loaded_at
            ) < self._ttl and self._tables
        if not fresh:
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - degrade to empty cache
                pass
        with self._lock:
            return list(self._tables)

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {
            t.lower()
            for t in _TOKEN_RE.findall(text)
            if t.lower() not in _STOPWORDS and len(t) > 1
        }

    def search_schema(self, query: str, top_k: int = 5) -> list[dict]:
        """Rank tables/columns against a natural-language query."""
        q_tokens = self._tokens(query)
        if not q_tokens:
            q_tokens = {query.lower()}
        scored: list[tuple[float, TableInfo]] = []
        for table in self.tables():
            t_tokens = self._tokens(table.text())
            if not t_tokens:
                continue
            overlap = len(q_tokens & t_tokens)
            if overlap == 0:
                continue
            score = overlap / (1.0 + 0.05 * len(t_tokens))
            scored.append((score, table))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = []
        for score, table in scored[:top_k]:
            results.append(
                {
                    "table": table.name,
                    "comment": table.comment or table.semantic,
                    "score": round(score, 4),
                    "columns": [
                        {
                            "name": c.name,
                            "type": c.type,
                            "comment": c.comment or c.semantic,
                        }
                        for c in table.columns
                    ],
                },
            )
        return results
