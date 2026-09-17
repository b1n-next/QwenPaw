# -*- coding: utf-8 -*-
"""Stdio MCP server entry: ``python -m qwenpaw.mcp_server``.

Reads newline-delimited JSON-RPC 2.0 from stdin and writes one JSON
response per line to stdout (notifications produce no output) — the
shape Claude Desktop and other MCP hosts expect for stdio servers.
"""

from __future__ import annotations

import sys
import logging

from .tools import build_server


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
    )
    server = build_server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        answer = server.handle_raw(line)
        if answer is not None:
            sys.stdout.write(answer + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
