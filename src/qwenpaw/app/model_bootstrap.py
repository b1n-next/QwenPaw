# -*- coding: utf-8 -*-
"""Runtime-side model catalog bootstrap (EP-1-2).

Reads ``QWENPAW_MODEL_BOOTSTRAP_JSON`` (injected by the hub at
provision time; see ``qwenpaw.hub.model_catalog``) and registers the
admin-maintained custom providers on the local ProviderManager.

Idempotency rules:
- a provider that already exists locally is left untouched (user
  modifications always win over catalog pushes on restart);
- the catalog default model is activated only when the runtime has no
  active chat model configured yet.

Failures are logged and swallowed: a bad catalog must never block
runtime startup.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from ..providers.provider import ModelInfo, ProviderInfo

logger = logging.getLogger(__name__)

BOOTSTRAP_ENV = "QWENPAW_MODEL_BOOTSTRAP_JSON"


def parse_bootstrap_payload(raw: str | None) -> list[dict[str, Any]]:
    """Parse and minimally validate the bootstrap JSON payload."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("model bootstrap: invalid JSON payload, ignored")
        return []
    if not isinstance(data, list):
        logger.warning("model bootstrap: payload is not a list, ignored")
        return []
    entries: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        provider_id = item.get("id")
        base_url = item.get("base_url")
        if not isinstance(provider_id, str) or not provider_id:
            continue
        if not isinstance(base_url, str) or not base_url:
            continue
        entries.append(item)
    return entries


def _provider_info_from_entry(entry: dict[str, Any]) -> ProviderInfo:
    models = [
        ModelInfo(id=str(model), name=str(model))
        for model in entry.get("models") or []
        if isinstance(model, str) and model
    ]
    return ProviderInfo(
        id=str(entry["id"]),
        name=str(entry.get("name") or entry["id"]),
        base_url=str(entry["base_url"]),
        api_key=str(entry.get("api_key") or ""),
        extra_models=models,
    )


async def apply_model_bootstrap(provider_manager: Any) -> int:
    """Apply the catalog bootstrap; returns applied provider count."""
    entries = parse_bootstrap_payload(os.environ.get(BOOTSTRAP_ENV))
    if not entries:
        return 0
    applied = 0
    for entry in entries:
        provider_id = str(entry["id"])
        try:
            existing = provider_manager.get_provider(provider_id)
            if existing is None:
                await provider_manager.add_custom_provider(
                    _provider_info_from_entry(entry),
                )
                applied += 1
                logger.info(
                    "model bootstrap: registered %s (%d models)",
                    provider_id,
                    len(entry.get("models") or []),
                )
            else:
                logger.debug(
                    "model bootstrap: provider %s already present, "
                    "left untouched",
                    provider_id,
                )
        except Exception:  # noqa: BLE001 - never block startup
            logger.exception(
                "model bootstrap: failed to register provider %s",
                provider_id,
            )
    try:
        if provider_manager.get_active_model() is None:
            for entry in entries:
                default_model = entry.get("default_model")
                provider_id = str(entry["id"])
                if not default_model:
                    continue
                if default_model not in (entry.get("models") or []):
                    continue
                await provider_manager.activate_model(
                    provider_id,
                    str(default_model),
                )
                logger.info(
                    "model bootstrap: activated default model %s/%s",
                    provider_id,
                    default_model,
                )
                break
    except Exception:  # noqa: BLE001 - never block startup
        logger.exception("model bootstrap: failed to activate default")
    return applied
