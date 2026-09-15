# -*- coding: utf-8 -*-
"""Expose QwenPaw agents and graphs as an MCP server (EP-2-20).

Transports:
    stdio  — ``python -m qwenpaw.mcp_server`` (Claude Desktop et al.)
    http   — POST /api/mcp on the runtime app (JSON-RPC per request)

Tools:
    agent_chat_submit / agent_task_status — asynchronous agent turns
    graph_run / graph_resume — EP-2-17 graph execution with human-gate
    suspension surfaced as an MCP-visible pending state
"""

from .protocol import MCPError, MCPServer, JSONRPCRequest
from .tools import build_server

__all__ = ["MCPError", "MCPServer", "JSONRPCRequest", "build_server"]
