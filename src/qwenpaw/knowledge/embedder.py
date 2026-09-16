# -*- coding: utf-8 -*-
"""Embedding gateway with an offline deterministic fallback .

Primary path: the agentscope embedding model configured in
EmbeddingModelConfig (real provider credentials).
Fallback: hash embedding — tokens are SHA-256 bucketed into a fixed
dense vector, so cosine similarity still rewards shared vocabulary.
The active mode is reported so callers never mistake the fallback
for semantic quality.
"""

from __future__ import annotations

import hashlib
import math
import re
from enum import Enum
from typing import List, Sequence

_HASH_DIMENSIONS = 512


class EmbeddingMode(str, Enum):
    """Which embedding engine produced the vectors."""

    MODEL = "model"
    HASH = "hash"


_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def _tokens(text: str) -> List[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text or "")]


def hash_embed(text: str) -> List[float]:
    """Deterministic token-hash dense vector (unit length)."""
    vector = [0.0] * _HASH_DIMENSIONS
    tokens = _tokens(text)
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % _HASH_DIMENSIONS
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


class EmbeddingGateway:
    """Try the configured real model; fall back to hash embedding."""

    def __init__(self, config=None) -> None:  # config: EmbeddingModelConfig
        self._config = config

    @property
    def mode(self) -> EmbeddingMode:
        """Report which engine a call would use right now."""
        if self._try_model() is not None:
            return EmbeddingMode.MODEL
        return EmbeddingMode.HASH

    def _try_model(self):
        if self._config is None:
            return None
        try:
            from ..agents.memory.embedding_model import (
                create_embedding_model,
                _is_embedding_enabled,  # noqa: PLC2701
            )
        except ImportError:
            return None
        try:
            if not _is_embedding_enabled(self._config):
                return None
            return create_embedding_model(self._config)
        except Exception:  # noqa: BLE001 - offline config is normal
            return None

    def embed(self, text: str) -> List[float]:
        """Embed one text (model if reachable, hash otherwise)."""
        model = self._try_model()
        if model is None:
            return hash_embed(text)
        try:
            results = model([text])
            vector = list(map(float, results[0]))
            return vector
        except Exception:  # noqa: BLE001 - provider outage → fallback
            return hash_embed(text)

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed many texts through the same engine choice."""
        if not texts:
            return []
        model = self._try_model()
        if model is None:
            return [hash_embed(text) for text in texts]
        try:
            results = model(list(texts))
            return [list(map(float, item)) for item in results]
        except Exception:  # noqa: BLE001 - provider outage → fallback
            return [hash_embed(text) for text in texts]


__all__ = ["EmbeddingGateway", "EmbeddingMode", "hash_embed"]
