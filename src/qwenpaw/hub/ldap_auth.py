# -*- coding: utf-8 -*-
"""C4: LDAP direct-bind authentication (optional dependency).

The hub keeps local accounts as the source of truth for roles and
tokens; LDAP only vouches for the password. ``ldap3`` is imported
lazily so deployments without LDAP carry zero new dependencies —
enabling the integration simply installs ``ldap3`` and drops a
``ldap.json`` next to the hub database.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_FILTER = "(uid={login})"
DEFAULT_FILTER_MAIL = "(mail={login})"


@dataclass(frozen=True)
class LdapSettings:
    """One LDAP directory the hub trusts (C4)."""

    url: str  # ldap://host:389 | ldaps://host:636
    base_dn: str
    bind_dn: str = ""
    bind_password: str = ""
    user_filter: str = DEFAULT_FILTER
    mail_filter: str = DEFAULT_FILTER_MAIL
    timeout_seconds: float = 5.0

    @property
    def uses_tls(self) -> bool:
        return self.url.startswith("ldaps://")

    @classmethod
    def from_file(cls, path: Path) -> Optional["LdapSettings"]:
        """Load ``ldap.json``; None when absent/disabled."""
        try:
            if not path.is_file():
                return None
            payload = json.loads(
                path.read_text(encoding="utf-8"),
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "LDAP config %s unreadable (%s); LDAP disabled",
                path,
                exc,
            )
            return None
        if not isinstance(payload, dict) or not payload.get("url"):
            return None
        if not payload.get("base_dn"):
            return None
        return cls(
            url=str(payload["url"]),
            base_dn=str(payload["base_dn"]),
            bind_dn=str(payload.get("bind_dn") or ""),
            bind_password=str(payload.get("bind_password") or ""),
            user_filter=str(
                payload.get("user_filter") or DEFAULT_FILTER,
            ),
            mail_filter=str(
                payload.get("mail_filter") or DEFAULT_FILTER_MAIL,
            ),
            timeout_seconds=float(payload.get("timeout_seconds") or 5.0),
        )


class LdapAuthenticator:
    """Search-then-bind verifier over ``ldap3`` (C4).

    Failure semantics: any transport/protocol error returns False
    (fail-closed — local password remains the fallback source).
    """

    def __init__(self, settings: LdapSettings) -> None:
        self._settings = settings

    @property
    def settings(self) -> LdapSettings:
        return self._settings

    def verify(self, username: str, password: str) -> bool:
        """True iff the directory binds with this pair."""
        if not username or not password:
            return False
        try:
            import ldap3  # optional extra: qwenpaw[ldap]
        except ImportError:  # pragma: no cover - env dependent
            logger.error(
                "LDAP configured but ldap3 is not installed; "
                "install qwenpaw[ldap].",
            )
            return False
        try:
            return self._verify_with(ldap3, username, password)
        except Exception:  # pylint: disable=broad-except
            logger.exception("LDAP verification failed")
            return False

    def _verify_with(
        self,
        ldap3: Any,
        username: str,
        password: str,
    ) -> bool:
        server = ldap3.Server(
            self._settings.url,
            use_ssl=self._settings.uses_tls,
            connect_timeout=int(self._settings.timeout_seconds),
            get_info=None,
        )
        connection = ldap3.Connection(
            server,
            user=self._settings.bind_dn or None,
            password=self._settings.bind_password or None,
            auto_bind=True,
            receive_timeout=self._settings.timeout_seconds,
        )
        try:
            for template in (
                self._settings.user_filter,
                self._settings.mail_filter,
            ):
                filt = template.format(
                    login=_escape_filter(username),
                )
                if not connection.search(
                    self._settings.base_dn,
                    filt,
                    attributes=(),
                ):
                    continue
                entries = connection.entries or []
                if not entries:
                    continue
                entry_dn = entries[0].entry_dn
                if connection.rebind(
                    user=entry_dn,
                    password=password,
                ):
                    return True
            return False
        finally:
            try:
                connection.unbind()
            except Exception:  # pylint: disable=broad-except
                pass


def _escape_filter(value: str) -> str:
    """RFC 4515 filter escaping."""
    out = []
    for char in value:
        if char in "\\*()\\\0":
            out.append(f"\\{ord(char):02x}")
        else:
            out.append(char)
    return "".join(out)


def load_ldap_authenticator(
    root_dir: Path,
) -> Optional[LdapAuthenticator]:
    """Build the authenticator from <hub root>/ldap.json (C4)."""
    settings = LdapSettings.from_file(root_dir / "ldap.json")
    if settings is None:
        return None
    logger.info("LDAP direct-bind enabled for %s", settings.url)
    return LdapAuthenticator(settings)
