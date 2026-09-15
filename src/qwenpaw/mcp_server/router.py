# -*- coding: utf-8 -*-
"""Streamable-HTTP MCP server endpoint (simplified JSON, EP-2-20).

POST /api/mcp-server — one JSON-RPC 2.0 message per request, one JSON
response. Distinct from /api/mcp/* (the pre-existing MCP *client*
management plane); this endpoint makes QwenPaw itself the server an
external MCP host drives over HTTP instead of stdio.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .tools import build_server

router = APIRouter(tags=["mcp"])

_server = build_server()


@router.post("/mcp-server")
async def mcp_endpoint(request: Request) -> JSONResponse:
    """Handle one JSON-RPC message (notification → 202)."""
    payload: Any = await request.json()
    answer = _server.handle_message(payload)
    if answer is None:
        return JSONResponse(status_code=202, content=None)
    return JSONResponse(answer)


__all__ = ["router"]
