# -*- coding: utf-8 -*-
"""Knowledge base HTTP API : /api/knowledge."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from ...agents.memory.embedding_model import EmbeddingModelConfig
from ...constant import WORKING_DIR
from ...knowledge.embedder import EmbeddingGateway
from ...knowledge.retriever import search
from ...knowledge.store import KnowledgeStore

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

_database_path: Path = Path(WORKING_DIR) / "knowledge.db"
_store: Optional[KnowledgeStore] = None
_store_lock = Lock()


def _get_store() -> KnowledgeStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = KnowledgeStore(
                _database_path,
                EmbeddingGateway(EmbeddingModelConfig()),
            )
        return _store


@router.post("/documents")
async def add_document(
    title: str = Form(...),
    text: str = Form(...),
    source: str = Form(""),
    tags: str = Form(""),
) -> dict:
    """Ingest one text document (chunk → embed → store)."""
    tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]
    try:
        document = await run_in_threadpool(
            _get_store().add_document,
            title,
            text,
            source=source,
            tags=tag_list,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_DOCUMENT", "message": str(exc)},
        ) from None
    return {"document": document}


@router.post("/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    tags: str = Form(""),
) -> dict:
    """Upload a text-ish file and ingest it."""
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNSUPPORTED_ENCODING",
                "message": "only UTF-8 text files are supported",
            },
        ) from None
    resolved_title = (title or file.filename or "untitled").strip()
    tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]
    document = await run_in_threadpool(
        _get_store().add_document,
        resolved_title,
        text,
        source=file.filename or "",
        mime=file.content_type or "text/plain",
        tags=tag_list,
    )
    return {"document": document}


@router.get("/documents")
async def list_documents() -> dict:
    """List ingested documents (metadata only)."""
    documents = await run_in_threadpool(_get_store().list_documents)
    return {"documents": documents}


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str) -> dict:
    """Remove a document and its chunks."""
    removed = await run_in_threadpool(
        _get_store().delete_document,
        doc_id,
    )
    if not removed:
        raise HTTPException(
            status_code=404,
            detail={"code": "DOC_NOT_FOUND", "message": "unknown doc_id"},
        )
    return {"deleted": doc_id}


@router.post("/search")
async def search_documents(
    query: str = Form(...),
    top_k: int = Form(5),
) -> dict:
    """Cosine top-k retrieval across every chunk."""
    store = _get_store()
    results = await run_in_threadpool(
        search,
        store,
        store.gateway,
        query,
        top_k=top_k,
    )
    return {
        "results": [item.as_dict() for item in results],
        "embedding_mode": store.gateway.mode.value,
    }


__all__ = ["router"]
