# -*- coding: utf-8 -*-
"""Knowledge base storage on SQLite (EP-2-22)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from .chunker import chunk_text
from .embedder import EmbeddingGateway, EmbeddingMode

_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_docs (
    doc_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    mime TEXT NOT NULL DEFAULT 'text/plain',
    bytes INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '[]',
    embedding_mode TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    vector TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_doc
    ON knowledge_chunks(doc_id);
"""


class KnowledgeStore:
    """Documents and their embedded chunks (vectors as JSON)."""

    def __init__(self, database_path: Path, gateway: EmbeddingGateway) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._gateway = gateway
        self._init_tables()

    @property
    def gateway(self) -> EmbeddingGateway:
        """Embedding gateway backing this store."""
        return self._gateway

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=30,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _init_tables(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    # ------------------------------------------------------------- writes

    def add_document(
        self,
        title: str,
        text: str,
        *,
        source: str = "",
        mime: str = "text/plain",
        tags: Optional[Sequence[str]] = None,
    ) -> dict:
        """Chunk, embed and store one document."""
        if not title.strip():
            raise ValueError("title is required")
        if not text.strip():
            raise ValueError("text is required")
        chunks = chunk_text(text)
        vectors = self._gateway.embed_batch(chunks)
        mode = self._gateway.mode.value
        doc_id = f"doc-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_docs(
                    doc_id, title, source, mime, bytes, tags,
                    embedding_mode, chunk_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_id,
                    title.strip(),
                    source,
                    mime,
                    len(text.encode("utf-8")),
                    json.dumps(list(tags or []), ensure_ascii=False),
                    mode,
                    len(chunks),
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO knowledge_chunks(
                    chunk_id, doc_id, ordinal, text, vector
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"{doc_id}-c{ordinal}",
                        doc_id,
                        ordinal,
                        chunk,
                        json.dumps(vector),
                    )
                    for ordinal, (chunk, vector) in enumerate(
                        zip(chunks, vectors),
                    )
                ],
            )
        return self.get_document(doc_id) or {}

    def delete_document(self, doc_id: str) -> bool:
        """Remove a document and all its chunks."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT doc_id FROM knowledge_docs WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "DELETE FROM knowledge_chunks WHERE doc_id = ?",
                (doc_id,),
            )
            connection.execute(
                "DELETE FROM knowledge_docs WHERE doc_id = ?",
                (doc_id,),
            )
        return True

    # -------------------------------------------------------------- reads

    def get_document(self, doc_id: str) -> Optional[dict]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_docs WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
        return self._from_row(row) if row else None

    def list_documents(self) -> List[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_docs ORDER BY created_at DESC",
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def all_chunks(self) -> List[dict]:
        """Every chunk with its document metadata (retrieval input)."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.chunk_id, c.doc_id, c.ordinal, c.text, c.vector,
                       d.title, d.source
                FROM knowledge_chunks c
                JOIN knowledge_docs d ON d.doc_id = c.doc_id
                """,
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> dict:
        return {
            "doc_id": row["doc_id"],
            "title": row["title"],
            "source": row["source"],
            "mime": row["mime"],
            "bytes": int(row["bytes"]),
            "tags": json.loads(row["tags"] or "[]"),
            "embedding_mode": row["embedding_mode"],
            "chunk_count": int(row["chunk_count"]),
            "created_at": row["created_at"],
        }


__all__ = ["KnowledgeStore", "EmbeddingMode"]
