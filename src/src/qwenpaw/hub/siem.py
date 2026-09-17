# -*- coding: utf-8 -*-
"""SIEM/log shipping for hub audit events (F9, 02 §8).

Zero-dependency relay: every audited event is mirrored to a
configurable webhook as a JSONL batch, fire-and-forget from the
request path. Delivery failures back off and are counted in
metrics; nothing about the hub's own audit chain (H2) depends on
the relay being up.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class SiemRelay:
    """Best-effort audit event relay to one SIEM webhook (F9)."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        *,
        secret_header: str = "X-QwenPaw-Siem-Key",
        secret: str = "",
        batch_size: int = 50,
        flush_interval: float = 5.0,
        timeout: float = 5.0,
        transport: Any = None,
    ) -> None:
        self._endpoint = endpoint
        self._secret_header = secret_header
        self._secret = secret
        self._batch_size = max(1, batch_size)
        self._flush_interval = flush_interval
        self._timeout = timeout
        self._transport = transport
        self._lock = threading.Lock()
        self._queue: List[Dict[str, Any]] = []
        self._last_flush = time.monotonic()
        self._last_error: str | None = None
        self._sent = 0
        self._dropped = 0

    # -------------------------------------------------- config

    def configure(
        self,
        endpoint: Optional[str],
        *,
        secret: str = "",
        batch_size: int = 50,
        flush_interval: float = 5.0,
    ) -> None:
        """Hot-apply relay settings (admin-managed)."""
        with self._lock:
            self._endpoint = endpoint
            self._secret = secret
            self._batch_size = max(1, batch_size)
            self._flush_interval = flush_interval
            self._last_error = None

    @property
    def endpoint(self) -> Optional[str]:
        return self._endpoint

    # -------------------------------------------------- shipping

    def enqueue(self, event: Dict[str, Any]) -> None:
        """Queue one audit event; flush when the batch is full."""
        if self._endpoint is None:
            return
        due = False
        with self._lock:
            self._queue.append(event)
            if (
                len(self._queue) >= self._batch_size
                or time.monotonic() - self._last_flush >= self._flush_interval
            ):
                due = True
        if due:
            self.flush()

    def flush(self) -> int:
        """Ship queued events; returns rows attempted."""
        with self._lock:
            batch = self._queue[: self._batch_size]
            del self._queue[: len(batch)]
            self._last_flush = time.monotonic()
        if not batch or self._endpoint is None:
            return 0
        headers = {"Content-Type": "application/x-ndjson"}
        if self._secret:
            headers[self._secret_header] = self._secret
        payload = "".join(
            json.dumps(row, ensure_ascii=False) + "\n" for row in batch
        )
        try:
            with httpx.Client(transport=self._transport) as client:
                response = client.post(
                    self._endpoint,
                    content=payload.encode("utf-8"),
                    headers=headers,
                    timeout=self._timeout,
                )
                response.raise_for_status()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            self._dropped += len(batch)
            self._last_error = str(exc)[:200]
            logger.warning(
                "SIEM relay delivery failed (%s rows dropped): %s",
                len(batch),
                exc,
            )
            return 0
        self._sent += len(batch)
        self._last_error = None
        return len(batch)

    # -------------------------------------------------- status

    def stats(self) -> Dict[str, Any]:
        """Relay health for the admin plane."""
        with self._lock:
            return {
                "endpoint": self._endpoint,
                "queued": len(self._queue),
                "sent": self._sent,
                "dropped": self._dropped,
                "last_error": self._last_error,
            }


__all__ = ["SiemRelay"]
