# -*- coding: utf-8 -*-
"""Core A2A 1.0 client (EP-2-21).

The canonical client every QwenPaw-side integration (cloudpaw
plugins, hub bridges, examples) reuses: discovery through
message/send / message/stream against any standard A2A server.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, Optional

import httpx

_USER_AGENT = "qwenpaw-a2a-client/1.0"


def discover_agent_card(
    base_url: str,
    *,
    timeout: float = 10.0,
    transport: Optional[httpx.BaseTransport] = None,
) -> Dict[str, Any]:
    """Fetch /.well-known/agent-card.json from an A2A server."""
    url = f"{base_url.rstrip('/')}/.well-known/agent-card.json"
    with httpx.Client(
        transport=transport,
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


class A2AClient:
    """Thin JSON-RPC client for one A2A server."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 60.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._endpoint = f"{base_url.rstrip('/')}/api/a2a"
        self._timeout = timeout
        self._transport = transport

    def _post(
        self,
        method: str,
        params: Dict[str, Any],
        request_id: int,
    ) -> Dict[str, Any]:
        with httpx.Client(
            transport=self._transport,
            timeout=self._timeout,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            response = client.post(
                self._endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
            )
            response.raise_for_status()
            payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(
                f"A2A error {payload['error'].get('code')}: "
                f"{payload['error'].get('message')}",
            )
        return (payload or {}).get("result") or {}

    # ------------------------------------------------------------ api

    def send_message(
        self,
        text: str,
        *,
        agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """message/send — completed Message or working Task payload."""
        message: Dict[str, Any] = {
            "role": "user",
            "messageId": f"msg-{len(text)}",
            "parts": [{"kind": "text", "text": text}],
        }
        if agent_id:
            message["agent_id"] = agent_id
        return self._post("message/send", {"message": message}, 1)

    def get_task(self, task_id: str) -> Dict[str, Any]:
        """tasks/get — current Task payload."""
        return self._post("tasks/get", {"id": task_id}, 2)

    def stream_message(
        self,
        text: str,
        *,
        agent_id: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        """message/stream — yield SSE events until the final one."""
        message: Dict[str, Any] = {
            "role": "user",
            "messageId": f"msg-{len(text)}",
            "parts": [{"kind": "text", "text": text}],
        }
        if agent_id:
            message["agent_id"] = agent_id
        with httpx.Client(
            transport=self._transport,
            timeout=self._timeout,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            with client.stream(
                "POST",
                self._endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "message/stream",
                    "params": {"message": message},
                },
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[len("data: ") :].strip()
                    if data == "[DONE]":
                        return
                    try:
                        yield json.loads(data)
                    except ValueError:
                        continue


__all__ = ["A2AClient", "discover_agent_card"]
