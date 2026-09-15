# -*- coding: utf-8 -*-
"""Minimal MCP JSON-RPC 2.0 server core (EP-2-20)."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"


class JSONRPCRequest(BaseModel):
    """One JSON-RPC 2.0 request (MCP carries these per transport)."""

    jsonrpc: str = "2.0"
    id: Optional[Any] = None
    method: str
    params: Dict[str, Any] = Field(default_factory=dict)


class MCPError(Exception):
    """JSON-RPC error surfaced to the MCP host."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MCPServer:
    """Method dispatch shared by the stdio and HTTP transports."""

    SERVER_INFO = {"name": "qwenpaw", "version": "1.0.0"}

    def __init__(
        self,
        tool_handlers: Dict[str, Callable[[Dict[str, Any]], Any]],
        server_info: Optional[Dict[str, str]] = None,
    ) -> None:
        self._tool_handlers = tool_handlers
        self._server_info = server_info or dict(self.SERVER_INFO)
        self._tool_descriptions: list[dict[str, Any]] = []

    def describe_tools(
        self,
        descriptions: list[dict[str, Any]],
    ) -> None:
        """Register the tools/list payload (schemas come from tools.py)."""
        self._tool_descriptions = descriptions

    # ------------------------------------------------------- dispatch

    def handle_raw(self, raw: str) -> Optional[str]:
        """Handle one raw JSON line; None for notifications."""
        try:
            payload = json.loads(raw)
        except ValueError:
            response = self._error_response(None, -32700, "Parse error")
            return json.dumps(response, ensure_ascii=False)
        answer = self.handle_message(payload)
        if answer is None:
            return None
        return json.dumps(answer, ensure_ascii=False)

    def handle_message(  # pylint: disable=too-many-return-statements
        self,
        payload: Any,
    ) -> Optional[dict[str, Any]]:
        """Handle one decoded message; None for notifications."""
        if not isinstance(payload, dict):
            return self._error_response(None, -32600, "Invalid Request")
        request_id = payload.get("id")
        method = str(payload.get("method") or "")
        is_notification = "id" not in payload
        try:
            result = self._dispatch(method, payload.get("params") or {})
        except MCPError as exc:
            if is_notification:
                return None
            return self._error_response(
                request_id,
                exc.code,
                exc.message,
            )
        except Exception as exc:  # noqa: BLE001 - protocol boundary
            logger.exception("MCP handler failed for %s", method)
            if is_notification:
                return None
            return self._error_response(
                request_id,
                -32603,
                f"Internal error: {exc}",
            )
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str, params: Dict[str, Any]) -> Any:
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": self._server_info,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self._tool_descriptions}
        if method == "tools/call":
            name = str(params.get("name") or "")
            handler = self._tool_handlers.get(name)
            if handler is None:
                raise MCPError(-32602, f"Unknown tool: {name}")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise MCPError(-32602, "arguments must be an object")
            return handler(arguments)
        raise MCPError(-32601, f"Method not found: {method}")

    @staticmethod
    def _error_response(
        request_id: Any,
        code: int,
        message: str,
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


__all__ = ["JSONRPCRequest", "MCPError", "MCPServer", "PROTOCOL_VERSION"]
