# -*- coding: utf-8 -*-
"""Tests for the idempotent first-admin bootstrap helper."""

from pathlib import Path

from qwenpaw.hub.bootstrap_admin import bootstrap_admin


def test_bootstrap_creates_then_noops(tmp_path: Path) -> None:
    root = tmp_path / "hub"
    first = bootstrap_admin(root, "owner", "pw-123456")
    assert first.startswith("created:owner:admin")
    second = bootstrap_admin(root, "owner", "other-pw")
    assert second.startswith("users-exists:1")
