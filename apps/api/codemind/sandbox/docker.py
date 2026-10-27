"""Run one fixed command in a throwaway, locked-down Docker container.

The container gets the patched workspace read-only at /work, no network, no capabilities, an
unprivileged user, a read-only root filesystem with a small /tmp, and CPU, memory, process and
wall-clock limits. Images are never pulled during a run (`--pull never`): they are built ahead of
time from sandbox/*.Dockerfile, so a run cannot fetch anything.
"""

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from codemind.core.config import Settings

POLL_SECONDS = 0.2
DOCKER_ERROR_EXIT = 125  # `docker run` itself failed: no such image, daemon error, bad flags


@dataclass(frozen=True)
class Profile:
    """A test command the server allows. Commands never come from the model or the request."""

    name: str
    image: str
    command: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    no_tests_exit_codes: tuple[int, ...] = ()


def profiles(settings: Settings) -> dict[str, Profile]:
    return {
        "pytest": Profile(
            "pytest",
            settings.sandbox_python_image,
            ("python", "-m", "pytest", "-q", "--color=no", "-p", "no:cacheprovider"),
            {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "/work"},
            no_tests_exit_codes=(5,),
        ),
        "node-test": Profile("node-test", settings.sandbox_node_image, ("node", "--test")),
    }


@dataclass(frozen=True)
class Limits:
    timeout_seconds: int
    memory_mb: int
    cpus: float
    pids: int
    tmp_mb: int
    output_bytes: int
    max_output_bytes: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "Limits":
        return cls(
            timeout_seconds=settings.sandbox_timeout_seconds,
            memory_mb=settings.sandbox_memory_mb,
            cpus=settings.sandbox_cpus,
            pids=settings.sandbox_pids,
            tmp_mb=settings.sandbox_tmp_mb,
            output_bytes=settings.sandbox_output_bytes,
            max_output_bytes=settings.sandbox_max_output_bytes,
        )

    def to_dict(self) -> dict[str, float | int]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "memory_mb": self.memory_mb,
            "cpus": self.cpus,
            "pids": self.pids,
            "tmp_mb": self.tmp_mb,
            "network": 0,
        }


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None
    output: str
    output_truncated: bool
    duration_ms: int
    timed_out: bool = False
    output_limit_hit: bool = False
    error_code: str | None = None
    error_message: str | None = None


class SandboxRunner(Protocol):
    def run(self, workspace: Path, profile: Profile, limits: Limits, name: str) -> RunResult: ...


class DockerRunner:
    def __init__(self, docker: str = "docker") -> None:
        self.docker = docker

    def command(self, workspace: Path, profile: Profile, limits: Limits, name: str) -> list[str]:
        argv = [
            self.docker, "run", "--rm", "--name", name,
            "--pull", "never",
            "--network", "none",
            "--user", "65534:65534",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", f"/tmp:rw,noexec,nosuid,size={limits.tmp_mb}m",
            "--memory", f"{limits.memory_mb}m",
            "--memory-swap", f"{limits.memory_mb}m",
            "--cpus", str(limits.cpus),
            "--pids-limit", str(limits.pids),
            "--mount", f"type=bind,source={workspace.resolve()},target=/work,readonly",
            "--workdir", "/work",
            "--env", "HOME=/tmp",
            "--env", "CI=1",
        ]  # fmt: skip
        for key, value in profile.env.items():
            argv += ["--env", f"{key}={value}"]
        return [*argv, profile.image, *profile.command]

    def run(self, workspace: Path, profile: Profile, limits: Limits, name: str) -> RunResult:
        argv = self.command(workspace, profile, limits, name)
        started = time.monotonic()
        with tempfile.TemporaryFile() as out:
            try:
                proc = subprocess.Popen(
                    argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT
                )
            except FileNotFoundError:
                return RunResult(
                    None,
                    "",
                    False,
                    0,
                    error_code="docker_not_found",
                    error_message=f"{self.docker!r} was not found. Install Docker to run tests.",
                )
            timed_out = output_limit_hit = False
            while proc.poll() is None:
                if time.monotonic() - started > limits.timeout_seconds:
                    timed_out = True
                elif os.fstat(out.fileno()).st_size > limits.max_output_bytes:
                    output_limit_hit = True
                if timed_out or output_limit_hit:
                    self._stop(name, proc)
                    break
                time.sleep(POLL_SECONDS)
            duration_ms = int((time.monotonic() - started) * 1000)
            size = os.fstat(out.fileno()).st_size
            out.seek(max(0, size - limits.output_bytes))
            output = out.read().decode("utf-8", errors="replace")
        exit_code = proc.returncode
        if exit_code == DOCKER_ERROR_EXIT and not (timed_out or output_limit_hit):
            return RunResult(
                exit_code,
                output,
                size > limits.output_bytes,
                duration_ms,
                error_code="sandbox_start_failed",
                error_message=(
                    f"Docker could not start the sandbox. Is the image {profile.image!r} built? "
                    "See docs/sandbox-design.md."
                ),
            )
        return RunResult(
            exit_code,
            output,
            size > limits.output_bytes,
            duration_ms,
            timed_out=timed_out,
            output_limit_hit=output_limit_hit,
        )

    def _stop(self, name: str, proc: "subprocess.Popen[bytes]") -> None:
        # Killing the docker client would leave the container running; kill the container.
        try:
            subprocess.run(
                [self.docker, "kill", name], capture_output=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
