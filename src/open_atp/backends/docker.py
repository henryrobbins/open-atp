"""Local Docker backend.

Runs an arbitrary command over a workdir in a Lean+Mathlib container. Mechanics:

* ``docker run --rm --name afps-<uuid>`` with the workdir bind-mounted at
  ``/workspace/wd`` (so file mutations land on the host with no copy-out step).
* A unique container name so a run can be cancelled with ``docker kill``.
* The image bakes a warm Mathlib olean cache at ``/workspace/.lake``; every command
  is wrapped to ``cd`` into the mount and symlink ``.lake`` to it, mirroring
  milp_flare's ``entrypoint.sh``. This is the one image-layout convention the backend
  owns, keeping the verifier image-agnostic.
"""

from __future__ import annotations

import logging
import subprocess
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from open_atp.backends.base import (
    CommandHandle,
    CommandResult,
    ComputeBackend,
    ComputeSession,
    wrap_command,
)
from open_atp.images import DEFAULT_IMAGE, Image

log = logging.getLogger("open_atp")


@dataclass
class DockerCommandHandle(CommandHandle):
    popen: subprocess.Popen[str]
    container: str
    started_at: float
    _stdout_lines: list[str] = field(default_factory=list)

    def stream(self) -> Iterator[str]:
        """Yield the container's stdout lines as they arrive, retaining each."""
        assert self.popen.stdout is not None
        for line in self.popen.stdout:
            line = line.rstrip("\n")
            self._stdout_lines.append(line)
            yield line

    def cancel(self) -> None:
        """``docker kill`` the container (bind-mounted artifacts already on host)."""
        log.debug("docker kill", extra={"container": self.container})
        subprocess.run(
            ["docker", "kill", self.container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def wait(self) -> CommandResult:
        """Block for the container to exit and collect its captured output."""
        stdout, stderr = self.popen.communicate()
        # communicate() returns "" for streams already drained via stream().
        out = stdout if stdout else "\n".join(self._stdout_lines)
        result = CommandResult(
            exit_code=self.popen.returncode,
            stdout=out,
            stderr=stderr or "",
            duration_s=time.time() - self.started_at,
        )
        log.debug(
            "docker command exited",
            extra={
                "container": self.container,
                "exit_code": result.exit_code,
                "duration_s": round(result.duration_s, 1),
            },
        )
        return result


@dataclass
class DockerSessionHandle(DockerCommandHandle):
    """A command ``docker exec``'d into a live :class:`DockerSession`.

    Identical streaming/wait to the one-shot handle, but :meth:`cancel` is a no-op:
    the session owns the container's lifecycle (``close`` -> ``docker kill``), so one
    exec finishing -- or its context-manager ``__exit__`` -- must not tear the
    container down out from under the next command.
    """

    def cancel(self) -> None:
        """No-op: the owning session, not the handle, kills the container."""


class DockerBackend(ComputeBackend):
    """Run sandboxes as local ``docker`` containers over a bind-mounted workdir.

    The workdir is bind-mounted (not copied), so a command's file edits land directly
    on the host. Construction is offline -- it just records its knobs; the daemon is
    only contacted when a command runs.

    Parameters
    ----------
    image : Image
        The sandbox image carrying Lean + Mathlib -- its tag plus the toolchain and
        Mathlib revision the verifier checks projects against. Default
        :data:`~open_atp.images.DEFAULT_IMAGE`. A mapping is coerced to an
        :class:`~open_atp.images.Image` (so a parsed config's nested ``image:`` block
        works).
    env : Mapping[str, str], optional
        Environment variables baked into every command run in the sandbox. Default
        empty.
    workdir_mount : str
        Path inside the container where the workdir is bind-mounted. Default
        ``/workspace/wd``.
    baked_lake : str
        Image-baked warm cache to symlink the workdir's ``.lake`` to. Empty skips the
        symlink. Default ``/workspace/.lake``.
    volumes : tuple[tuple[str, str], ...]
        Extra ``-v host:container`` mounts (e.g. agent credential dirs). Default empty.

    Examples
    --------

    >>> from open_atp.backends.docker import DockerBackend
    >>> from open_atp.images import DEFAULT_IMAGE
    >>> backend = DockerBackend(image=DEFAULT_IMAGE)
    >>> backend.name
    'docker'
    >>> backend.workdir_mount
    '/workspace/wd'
    """

    # The image runs as the non-root ``agent`` user (Lean's elan + Claude Code
    # both live under this HOME); credential mounts land here.
    container_home = "/home/agent"

    def __init__(
        self,
        *,
        image: Image | Mapping[str, object] = DEFAULT_IMAGE,
        env: Mapping[str, str] | None = None,
        workdir_mount: str = "/workspace/wd",
        baked_lake: str = "/workspace/.lake",
        volumes: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__(image=image, env=env)
        self.workdir_mount = workdir_mount
        self.baked_lake = baked_lake
        self.volumes = tuple(tuple(v) for v in volumes)

    @property
    def name(self) -> str:
        """Short identifier for the backend: ``"docker"``."""
        return "docker"

    @property
    def wallclock_overhead_s(self) -> int:
        """Overhead beyond a command's budget: only container start + kill teardown.

        The workdir is bind-mounted, so there is no push/pull and no warm-cache paging
        -- a few seconds of ``docker run``/``docker kill`` around the command, covered
        by a small slack.
        """
        return 60

    def _wrap(self, command: str) -> str:
        """cd into the mount and wire up the warm Mathlib cache before ``command``."""
        return wrap_command(self.workdir_mount, self.baked_lake, command)

    def _build_cmd(
        self,
        workdir: Path,
        container: str,
        env: Mapping[str, str],
        mounts: Sequence[tuple[str, str]],
        *,
        detach: bool = False,
    ) -> list[str]:
        """Build the ``docker run`` argv: workdir + extra mounts, env, and image.

        Parameters
        ----------
        workdir : pathlib.Path
            Host directory bind-mounted at :attr:`workdir_mount`.
        container : str
            ``--name`` for the container, so a run can later be ``docker kill``ed.
        env : Mapping[str, str]
            Per-call environment variables, merged over the backend's :attr:`env`.
        mounts : Sequence[tuple[str, str]]
            Extra ``(host_path, container_path)`` bind mounts appended after
            :attr:`volumes`.
        detach : bool
            Add ``-d`` for a keep-alive session container (commands then land via
            ``docker exec``). Default ``False``.

        Returns
        -------
        list[str]
            The ``docker run`` argv, up to but not including the in-container command.
        """
        cmd = ["docker", "run", "--rm"]
        if detach:
            cmd.append("-d")
        cmd += ["--name", container]
        cmd += ["-v", f"{workdir.resolve()}:{self.workdir_mount}"]
        # Backend-level mounts (baked in) then per-call mounts (e.g. credential dirs).
        for host, dest in (*self.volumes, *mounts):
            cmd += ["-v", f"{host}:{dest}"]
        for key, value in {**self.env, **env}.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += [self.image.name]
        return cmd

    def start(
        self,
        workdir: Path,
        command: str,
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
    ) -> CommandHandle:
        """``docker run`` ``command`` with ``workdir`` bind-mounted, returning a handle.

        Edits land on the host directly via the bind mount, so there is no copy-out
        step; the unique container name lets :meth:`DockerCommandHandle.cancel` kill it.

        Parameters
        ----------
        workdir : pathlib.Path
            Host directory bind-mounted at :attr:`workdir_mount`; the command's edits
            land here directly.
        command : str
            The shell command to run inside the container.
        timeout_s : int
            Unused by Docker (the container has no built-in cap); callers enforce
            timeouts on the handle.
        env : Mapping[str, str], optional
            Per-call environment variables, merged over the backend's :attr:`env`.
            Default empty.
        mounts : Sequence[tuple[str, str]], optional
            Extra ``(host_path, container_path)`` bind mounts (e.g. agent credential
            dirs). Default empty.

        Returns
        -------
        CommandHandle
            A live :class:`DockerCommandHandle` to stream or :meth:`~CommandHandle.wait`
            on.
        """
        container = f"afps-{uuid.uuid4().hex[:12]}"
        argv = self._build_cmd(workdir, container, env or {}, mounts or ())
        argv += ["bash", "-lc", self._wrap(command)]
        log.debug(
            "docker run",
            extra={
                "container": container,
                "image": self.image.name,
                "env_keys": sorted({**self.env, **(env or {})}),
            },
        )
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

    def session(
        self,
        workdir: Path,
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
    ) -> ComputeSession:
        """Start a detached keep-alive container for repeated ``docker exec`` commands.

        All mounts are wired at ``docker run`` time (Docker can't add them per-exec);
        the container lives until :meth:`DockerSession.close`.

        Parameters
        ----------
        workdir : pathlib.Path
            Host directory bind-mounted at :attr:`workdir_mount` for the session's life.
        timeout_s : int
            Unused by Docker (the container has no built-in cap).
        env : Mapping[str, str], optional
            Environment variables pinned on the container at creation, merged over the
            backend's :attr:`env`. Default empty.
        mounts : Sequence[tuple[str, str]], optional
            Extra ``(host_path, container_path)`` bind mounts wired at creation (Docker
            can't add them per-exec). Default empty.

        Returns
        -------
        ComputeSession
            A live :class:`DockerSession` over the workdir.
        """
        container = f"afps-{uuid.uuid4().hex[:12]}"
        argv = self._build_cmd(workdir, container, env or {}, mounts or (), detach=True)
        argv += ["sleep", "infinity"]
        log.debug(
            "docker session start",
            extra={
                "container": container,
                "image": self.image.name,
                "env_keys": sorted({**self.env, **(env or {})}),
            },
        )
        proc = subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        if proc.returncode != 0:
            log.error(
                "docker session container failed to start",
                extra={"container": container, "stderr": proc.stderr.strip()},
            )
            raise RuntimeError(
                f"failed to start docker session container: {proc.stderr.strip()}"
            )
        return DockerSession(backend=self, container=container)


@dataclass
class DockerSession(ComputeSession):
    """A live ``docker run -d`` container; exec many commands, ``docker kill`` once.

    The workdir is bind-mounted, so :meth:`sync_out`/:meth:`sync_in` are no-ops --
    edits already live on the host.
    """

    backend: DockerBackend
    container: str

    def exec(
        self,
        command: str,
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandHandle:
        """``docker exec`` ``command`` into the container; close() owns teardown.

        Parameters
        ----------
        command : str
            The shell command to run in the live container.
        timeout_s : int
            Unused by Docker (the container has no built-in cap).
        env : Mapping[str, str], optional
            Per-command environment variables (``docker exec -e``). Default empty.

        Returns
        -------
        CommandHandle
            A live :class:`DockerSessionHandle` whose ``cancel`` is a no-op (the session
            owns teardown).
        """
        argv = ["docker", "exec"]
        for key, value in (env or {}).items():
            argv += ["-e", f"{key}={value}"]
        argv += [self.container, "bash", "-lc", self.backend._wrap(command)]
        log.debug(
            "docker exec",
            extra={"container": self.container, "env_keys": sorted(env or {})},
        )
        popen = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        return DockerSessionHandle(
            popen=popen, container=self.container, started_at=time.time()
        )

    def sync_out(self) -> None:
        """No-op: the bind mount means edits are already on the host."""

    def sync_in(self) -> None:
        """No-op: the bind mount means host edits are already visible in-container."""

    def close(self) -> None:
        """``docker kill`` the keep-alive container. Idempotent."""
        log.debug("docker session close", extra={"container": self.container})
        subprocess.run(
            ["docker", "kill", self.container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
