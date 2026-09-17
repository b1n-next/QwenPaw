# -*- coding: utf-8 -*-
"""Paragraph-aware text chunking (EP-2-22)."""

from __future__ import annotations

import re
from typing import List

_MAX_CHARS = 1200
_OVERLAP = 150


def chunk_text(text: str, *, max_chars: int = _MAX_CHARS) -> List[str]:
    """Split text into retrieval chunks.

    Paragraphs are kept intact while they fit; oversized paragraphs
    are hard-split with a small overlap so phrases straddling a cut
    stay retrievable. Always returns at least one chunk for
    non-empty input.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    paragraphs = [
        p.strip() for p in re.split(r"\n\s*\n", cleaned) if p.strip()
    ]
    chunks: List[str] = []
    buffer = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.extend(_hard_split(paragraph, max_chars))
            continue
        if buffer and len(buffer) + len(paragraph) + 2 > max_chars:
            chunks.append(buffer)
            buffer = paragraph
        else:
            buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph
    if buffer:
        chunks.append(buffer)
    return chunks


def _hard_split(paragraph: str, max_chars: int) -> List[str]:
    parts: List[str] = []
    start = 0
    while start < len(paragraph):
        end = min(start + max_chars, len(paragraph))
        parts.append(paragraph[start:end])
        if end >= len(paragraph):
            break
        start = end - _OVERLAP if end - _OVERLAP > start else end
    return parts


__all__ = ["chunk_text"]
