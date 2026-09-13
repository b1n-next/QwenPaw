# -*- coding: utf-8 -*-
"""Background usage collector for the hub control plane (EP-1-4)."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

import httpx

from .store import UsageStore

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_WINDOW_DAYS = 35


class UsageCollector:
    """Poll running runtimes for token-usage counters.

    One pass lists running runtimes, fetches each runtime's
    ``/api/token-usage/details`` with its internal token and upserts
    the rows. Failures are logged at debug level and retried on the
    next tick — collection must never disturb the control plane.
    """

    def __init__(
        self,
        *,
        runtime_service: Any,
        credential_vault: Any,
        store: UsageStore,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        window_days: int = DEFAULT_WINDOW_DAYS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._runtime_service = runtime_service
        self._credential_vault = credential_vault
        self._store = store
        self._interval = interval_seconds
        self._window_days = window_days
        self._transport = transport
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_pass_at: str | None = None
        self.last_error: str | None = None

    async def collect_once(self) -> int:
        """Run one collection pass; returns collected row count."""
        records = await asyncio.to_thread(self._runtime_service.list)
        running = [
            record
            for record in records
            if getattr(record, "state", None) == "running"
        ]
        end_d = date.today()
        start_d = end_d - timedelta(days=self._window_days)
        params = {
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
        }
        collected = 0
        had_error = False
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=10.0,
        ) as client:
            for record in running:
                target = (
                    f"http://{record.host}:{record.port}"
                    f"/api/token-usage/details"
                )
                try:
                    token = await asyncio.to_thread(
                        self._credential_vault.get_runtime_secret,
                        tenant_id=record.tenant_id,
                        runtime_id=record.runtime_id,
                        name="QWENPAW_RUNTIME_INTERNAL_TOKEN",
                    )
                except Exception:  # noqa: BLE001
                    token = None
                if not token:
                    continue
                try:
                    response = await client.get(
                        target,
                        params=params,
                        headers={"X-QwenPaw-Runtime-Token": token},
                    )
                    response.raise_for_status()
                    rows = response.json()
                except Exception as exc:  # noqa: BLE001
                    had_error = True
                    self.last_error = f"{record.runtime_id}: {exc}"
                    logger.debug(
                        "usage collection failed for %s: %s",
                        record.runtime_id,
                        exc,
                    )
                    continue
                if isinstance(rows, list) and rows:
                    collected += await asyncio.to_thread(
                        self._store.upsert_rows,
                        record.tenant_id,
                        rows,
                    )
        from datetime import datetime, timezone

        self.last_pass_at = datetime.now(timezone.utc).isoformat()
        if not had_error:
            self.last_error = None
        return collected

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.collect_once()
            except Exception:  # noqa: BLE001
                logger.exception("usage collector pass crashed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(
                self._run(),
                name="qwenpaw-hub-usage-collector",
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
