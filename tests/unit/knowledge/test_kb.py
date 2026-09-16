# -*- coding: utf-8 -*-
"""knowledge base — chunker, embedder, store, API, tool."""

from __future__ import annotations

# pylint: disable=protected-access

import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwenpaw.agents.tools import discover_builtin_tool_funcs
from qwenpaw.app.routers import knowledge as knowledge_api
from qwenpaw.app._app import app
from qwenpaw.knowledge.chunker import chunk_text
from qwenpaw.knowledge.embedder import EmbeddingGateway, hash_embed
from qwenpaw.knowledge.retriever import search
from qwenpaw.knowledge.store import KnowledgeStore


@pytest.fixture(name="store")
def _store(tmp_path: Path):
    return KnowledgeStore(
        tmp_path / "knowledge.db",
        EmbeddingGateway(None),
    )


@pytest.fixture(name="client")
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        knowledge_api,
        "_database_path",
        tmp_path / "knowledge.db",
    )
    monkeypatch.setattr(knowledge_api, "_store", None)
    with TestClient(app) as test_client:
        yield test_client


# ------------------------------------------------------------ chunker


def test_chunker_keeps_paragraphs_together() -> None:
    text = "\n\n".join(f"paragraph {i} " + "x" * 80 for i in range(20))
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1200 for chunk in chunks)
    # no paragraph got torn between fitting chunks
    assert "paragraph 0" in chunks[0]


def test_chunker_hard_splits_oversized_paragraph() -> None:
    text = "y" * 3000
    chunks = chunk_text(text)
    assert len(chunks) >= 3
    assert "".join(chunks).count("y") >= 3000
    # overlap preserves boundary text
    assert chunks[1][:100] in "y" * 3000


def test_chunker_empty() -> None:
    assert not chunk_text("")
    assert not chunk_text("   \n\n  ")


# ----------------------------------------------------------- embedder


def test_hash_embed_deterministic_and_normalized() -> None:
    first = hash_embed("vacation policy days")
    second = hash_embed("vacation policy days")
    assert first == second
    norm = math.sqrt(sum(value * value for value in first))
    assert abs(norm - 1.0) < 1e-9


def test_gateway_reports_hash_mode_without_config() -> None:
    gateway = EmbeddingGateway(None)
    assert gateway.mode.value == "hash"
    assert len(gateway.embed_batch(["a", "b"])) == 2


# -------------------------------------------------------------- store


def test_store_add_list_delete(store: KnowledgeStore) -> None:
    document = store.add_document(
        "Handbook",
        "Vacation policy: 15 days per year.",
        source="handbook.md",
        tags=["hr"],
    )
    assert document["chunk_count"] == 1
    assert document["embedding_mode"] == "hash"
    assert document["tags"] == ["hr"]

    listed = store.list_documents()
    assert [item["doc_id"] for item in listed] == [document["doc_id"]]

    assert store.delete_document(document["doc_id"]) is True
    assert store.list_documents() == []
    assert store.delete_document("ghost") is False


def test_store_rejects_empty_inputs(store: KnowledgeStore) -> None:
    with pytest.raises(ValueError):
        store.add_document("   ", "text")
    with pytest.raises(ValueError):
        store.add_document("t", "   ")


# ----------------------------------------------------------- retrieval


def test_search_ranks_matching_chunk_first(store: KnowledgeStore) -> None:
    store.add_document(
        "Handbook",
        "Vacation policy: 15 days per year. Sick leave is unlimited.",
    )
    store.add_document(
        "Runbook",
        "Restart the cache nodes after every deploy window.",
    )
    results = search(
        store,
        store.gateway,
        "how many vacation days do I get",
        top_k=2,
    )
    assert results
    assert results[0].title == "Handbook"
    assert "Vacation policy" in results[0].text
    assert results[0].score >= results[-1].score


# ---------------------------------------------------------------- api


def test_api_add_search_delete_flow(client: TestClient) -> None:
    added = client.post(
        "/api/knowledge/documents",
        data={"title": "Handbook", "text": "Q1 OKR: ship the graph engine."},
    )
    assert added.status_code == 200
    doc_id = added.json()["document"]["doc_id"]

    listed = client.get("/api/knowledge/documents").json()["documents"]
    assert [item["doc_id"] for item in listed] == [doc_id]

    found = client.post(
        "/api/knowledge/search",
        data={"query": "OKR graph engine", "top_k": 3},
    ).json()
    assert found["embedding_mode"] == "hash"
    assert found["results"][0]["doc_id"] == doc_id

    deleted = client.delete(f"/api/knowledge/documents/{doc_id}")
    assert deleted.status_code == 200
    assert client.get("/api/knowledge/documents").json()["documents"] == []
    assert (
        client.delete(f"/api/knowledge/documents/{doc_id}").status_code == 404
    )


def test_api_rejects_blank_document(client: TestClient) -> None:
    response = client.post(
        "/api/knowledge/documents",
        data={"title": "  ", "text": "x"},
    )
    assert response.status_code == 422


def test_api_upload_file(client: TestClient) -> None:
    uploaded = client.post(
        "/api/knowledge/documents/upload",
        files={
            "file": (
                "notes.md",
                b"# Notes\ntravel budget 2025",
                "text/markdown",
            ),
        },
        data={"title": "Notes", "tags": "team, misc"},
    )
    assert uploaded.status_code == 200
    document = uploaded.json()["document"]
    assert document["source"] == "notes.md"
    assert document["tags"] == ["team", "misc"]
    hits = client.post(
        "/api/knowledge/search",
        data={"query": "travel budget", "top_k": 1},
    ).json()["results"]
    assert hits[0]["doc_id"] == document["doc_id"]


# --------------------------------------------------------------- tool


def test_knowledge_tool_registered() -> None:
    tools = discover_builtin_tool_funcs()
    names = {tool.__name__ for tool in tools}
    assert "knowledge_search" in names
