# -*- coding: utf-8 -*-
"""Agent tool: knowledge base retrieval (EP-2-22)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ...constant import WORKING_DIR
from ...knowledge.embedder import EmbeddingGateway
from ...knowledge.retriever import search
from ...knowledge.store import KnowledgeStore
from ...runtime.tool_registry import tool_descriptor

_database_path: Path = Path(WORKING_DIR) / "knowledge.db"
_store: Optional[KnowledgeStore] = None


def _get_store() -> KnowledgeStore:
    global _store
    if _store is None:
        _store = KnowledgeStore(
            _database_path,
            EmbeddingGateway(None),
        )
    return _store


@tool_descriptor(
    async_execution=True,
    tool_type="internal",
    target_param="query",
    policy_name="KnowledgeSearch",
    default_policy="allow",
    policy_reason="Read-only retrieval over the enterprise knowledge base",
    ui_description="Search the enterprise knowledge base",
    ui_icon="📚",
)
async def knowledge_search(query: str, top_k: int = 5) -> str:
    """Search the knowledge base and return the most relevant passages.

    Use this tool whenever the answer may live in ingested company or
    project documents. Pass a focused natural-language query; results
    carry the source document title and a similarity score.

    Args:
        query: Natural-language search phrase.
        top_k: Maximum number of passages to return (default 5).

    Returns:
        Ranked passages as tool output chunks.
    """
    store = _get_store()
    results = search(store, store.gateway, query, top_k=top_k)
    if not results:
        return "no matching knowledge found"
    lines = []
    for rank, item in enumerate(results, start=1):
        header = f"[{rank}] {item.title} (score {item.score:.3f})"
        lines.append(f"{header}\n{item.text}")
    return "\n\n".join(lines)


__all__ = ["knowledge_search"]
