# -*- coding: utf-8 -*-
"""Idempotent first-admin bootstrap for containerized Hub deploys.

Public Hub binding refuses to start until an enabled administrator
exists (control_app public-bind gate). Container/K8s deployments
cannot do that interactively, so this module creates the first admin
directly against the hub database. It is a no-op when the username
already exists.

Usage (chart initContainer / docker one-shot):

    python -m qwenpaw.hub.bootstrap_admin \
        --root /var/lib/qwenpaw --username owner --password 's3cret'
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def bootstrap_admin(root_dir: Path, username: str, password: str) -> str:
    """Create the first admin user; return a short status string."""
    from .auth import HubAuthService
    from .credentials import TenantCredentialVault

    root_dir.mkdir(parents=True, exist_ok=True)
    database_path = root_dir / "control.db"
    vault = TenantCredentialVault(
        database_path,
        root_dir / "secrets" / ".vault_key",
    )
    service = HubAuthService(database_path, vault)
    existing = service.user_count()
    if existing:
        return f"users-exists:{existing}"
    user, _token = service.register(username, password)
    return f"created:{user.username}:{user.role}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qwenpaw.hub.bootstrap_admin",
        description="Create the first Hub administrator (idempotent).",
    )
    parser.add_argument(
        "--root",
        required=True,
        type=Path,
        help="Hub root directory (QWENPAW_HUB_DIR)",
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args(argv)
    status = bootstrap_admin(args.root, args.username, args.password)
    print(f"[bootstrap_admin] {status}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
