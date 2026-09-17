# -*- coding: utf-8 -*-
"""Request-scoped trace context for the runtime plane (EP-2-11).

The Hub injects ``X-QwenPaw-Trace-Id`` into every proxied request. This
pure-ASGI middleware mirrors the header into a ``ContextVar`` so that
deep call sites (governance tool-decision audit, driver gates) can tag
their records without threading a parameter through every layer.

Direct (non-hub) access keeps ``None`` traces: the variable is always
reset, so state never leaks across requests.
"""

from __future__ import annotations

from contextvars import ContextVar

_TRACE_HEADER_BYTES = b"x-qwenpaw-trace-id"

trace_id_var: ContextVar[str | None] = ContextVar(
    "qwenpaw_trace_id",
    default=None,
)


def current_trace_id() -> str | None:
    """Return the Hub-issued trace id for the active request, if any."""
    return trace_id_var.get()


class TraceContextMiddleware:
    """Copy the inbound trace header into ``trace_id_var`` per request."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        trace_id: str | None = None
        for name, value in scope.get("headers", []):
            if name == _TRACE_HEADER_BYTES:
                try:
                    candidate = value.decode("latin-1").strip()
                except UnicodeDecodeError:  # pragma: no cover - defensive
                    candidate = ""
                if 0 < len(candidate) <= 64:
                    trace_id = candidate
                break
        token = trace_id_var.set(trace_id)
        try:
            await self.app(scope, receive, send)
        finally:
            trace_id_var.reset(token)
