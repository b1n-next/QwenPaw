# -*- coding: utf-8 -*-
"""Cosine top-k retrieval over the knowledge store ."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import List, Sequence

from .embedder import EmbeddingGateway


@dataclass(frozen=True)
class RetrievedChunk:
    """One scored chunk handed back to the caller or agent tool."""

    doc_id: str
    title: str
    source: str
    ordinal: int
    text: str
    score: float

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "source": self.source,
            "ordinal": self.ordinal,
            "text": self.text,
            "score": round(self.score, 4),
        }


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


def search(
    store,
    gateway: EmbeddingGateway,
    query: str,
    *,
    top_k: int = 5,
) -> List[RetrievedChunk]:
    """Embed the query and rank every chunk by cosine similarity."""
    if not query.strip():
        return []
    query_vector = gateway.embed(query)
    ranked: List[RetrievedChunk] = []
    for chunk in store.all_chunks():
        try:
            vector = json.loads(chunk["vector"])
        except (TypeError, ValueError):
            continue
        score = _cosine(query_vector, vector)
        ranked.append(
            RetrievedChunk(
                doc_id=chunk["doc_id"],
                title=chunk["title"],
                source=chunk["source"],
                ordinal=int(chunk["ordinal"]),
                text=chunk["text"],
                score=score,
            ),
        )
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[: max(0, top_k)]


__all__ = ["RetrievedChunk", "search"]
