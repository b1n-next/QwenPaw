# -*- coding: utf-8 -*-
"""Docker-container sandbox backend (Phase 3, G-P14 remainder).

One sandbox lifetime maps to one long-lived container started with
``docker run -d ... sleep infinity``; every ``execute()`` rides
``docker exec``; teardown is ``docker rm -f``. Hardware constraints
(memory, pids) become real cgroup limits instead of best-effort
promises, and ``--runtime`` lets operators pick gVisor / Kata for
kernel-isolation upgrades without code changes.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from typing import List, Optional

from .config import ExecutionResult, SandboxConfig
from .local_sandbox import LocalSandbox

logger = logging.getLogger(__name__)

_DEFAULT_IMAGE = "python:3.11-slim"
_SANDBOX_VIOLATION_MARKERS = (
    "docker:",
    "OCI runtime create failed",
    "operation not permitted",
)


class ContainerSandbox(LocalSandbox):
    """Container-isolated sandbox backed by the docker CLI."""

    _ENFORCED_FIELDS = frozenset(
        {
            "workspace_dir",
            "mounts",
            "network_allow",
            "max_memory_mb",
            "max_processes",
            "env_mode",
        },
    )

    def __init__(self, config: SandboxConfig) -> None:
        super().__init__(config)
        hints = getattr(self, "_ENFORCEMENT_HINTS", {})
        hints = dict(hints)
        hints["network_allow"] = (
            "Containers filter the network as all-open or fully "
            "blocked (--network none); domain-level allowlists are "
            "not expressible."
        )
        self._ENFORCEMENT_HINTS = hints  # type: ignore[assignment]
        self._container_name = f"qwenpaw-sbx-{uuid.uuid4().hex[:12]}"
        self._started = False
        self._image = str(
            config.platform_hints.get("container_image", _DEFAULT_IMAGE),
        )
        runtime = str(config.platform_hints.get("container_runtime", ""))
        self._runtime = runtime.strip()

    # ------------------------------------------------------- plumbing

    def _find_docker(self) -> str:
        docker = shutil.which("docker")
        if docker is None:
            raise FileNotFoundError(
                "docker CLI not found on PATH; the container sandbox "
                "backend requires a docker daemon (any runtime: "
                "runc / gVisor / Kata)",
            )
        return docker

    def _run_argv(self, docker: str) -> List[str]:
        config = self._config
        argv = [
            docker,
            "run",
            "-d",
            "--name",
            self._container_name,
        ]
        if self._runtime:
            argv += ["--runtime", self._runtime]
        if config.max_memory_mb:
            argv += ["--memory", f"{int(config.max_memory_mb)}m"]
        if config.max_processes:
            argv += ["--pids-limit", str(int(config.max_processes))]
        if config.network_allow != ["*"]:
            argv += ["--network", "none"]
        argv += ["-v", f"{config.workspace_dir}:/workspace:rw"]
        for mount in config.mounts:
            flag = "rw" if mount.writable else "ro"
            argv += ["-v", f"{mount.path}:/mnt{mount.path}:{flag}"]
        argv += ["-w", "/workspace", self._image, "sleep", "infinity"]
        return argv

    async def _run_command(
        self,
        argv: List[str],
        timeout: float,
    ) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        return (
            process.returncode or 0,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )

    async def _ensure_started(self) -> Optional[str]:
        """Start the container once; None (with log) when docker is
        unavailable — matches the bubblewrap backend's soft failure."""
        if self._started:
            return self._container_name
        try:
            docker = self._find_docker()
        except FileNotFoundError as exc:
            logger.warning("container sandbox unavailable: %s", exc)
            return None
        argv = self._run_argv(docker)
        code, _stdout, stderr = await self._run_command(
            argv,
            timeout=float(self._config.timeout_seconds),
        )
        if code != 0:
            logger.warning(
                "container sandbox failed to start (%s): %s",
                code,
                stderr.strip(),
            )
            return None
        self._started = True
        logger.info(
            "container sandbox %s started (image=%s runtime=%s)",
            self._container_name,
            self._image,
            self._runtime or "default",
        )
        return self._container_name

    # ------------------------------------------------------- surface

    async def execute(
        self,
        cmd: str,
        cwd: Optional[str] = None,
    ) -> ExecutionResult:
        """Execute one command inside the container."""
        start = time.monotonic()
        container = await self._ensure_started()
        if container is None:
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr="container sandbox unavailable (see logs)",
                duration_ms=0,
            )
        docker = self._find_docker()
        workdir = cwd or "/workspace"
        argv = [
            docker,
            "exec",
            "-w",
            workdir,
            container,
            "sh",
            "-c",
            cmd,
        ]
        try:
            code, stdout, stderr = await self._run_command(
                argv,
                timeout=float(self._config.timeout_seconds),
            )
        except asyncio.TimeoutError:
            await self.stop()
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr="command timed out; container terminated",
                timed_out=True,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        violation = None
        if code != 0 and any(
            marker in stderr for marker in _SANDBOX_VIOLATION_MARKERS
        ):
            violation = stderr.strip()
        return ExecutionResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.monotonic() - start) * 1000),
            sandbox_violation=violation,
        )

    async def stop(self) -> None:
        """Remove the container (idempotent)."""
        if not self._started:
            return
        self._started = False
        try:
            docker = self._find_docker()
        except FileNotFoundError:
            return
        try:
            await self._run_command(
                [docker, "rm", "-f", self._container_name],
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "container sandbox %s teardown timed out",
                self._container_name,
            )

    async def __aenter__(self) -> "ContainerSandbox":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()


__all__ = ["ContainerSandbox"]
