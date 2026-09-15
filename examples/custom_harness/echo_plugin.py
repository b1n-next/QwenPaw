# -*- coding: utf-8 -*-
"""EP-2-16 demo plugin: a third-party agent harness in ~60 lines.

Registers an ``echo`` harness provider through the public plugin API.
The provider shows up in the harness provider list, creates adapters
through the normal workspace runtime, and streams every prompt back as
a completion — a copy-paste starting point for real CLI harnesses
(binary / env / args arrive via the ``settings`` dict).

Install: drop this directory into the QwenPaw plugins directory (or
point the plugin loader at it) and restart; the provider list gains
"Echo Harness (demo)".
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List

from qwenpaw.harnesses.base import HarnessAdapter
from qwenpaw.harnesses.events import (
    HarnessEvent,
    HarnessEventKind,
    HarnessProvider,
)


class EchoAdapter(HarnessAdapter):
    """Echoes each prompt back as a completed turn."""

    def __init__(self, state_dir: Path, settings: Dict[str, Any]) -> None:
        self._state_dir = state_dir
        # Generic config surface (EP-2-16): binary / env / args are the
        # three fields a CLI-backed harness typically needs.
        self._binary = str(settings.get("binary") or "echo")
        self._env = dict(settings.get("env") or {})
        self._args = list(settings.get("args") or [])

    async def status(self) -> HarnessProvider:
        return HarnessProvider(
            id="echo",
            name="Echo Harness",
            available=True,
            installed=True,
            authenticated=True,
            runtime_path=self._binary,
        )

    async def start_login(self, device_code: bool = False) -> Dict[str, Any]:
        return {"ok": True, "message": "echo harness needs no login"}

    async def logout(self) -> None:
        return None

    def run_turn(
        self,
        *,
        session_id: str,
        prompt: str,
        cwd: Path,
        settings: Dict[str, Any],
        attachments: List[Any] | None = None,
    ) -> AsyncIterator[HarnessEvent]:
        async def _stream() -> AsyncIterator[HarnessEvent]:
            yield HarnessEvent(
                kind=HarnessEventKind.TEXT_DELTA,
                item_id=str(uuid.uuid4()),
                text=f"echo: {prompt}",
            )
            yield HarnessEvent(kind=HarnessEventKind.COMPLETED)

        return _stream()

    async def stop(self) -> None:
        return None


def register(api) -> None:  # noqa: ANN001 - PluginApi injected by loader
    """Plugin entry point: add the echo harness to the provider list."""
    from qwenpaw.harnesses.events import HarnessCapabilities

    api.register_harness_provider(
        provider_id="echo",
        adapter_class=EchoAdapter,
        name="Echo Harness",
        capabilities=HarnessCapabilities(),
    )
