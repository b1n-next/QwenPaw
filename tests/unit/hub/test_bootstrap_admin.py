# -*- coding: utf-8 -*-
"""Tests for the idempotent first-admin bootstrap helper (official API)."""

from pathlib import Path

import pytest

from qwenpaw.hub.bootstrap_admin import bootstrap_admin, main


def test_bootstrap_creates_then_noops(tmp_path: Path) -> None:
    root = tmp_path / "hub"
    first = bootstrap_admin(root, "owner", "pw-123456")
    assert first.startswith("created:owner:admin")
    second = bootstrap_admin(root, "owner", "other-pw")
    assert second.startswith("users-exists:1")


def test_bootstrap_uses_env_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "from-env"
    monkeypatch.setenv("QWENPAW_HUB_DIR", str(root))
    status = bootstrap_admin(None, "envowner", "pw-123456")
    assert status.startswith("created:envowner:admin")
    assert (root / "control.db").exists()


def test_bootstrap_root_arg_wins_over_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_HUB_DIR", str(tmp_path / "ignored"))
    root = tmp_path / "from-arg"
    status = bootstrap_admin(root, "argowner", "pw-123456")
    assert status.startswith("created:argowner:admin")
    assert (root / "control.db").exists()
    assert not (tmp_path / "ignored" / "control.db").exists()


def test_main_exit_codes(tmp_path: Path, capsys) -> None:
    root = tmp_path / "hub"
    code = main(
        ["--root", str(root), "--username", "u1", "--password", "pw-123456"],
    )
    assert code == 0
    assert "created:u1:admin" in capsys.readouterr().out
    # rerun: idempotent
    code = main(
        ["--root", str(root), "--username", "u1", "--password", "pw-123456"],
    )
    assert code == 0
    assert "users-exists:1" in capsys.readouterr().out


def test_main_bad_password_fails_cleanly(
    tmp_path: Path,
    capsys,
) -> None:
    code = main(
        [
            "--root",
            str(tmp_path / "hub"),
            "--username",
            "u2",
            "--password",
            "short",
        ],
    )
    assert code == 1
    assert "bootstrap failed" in capsys.readouterr().err
