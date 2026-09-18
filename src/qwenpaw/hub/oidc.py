# -*- coding: utf-8 -*-
"""OIDC SSO client for the hub (05 §3, EP-2-2).

Authorization-code flow with the userinfo backchannel: claims are
fetched server-to-server with the access token, so no JWT
signature verification is needed (TLS to the IdP plus the
client_secret exchange carry the trust). IdP-side account state is
authoritative on every login — disabled locally or at the IdP
means no session. Group claims sync into local ``source='oidc'``
groups on each login (full reset, so IdP removals propagate).
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx


class OidcError(RuntimeError):
    """OIDC flow failure with a stable code for HTTP mapping."""


@dataclass(frozen=True)
class OidcSettings:
    """Admin-managed OIDC configuration (hub settings payload)."""

    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""
    username_claim: str = "preferred_username"
    groups_claim: str = "groups"
    display_name_claim: str = "name"

    @property
    def enabled(self) -> bool:
        return bool(self.issuer and self.client_id)


@dataclass(frozen=True)
class OidcIdentity:
    """Normalized identity resolved from one callback."""

    username: str
    display_name: str
    groups: Tuple[str, ...]


_STATE_TTL_SECONDS = 600


@dataclass
class _StateEntry:
    next_path: str
    created_at: float


class OidcClient:
    """One IdP connection: discovery cache + state registry."""

    def __init__(
        self,
        settings: OidcSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._discovered_at: float = -(1 << 30)
        self._endpoints: Dict[str, str] = {}
        self._states: Dict[str, _StateEntry] = {}

    @property
    def settings(self) -> OidcSettings:
        """Read-only view for diagnostics and tests (A4)."""
        return self._settings

    # -------------------------------------------------- config

    def authorization_url(self, redirect_uri: str, next_path: str) -> str:
        """Build the IdP login URL and register CSRF state."""
        endpoints = self._cached_endpoints()
        auth_endpoint = endpoints.get("authorization_endpoint")
        if not auth_endpoint:
            raise OidcError("oidc_not_configured")
        state = secrets.token_urlsafe(24)
        now = self._clock()
        with self._lock:
            self._gc_states(now)
            self._states[state] = _StateEntry(next_path, now)
        params = urlencode(
            {
                "response_type": "code",
                "client_id": self._settings.client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": "openid profile",
            },
        )
        return f"{auth_endpoint}?{params}"

    def consume_state(self, state: str) -> Optional[str]:
        """Pop one CSRF state; returns its next path or None."""
        with self._lock:
            entry = self._states.pop(state, None)
        if entry is None:
            return None
        if self._clock() - entry.created_at > _STATE_TTL_SECONDS:
            return None
        return entry.next_path

    def _gc_states(self, now: float) -> None:
        stale = [
            state
            for state, entry in self._states.items()
            if now - entry.created_at > _STATE_TTL_SECONDS
        ]
        for state in stale:
            del self._states[state]

    # -------------------------------------------------- discovery

    def _cached_endpoints(self) -> Dict[str, str]:
        now = self._clock()
        with self._lock:
            if self._endpoints and now - self._discovered_at < 3600:
                return dict(self._endpoints)
        endpoints = self._discover()
        with self._lock:
            self._endpoints = dict(endpoints)
            self._discovered_at = now
        return dict(endpoints)

    def _discover(self) -> Dict[str, str]:
        url = self._settings.issuer.rstrip("/") + (
            "/.well-known/openid-configuration"
        )
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=5.0,
            ) as client:
                response = client.get(url)
        except httpx.HTTPError as exc:
            raise OidcError("oidc_discovery_failed") from exc
        if response.status_code != 200:
            raise OidcError("oidc_discovery_failed")
        document = response.json()
        endpoints = {
            key: str(document.get(key) or "")
            for key in (
                "authorization_endpoint",
                "token_endpoint",
                "userinfo_endpoint",
            )
        }
        if not all(endpoints.values()):
            raise OidcError("oidc_discovery_incomplete")
        return endpoints

    # -------------------------------------------------- callback

    async def exchange_and_resolve(
        self,
        code: str,
        redirect_uri: str,
    ) -> OidcIdentity:
        """Exchange the code and resolve the identity via userinfo."""
        endpoints = self._cached_endpoints()
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=10.0,
            ) as client:
                token_response = await client.post(
                    endpoints["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "client_id": self._settings.client_id,
                        "client_secret": self._settings.client_secret,
                    },
                )
                if token_response.status_code != 200:
                    raise OidcError("oidc_exchange_failed")
                access_token = str(
                    token_response.json().get("access_token") or "",
                )
                if not access_token:
                    raise OidcError("oidc_exchange_failed")
                userinfo_response = await client.get(
                    endpoints["userinfo_endpoint"],
                    headers={
                        "Authorization": f"Bearer {access_token}",
                    },
                )
                if userinfo_response.status_code != 200:
                    raise OidcError("oidc_userinfo_failed")
                claims = userinfo_response.json()
        except httpx.HTTPError as exc:
            raise OidcError("oidc_unreachable") from exc
        return self._identity_from_claims(claims)

    def _identity_from_claims(self, claims: Dict[str, Any]) -> OidcIdentity:
        username = str(
            claims.get(self._settings.username_claim) or "",
        ).strip()
        if not username or ":" in username or len(username) > 128:
            raise OidcError("oidc_username_invalid")
        display_name = str(
            claims.get(self._settings.display_name_claim) or "",
        ).strip()
        raw_groups = claims.get(self._settings.groups_claim) or []
        if isinstance(raw_groups, str):
            raw_groups = [raw_groups]
        groups = tuple(
            name
            for name in (
                str(group).strip()
                for group in raw_groups
                if isinstance(raw_groups, list)
            )
            if name and ":" not in name and len(name) <= 128
        )
        return OidcIdentity(
            username=username,
            display_name=display_name,
            groups=groups,
        )


__all__ = [
    "OidcClient",
    "OidcError",
    "OidcIdentity",
    "OidcSettings",
]
