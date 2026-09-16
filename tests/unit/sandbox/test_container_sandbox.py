# -*- coding: utf-8 -*-
"""Phase 3 (G-P14): container sandbox backend tests (mocked docker)."""

from __future__ import annotations

# pylint: disable=protected-access

import asyncio
from typing import List

import pytest

from qwenpaw.sandbox.config import (
    ExecutionResult,
    SandboxConfig,
    SandboxMode,
    create_sandbox,
)
from qwenpaw.sandbox.container_sandbox import ContainerSandbox


class _FakeDocker:
    """Record argv sequences and answer like the docker CLI."""

    def __init__(
        self,
        *,
        start_fails: bool = False,
        hang_exec: bool = False,
    ) -> None:
        self.calls: List[List[str]] = []
        self._start_fails = start_fails
        self._hang_exec = hang_exec

    async def run(self, argv: List[str], timeout: float):
        del timeout
        self.calls.append(list(argv))
        if "run" in argv and self._start_fails:
            return 125, "", "docker: image not found"
        if "exec" in argv and self._hang_exec:
            raise asyncio.TimeoutError()
        if "run" in argv:
            return 0, "container-id-123\n", ""
        if "rm" in argv:
            return 0, "container-id-123\n", ""
        return 0, "hello from container\n", ""


@pytest.fixture(name="docker")
def _docker(monkeypatch: pytest.MonkeyPatch) -> _FakeDocker:
    fake = _FakeDocker()
    monkeypatch.setattr(
        "qwenpaw.sandbox.container_sandbox.shutil.which",
        lambda _name: "/usr/local/bin/docker",
    )

    async def _runner(*args, **kwargs):
        # class-level patch: called bound (self, argv, timeout=...) or
        # unbound — normalise to (argv, timeout)
        argv = args[-1] if args else list(kwargs["argv"])
        timeout = kwargs.get("timeout", 30.0)
        return await fake.run(list(argv), timeout)

    monkeypatch.setattr(
        "qwenpaw.sandbox.container_sandbox.ContainerSandbox._run_command",
        _runner,
    )
    return fake


def _config(**overrides) -> SandboxConfig:
    values = {
        "mode": SandboxMode.CONTAINER,
        "workspace_dir": "/tmp/project",
        "timeout_seconds": 5,
    }
    values.update(overrides)
    return SandboxConfig(**values)


def test_dispatch_returns_container_backend() -> None:
    sandbox = create_sandbox(_config())
    assert isinstance(sandbox, ContainerSandbox)


def test_run_flags_network_memory_runtime(docker: _FakeDocker) -> None:
    sandbox = ContainerSandbox(
        _config(
            max_memory_mb=512,
            max_processes=64,
            platform_hints={
                "container_image": "qwenpaw/runner:2",
                "container_runtime": "gvisor",
            },
        ),
    )
    result = asyncio.run(sandbox.execute("echo hi"))
    assert result.exit_code == 0
    assert "hello from container" in result.stdout

    run_call = next(call for call in docker.calls if "run" in call)
    joined = " ".join(run_call)
    assert "--network none" in joined
    assert "--memory 512m" in joined
    assert "--pids-limit 64" in joined
    assert "--runtime gvisor" in joined
    assert "qwenpaw/runner:2" in joined
    assert "/tmp/project:/workspace:rw" in joined
    assert "sleep infinity" in joined


def test_open_network_skips_none_flag(docker: _FakeDocker) -> None:
    sandbox = ContainerSandbox(
        _config(network_allow=["*"], timeout_seconds=5),
    )
    asyncio.run(sandbox.execute("curl example.com"))
    run_call = next(call for call in docker.calls if "run" in call)
    assert "--network none" not in " ".join(run_call)


def test_mounts_rendered_readonly_by_default(
    docker: _FakeDocker,
) -> None:
    from qwenpaw.sandbox.config import MountSpec

    sandbox = ContainerSandbox(
        _config(
            mounts=[
                MountSpec(path="/opt/data", writable=False),
                MountSpec(path="/opt/cache", writable=True),
            ],
        ),
    )
    asyncio.run(sandbox.execute("ls /mnt/opt/data"))
    run_call = next(call for call in docker.calls if "run" in call)
    joined = " ".join(run_call)
    assert "/opt/data:/mnt/opt/data:ro" in joined
    assert "/opt/cache:/mnt/opt/cache:rw" in joined


def test_exec_uses_workdir_and_shell(docker: _FakeDocker) -> None:
    sandbox = ContainerSandbox(_config())
    asyncio.run(sandbox.execute("make build", cwd="/workspace/sub"))
    exec_call = next(call for call in docker.calls if "exec" in call)
    assert exec_call[exec_call.index("-w") + 1] == "/workspace/sub"
    assert exec_call[-3:] == ["sh", "-c", "make build"]


def _failing_runner():
    async def _runner(*args, **kwargs):
        del args, kwargs
        return 125, "", "docker: image not found"

    return _runner


def test_start_failure_soft_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "qwenpaw.sandbox.container_sandbox.ContainerSandbox._run_command",
        _failing_runner(),
    )
    sandbox = ContainerSandbox(_config())
    result = asyncio.run(sandbox.execute("echo hi"))
    assert result.exit_code == -1
    assert "unavailable" in result.stderr


def test_missing_docker_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "qwenpaw.sandbox.container_sandbox.shutil.which",
        lambda _name: None,
    )
    sandbox = ContainerSandbox(_config())
    result = asyncio.run(sandbox.execute("echo hi"))
    assert result.exit_code == -1
    assert "unavailable" in result.stderr


def test_timeout_kills_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "qwenpaw.sandbox.container_sandbox.shutil.which",
        lambda _name: "/usr/local/bin/docker",
    )
    stopped: List[str] = []
    hang = _FakeDocker(hang_exec=True)

    async def _runner(*args, **kwargs):
        argv = list(args[-1] if args else kwargs["argv"])
        if "run" in argv:
            return await hang.run(argv, kwargs.get("timeout", 30.0))
        if "exec" in argv:
            raise asyncio.TimeoutError()
        if "rm" in argv:
            stopped.append(" ".join(argv))
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(
        "qwenpaw.sandbox.container_sandbox.ContainerSandbox._run_command",
        _runner,
    )
    sandbox = ContainerSandbox(_config(timeout_seconds=1))
    result = asyncio.run(sandbox.execute("sleep 60"))
    assert result.timed_out is True
    assert result.exit_code == -1
    assert stopped and "rm" in stopped[0] and "-f" in stopped[0]


def test_stop_is_idempotent(docker: _FakeDocker) -> None:
    sandbox = ContainerSandbox(_config())
    asyncio.run(sandbox.execute("true"))
    asyncio.run(sandbox.stop())
    asyncio.run(sandbox.stop())
    rm_calls = [call for call in docker.calls if "rm" in call]
    assert len(rm_calls) == 1


def test_result_shape_defaults() -> None:
    result = ExecutionResult(
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=1,
    )
    assert result.timed_out is False
    assert result.sandbox_violation is None


def test_unknown_mode_raises() -> None:
    config = _config()
    object.__setattr__(config, "mode", "bogus")
    with pytest.raises(ValueError):
        create_sandbox(config)
