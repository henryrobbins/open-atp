"""Local Docker backend.

Ported from milp_flare ``harness/runner/docker.py``, generalised from "launch an
agent" to "run an arbitrary command over a workdir". Mechanics:

* ``docker run --rm --name afps-<uuid>`` with the workdir bind-mounted at
  ``/workspace/wd`` (so file mutations land on the host with no copy-out step).
* A unique container name so a run can be cancelled with ``docker kill``.
* The image bakes a warm Mathlib olean cache at ``/workspace/.lake``; every command
  is wrapped to ``cd`` into the mount and symlink ``.lake`` to it, mirroring
  milp_flare's ``entrypoint.sh``. This is the one image-layout convention the backend
  owns, keeping the verifier image-agnostic.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from open_afps.backends.base import (
    BackendConfig,
    CommandHandle,
    CommandResult,
    ComputeBackend,
    wrap_command,
)


@dataclass
class DockerConfig(BackendConfig):
    # Path inside the container where the workdir is bind-mounted.
    workdir_mount: str = "/workspace/wd"
    # Image-baked warm cache to symlink the workdir's .lake to (skip if empty).
    baked_lake: str = "/workspace/.lake"
    # Extra ``-v host:container`` mounts (e.g. agent credential dirs, in later phases).
    volumes: tuple[tuple[str, str], ...] = ()


@dataclass
class DockerCommandHandle(CommandHandle):
    popen: subprocess.Popen[str]
    container: str
    started_at: float
    _stdout_lines: list[str] = field(default_factory=list)

    def stream(self) -> Iterator[str]:
        assert self.popen.stdout is not None
        for line in self.popen.stdout:
            line = line.rstrip("\n")
            self._stdout_lines.append(line)
            yield line

    def cancel(self) -> None:
        # The workdir is bind-mounted, so partial artifacts are already on the host;
        # just kill the container.
        subprocess.run(
            ["docker", "kill", self.container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def wait(self) -> CommandResult:
        stdout, stderr = self.popen.communicate()
        # communicate() returns "" for streams already drained via stream().
        out = stdout if stdout else "\n".join(self._stdout_lines)
        return CommandResult(
            exit_code=self.popen.returncode,
            stdout=out,
            stderr=stderr or "",
            duration_s=time.time() - self.started_at,
        )


class DockerBackend(ComputeBackend):
    config: DockerConfig

    # The image runs as the non-root ``agent`` user (Lean's elan + Claude Code
    # both live under this HOME); credential mounts land here.
    container_home = "/home/agent"

    def __init__(self, config: DockerConfig) -> None:
        super().__init__(config)

    @property
    def name(self) -> str:
        return "docker"

    def _wrap(self, command: str) -> str:
        """cd into the mount and wire up the warm Mathlib cache before ``command``."""
        return wrap_command(self.config.workdir_mount, self.config.baked_lake, command)

    def _build_cmd(
        self,
        workdir: Path,
        container: str,
        env: Mapping[str, str],
        mounts: Sequence[tuple[str, str]],
    ) -> list[str]:
        cmd = ["docker", "run", "--rm", "--name", container]
        cmd += ["-v", f"{workdir.resolve()}:{self.config.workdir_mount}"]
        # Config-level mounts (baked in) then per-call mounts (e.g. credential dirs).
        for host, dest in (*self.config.volumes, *mounts):
            cmd += ["-v", f"{host}:{dest}"]
        for key, value in {**self.config.env, **env}.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += [self.config.image]
        return cmd

    def start(
        self,
        workdir: Path,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
        timeout_s: int | None = None,
    ) -> CommandHandle:
        container = f"afps-{uuid.uuid4().hex[:12]}"
        argv = self._build_cmd(workdir, container, env or {}, mounts or ())
        argv += ["bash", "-lc", self._wrap(command)]
        popen = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        return DockerCommandHandle(
            popen=popen, container=container, started_at=time.time()
        )
