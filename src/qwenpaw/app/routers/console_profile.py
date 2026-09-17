# -*- coding: utf-8 -*-
"""Restricted console profile for direct-runtime deployments (B6, EP-2-9).

Hub deployments answer ``/api/hub/me/permissions`` from the real ACL;
direct-runtime (no hub) deployments historically degrade to the full
menu. Setting ``QWENPAW_CONSOLE_PROFILE=restricted`` on the runtime
makes this router serve the same shape as the hub's user-role
payload, so a browser pointed straight at a runtime still renders
the locked-down console. The env var is the single switch — unset
or ``full`` keeps upstream behavior untouched (404).
"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException, Response

from ...hub.acl.console_map import permissions_payload

router = APIRouter()

_ENV_VAR = "QWENPAW_CONSOLE_PROFILE"


@router.get("/api/console/profile")
async def console_profile() -> Response:
    """Serve the restricted profile when the env var asks for it."""
    profile = os.environ.get(_ENV_VAR, "").strip().lower()
    if profile != "restricted":
        raise HTTPException(
            status_code=404,
            detail={
                "code": "PROFILE_NOT_CONFIGURED",
                "message": (
                    f"Set {_ENV_VAR}=restricted on the runtime to "
                    "enable the locked-down direct-connect console."
                ),
            },
        )
    payload = permissions_payload("user")
    payload["profile"] = "restricted"
    return Response(
        content=json.dumps(payload),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["router"]
