# -*- coding: utf-8 -*-
"""Idempotent, non-interactive first-admin bootstrap for containerized
Hub deploys — now a thin shell over the official bootstrap API.

Core logic lives upstream (hub/bootstrap.py, merged via #7696):
root resolution honours QWENPAW_HUB_DIR, and initialize_hub_admin()
creates the first administrator transactionally. This module only
adds the two container constraints the official interactive CLI
(qwenpaw hub --init-admin) cannot serve:

  1. non-interactive: credentials come from argv, not a tty prompt;
  2. idempotent: initContainers rerun on every pod restart, so an
     already-initialized hub must exit 0 with a status marker.

Usage (chart initContainer / docker one-shot):

    QWENPAW_HUB_DIR=/var/lib/qwenpaw \
        python -m qwenpaw.hub.bootstrap_admin \
        --username owner --password 's3cret'

Legacy --root is still accepted and maps to QWENPAW_HUB_DIR.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def bootstrap_admin(
    root_dir: Path | None,
    username: str,
    password: str,
) -> str:
    """Create the first admin via the official API; idempotent."""
    if root_dir is not None:
        os.environ["QWENPAW_HUB_DIR"] = str(root_dir)
    from .bootstrap import (
        ensure_admin_initialization_available,
        get_hub_root,
        initialize_hub_admin,
    )

    root = get_hub_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        ensure_admin_initialization_available()
    except PermissionError:
        # Idempotent rerun (pod restarted after the first bootstrap).
        user_count = _user_count(root)
        return f"users-exists:{user_count}"
    user = initialize_hub_admin(username, password)
    return f"created:{user.username}:{user.role}"


def _user_count(root_dir: Path) -> int:
    """Best-effort count for the status marker (never masks errors)."""
    from .auth import HubAuthService
    from .credentials import TenantCredentialVault

    vault = TenantCredentialVault(
        root_dir / "control.db",
        root_dir / "secrets" / ".vault_key",
    )
    return HubAuthService(root_dir / "control.db", vault).user_count()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qwenpaw.hub.bootstrap_admin",
        description=(
            "Create the first Hub administrator " "(official API, idempotent)."
        ),
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--root",
        required=False,
        default=None,
        type=Path,
        help="Hub root directory (preferred: QWENPAW_HUB_DIR env)",
    )
    args = parser.parse_args(argv)
    try:
        status = bootstrap_admin(
            args.root,
            args.username,
            args.password,
        )
    except (PermissionError, ValueError, RuntimeError) as exc:
        print(f"[bootstrap_admin] bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print(f"[bootstrap_admin] {status}")
    return 0


if __name__ == "__main__":  # pragma: no cover - container entrypoint
    sys.exit(main())

__all__ = ["bootstrap_admin", "main"]
