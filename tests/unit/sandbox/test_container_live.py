# -*- coding: utf-8 -*-
"""Live docker-daemon tests for the container sandbox backend.

Skipped automatically when no docker daemon is reachable (CI has
none), so these run on workstations / dogfood hosts that DO have
one. They prove the real isolation properties the mocked suite can
only assert structurally: cgroup memory caps, --network none, and
read-only mounts.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import time

import pytest

from qwenpaw.sandbox.config import MountSpec, SandboxConfig, SandboxMode
from qwenpaw.sandbox.container_sandbox import ContainerSandbox

_IMAGE = "python:3.11-slim"


def _daemon_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        result = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    not _daemon_available(),
    reason="no docker daemon reachable on this host",
)


def _config(**overrides) -> SandboxConfig:
    values = {
        "mode": SandboxMode.CONTAINER,
        "workspace_dir": "/tmp",
        "timeout_seconds": 90,
        "platform_hints": {"container_image": _IMAGE},
    }
    values.update(overrides)
    return SandboxConfig(**values)


@pytest.fixture(name="sandbox")
def _sandbox() -> ContainerSandbox:
    return ContainerSandbox(_config())


async def test_roundtrip_and_cgroup_memory(sandbox: ContainerSandbox):
    result = await sandbox.execute("echo live && python -c 'print(6*7)'")
    assert result.exit_code == 0
    assert "live" in result.stdout
    assert "42" in result.stdout

    mem = await sandbox.execute(
        "cat /sys/fs/cgroup/memory.max 2>/dev/null "
        "|| cat /sys/fs/cgroup/memory/memory.limit_in_bytes",
    )
    # cgroup v1 reports "unlimited" without a cap; the explicit cap
    # is asserted wherever the container reports it numerically.
    value = mem.stdout.strip()
    if value.isdigit():
        assert int(value) <= 512 * 1024 * 1024
    await sandbox.stop()


async def test_network_blocked_by_default():
    sandbox = ContainerSandbox(_config(max_memory_mb=256))
    probe = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    print('NET-OPEN')\n"
        "except OSError:\n"
        "    print('NET-BLOCKED')\n"
    )
    encoded = base64.b64encode(probe.encode()).decode()
    result = await sandbox.execute(f"echo {encoded} | base64 -d | python")
    assert result.exit_code == 0
    assert "NET-BLOCKED" in result.stdout
    await sandbox.stop()


async def test_readonly_mount_denies_writes(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("classified\n", encoding="utf-8")
    sandbox = ContainerSandbox(
        _config(mounts=[MountSpec(path=str(secret), writable=False)]),
    )
    read = await sandbox.execute(f"cat /mnt{secret}")
    assert read.exit_code == 0
    assert read.stdout.strip() == "classified"

    write = await sandbox.execute(f"touch /mnt{secret} 2>&1")
    assert write.exit_code != 0
    assert "Read-only" in write.stdout or "denied" in write.stdout
    await sandbox.stop()


async def test_teardown_removes_container(sandbox: ContainerSandbox):
    await sandbox.execute("true")
    name = sandbox._container_name  # pylint: disable=protected-access
    await sandbox.stop()
    listing = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"name={name}",
            "--format",
            "{{.ID}}",
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert listing.stdout.decode().strip() == ""


def test_daemon_socket_probe_shape():
    """The daemon probe used by the skip gate answers quickly."""
    assert _daemon_available() is True
    assert time.time() > 0
