# -*- coding: utf-8 -*-
"""Bounded model forwarding with cancellation-safe resource ownership."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from ...utils.io_utils import run_sync_io
from .request import GatewayRequest, GatewayStreamingResponse
from .limiter import SharedLimiter
from .provider_setup import provider_headers
from .protocol import (
    safe_payload,
    upstream_payload,
    usage_tokens,
)

_TIMEOUT = 120


class ModelGateway:
    """Keep upstream keys, request attribution, and settlement in Hub."""

    def __init__(self, catalog, budgets, transport=None):
        self.catalog = catalog
        self.budgets = budgets
        self.transport = transport
        # fork E5 tests construct without a backing store; production
        # always passes one and gets the shared limiter as before.
        store = getattr(catalog, "store", None)
        self.limiter = SharedLimiter(store) if store is not None else None

    def recover(self):
        """Wait out attempts that might still be running after a restart."""
        with self.catalog.store.connect() as db:
            orphan = db.execute(
                "SELECT 1 FROM hub_model_requests "
                "WHERE status = 'dispatched' LIMIT 1",
            ).fetchone()
        self.budgets.recover()
        if orphan:
            self.limiter.cooldown_until = time.monotonic() + 2 * _TIMEOUT

    async def _open(self, attempt, model, connection, payload):
        stack = attempt.stack
        if self.limiter is not None:
            await stack.enter_async_context(
                self.limiter.acquire(model, connection),
            )
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=self.transport,
                follow_redirects=False,
                timeout=httpx.Timeout(_TIMEOUT, connect=10),
                trust_env=False,
            ),
        )
        key = await run_sync_io(self.catalog.key, connection)
        request = client.build_request(
            "POST",
            f"{connection['base_url']}/chat/completions",
            headers={
                **provider_headers(connection),
                "Authorization": f"Bearer {key}",
            },
            json=payload,
        )
        await attempt.dispatch()
        response = await asyncio.wait_for(
            client.send(request, stream=True),
            timeout=_TIMEOUT,
        )
        stack.push_async_callback(response.aclose)
        if response.status_code != 200:
            raise HTTPException(
                502,
                f"hub_upstream_error:{attempt.request_id}",
            )
        return response

    async def call(  # pylint: disable=too-many-branches
        self,
        identity,
        body,
        *,
        admin_test=False,
    ):
        """Reserve once and transfer resource ownership to the response."""
        # E5 failover (fork): on upstream failure walk the admin-defined
        # fallback chain before surfacing 502. Each hop is a full
        # reserve+open so budgets meter per model actually used; a
        # seen-set guards against cross-model cycles (A->B->A).
        chain = []
        if self.fallbacks_for is not None:
            requested = str(body.get("model") or "")
            try:
                chain = list(self.fallbacks_for(requested) or [])
            except Exception:  # noqa: BLE001 - fail-open to direct path
                chain = []
        attempts: list[tuple[GatewayRequest, object, str]] = []
        candidates = [None]
        seen = {str(body.get("model") or "")}
        for model_id in chain:
            if model_id and model_id not in seen:
                seen.add(model_id)
                candidates.append(model_id)
        last_error: HTTPException | None = None
        used_model = None
        response = None
        attempt = None
        for index, fallback_model in enumerate(candidates):
            attempt = GatewayRequest(self.budgets, self.catalog)
            try:
                payload = dict(body)
                if fallback_model is not None:
                    payload["model"] = fallback_model
                _, model, connection, cap = await attempt.reserve(
                    identity,
                    payload,
                    admin_test=admin_test,
                )
                opened = await self._open(
                    attempt,
                    model,
                    connection,
                    upstream_payload(payload, model, cap, connection),
                )
            except BaseException as exc:  # noqa: BLE001 - try next hop
                await attempt.close(error="upstream_failed")
                last_error = (
                    exc
                    if isinstance(exc, HTTPException)
                    else HTTPException(
                        502,
                        f"hub_upstream_error:{attempt.request_id}",
                    )
                )
                if isinstance(exc, asyncio.CancelledError):
                    raise
                continue
            attempts.append((attempt, opened, model))
            used_model = str(model.get("upstream_model") or model)
            response = opened
            if index > 0 and self.on_fallback is not None:
                try:
                    self.on_fallback(
                        str(body.get("model") or ""),
                        used_model,
                        attempt.request_id,
                    )
                except Exception:  # noqa: BLE001 - audit must not break
                    pass
            break
        if response is None:
            raise last_error or HTTPException(502, "hub_upstream_error")
        attempt, response, _ = attempts[0]
        fallback_header = {}
        if used_model is not None and str(
            body.get("model") or "",
        ) != str(used_model):
            fallback_header[
                "X-QwenPaw-Fallback"
            ] = f"{body.get('model')}->{used_model}"
        reported_model = str(used_model or body.get("model") or "")
        if body.get("stream", False):
            return GatewayStreamingResponse(
                self._stream(response, attempt, reported_model),
                attempt,
                media_type="text/event-stream",
                headers={
                    "X-Request-ID": attempt.request_id,
                    "Cache-Control": "no-store",
                    **fallback_header,
                },
            )
        return await self._complete(
            response,
            attempt,
            reported_model,
            extra_headers=fallback_header,
        )

    async def _complete(self, response, attempt, model_id, extra_headers=None):
        actual = None
        error = "response_incomplete"
        try:
            raw = bytearray()
            async with asyncio.timeout(_TIMEOUT):
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 16 * 1024 * 1024:
                        raise ValueError("Upstream response too large")
            data = json.loads(raw)
            result = safe_payload(data, model_id)
            actual = usage_tokens(data)
            error = None
            return JSONResponse(
                result,
                headers={
                    "X-Request-ID": attempt.request_id,
                    **(extra_headers or {}),
                },
            )
        except Exception:
            raise HTTPException(
                502,
                f"hub_upstream_error:{attempt.request_id}",
            ) from None
        finally:
            await attempt.close(actual, error)

    async def _stream(self, response, attempt, model_id):
        actual = None
        complete = False
        try:
            async with asyncio.timeout(_TIMEOUT):
                async for line in response.aiter_lines():
                    if len(line) > 4 * 1024 * 1024:
                        raise ValueError("Stream event too large")
                    if not line.startswith("data:"):
                        continue
                    text = line[5:].strip()
                    if text == "[DONE]":
                        complete = True
                        yield b"data: [DONE]\n\n"
                        break
                    data = json.loads(text)
                    found = usage_tokens(data)
                    if found is not None:
                        actual = found
                    event = json.dumps(safe_payload(data, model_id))
                    yield f"data: {event}\n\n".encode()
        except Exception:
            event = json.dumps(
                {
                    "error": {
                        "code": "hub_upstream_error",
                        "request_id": attempt.request_id,
                    },
                },
            )
            yield f"data: {event}\n\n".encode()
        finally:
            await attempt.close(
                actual if complete else None,
                None if complete else "stream_incomplete",
            )
