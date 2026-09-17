# -*- coding: utf-8 -*-
"""Knowledge base for QwenPaw (EP-2-22).

Documents are chunked, embedded and stored in SQLite (vectors as
JSON float arrays — no external vector store dependency); retrieval
is in-process cosine top-k. Embeddings prefer the configured real
model and fall back to a deterministic hash embedding offline, so
the whole loop is exercisable without network access.
"""

from .chunker import chunk_text
from .embedder import EmbeddingGateway, EmbeddingMode
from .retriever import RetrievedChunk, search
from .store import KnowledgeStore

__all__ = [
    "chunk_text",
    "EmbeddingGateway",
    "EmbeddingMode",
    "RetrievedChunk",
    "search",
    "KnowledgeStore",
]
