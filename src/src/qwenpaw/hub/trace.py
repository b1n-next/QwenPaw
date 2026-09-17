# -*- coding: utf-8 -*-
"""Cross-plane trace identifiers for the Hub governance bridge (EP-2-11).

The Hub is the only component allowed to mint trace ids. Every proxied
request carries ``X-QwenPaw-Trace-Id`` downstream to the personal runtime
(the runtime mirrors it into its tool-decision audit ``extra``) and back
to the client on the response, so one id replays across:

    hub audit row -> proxied request -> runtime audit row -> response.
"""

from __future__ import annotations

import re
import uuid

TRACE_HEADER = "X-QwenPaw-Trace-Id"

# Hex, 32 chars (uuid4().hex). Tolerant when echo-filtering client input.
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def new_trace_id() -> str:
    """Mint a fresh opaque trace id."""
    return uuid.uuid4().hex


def sanitize_trace_id(value: str | None) -> str | None:
    """Return the value only when it is a well-formed trace id."""
    if not value:
        return None
    candidate = value.strip().lower()
    return candidate if _TRACE_ID_RE.match(candidate) else None


#: W3C traceparent version 00, 32-hex trace id, 16-hex span id.
_TRACEPARENT_RE = re.compile(
    r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$",
)

TRACEPARENT_HEADER = "traceparent"


def sanitize_traceparent(value: str | None) -> str | None:
    """Return a version-00 W3C traceparent only when well-formed.

    Malformed inbound values are dropped (never echoed) per the
    W3C tracecontext rules for proxies.
    """
    if not value:
        return None
    candidate = value.strip()
    return candidate if _TRACEPARENT_RE.match(candidate) else None


def traceparent_from_headers(headers) -> str | None:
    """Extract a valid inbound W3C traceparent, if any (F5)."""
    try:
        return sanitize_traceparent(headers.get(TRACEPARENT_HEADER))
    except Exception:  # pragma: no cover - defensive
        return None


def trace_id_from_traceparent(traceparent: str | None) -> str | None:
    """Map a W3C trace id onto the hub trace id namespace (F5)."""
    if not traceparent:
        return None
    parts = traceparent.split("-")
    return sanitize_trace_id(parts[1]) if len(parts) == 4 else None


def trace_id_from_headers(headers) -> str:
    """Extract a valid incoming trace id or mint a fresh one.

    ``headers`` is any mapping with case-insensitive lookup such as
    ``starlette.datastructures.Headers``.
    """
    incoming = None
    try:
        incoming = headers.get(TRACE_HEADER)
        if not sanitize_trace_id(incoming):
            # F5: fall back to the W3C trace id when the QwenPaw
            # header is absent but tracecontext is present
            incoming = trace_id_from_traceparent(
                traceparent_from_headers(headers) or "",
            )
    except Exception:  # pragma: no cover - defensive, header access varies
        incoming = None
    return sanitize_trace_id(incoming) or new_trace_id()
