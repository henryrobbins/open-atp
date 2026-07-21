"""Local Docker backend.

Runs an arbitrary command over a workdir in a Lean+Mathlib container. Mechanics:

* ``docker run --rm --name open-atp-<uuid>`` with the workdir bind-mounted at
  ``/workspace/wd`` (so file mutations land on the host with no copy-out step).
* A unique container name so a run can be cancelled with ``docker kill``.
* The image bakes a warm Mathlib olean cache at ``/workspace/.lake``; every command
  is wrapped to ``cd`` into the mount and symlink ``.lake`` to it. This is the one
  image-layout convention the backend owns, keeping the verifier image-agnostic.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from open_atp.backends.base import (
    EXEC_DEADLINE_MARGIN_S,
    TIMEOUT_EXIT_CODE,
    TIMEOUT_KILL_AFTER_S,
    WORKDIR_MOUNT,
    CommandHandle,
    CommandResult,
    CommandTimeout,
    ComputeBackend,
    ComputeSession,
    ImageUnavailable,
    ProvisionError,
    wrap_command,
)
from open_atp.images import DEFAULT_IMAGE, Image

log = logging.getLogger("open_atp")


@dataclass
class DockerCommandHandle(CommandHandle):
    """A command ``docker exec``'d into a live :class:`DockerSession`.

    The handle owns no teardown: the session owns the container's lifecycle
    (``close`` -> ``docker kill``), so a finished exec leaves the container up for
    the next command.
    """

    popen: subprocess.Popen[str]
    container: str
    started_at: float
    budget_s: int
    deadline_s: float
    _stdout_lines: list[str] = field(default_factory=list)

    def stream(self) -> Iterator[str]:
        """Yield the container's stdout lines as they arrive, retaining each."""
        assert self.popen.stdout is not None
        for line in self.popen.stdout:
            line = line.rstrip("\n")
            self._stdout_lines.append(line)
            yield line

    def wait(self) -> CommandResult:
        """Block for the container to exit and collect its captured output.

        The in-container ``timeout`` wrapper caps the command; if it fails to kill in
        time the local ``communicate`` deadline is a backstop that ``docker kill``s the
        container. A command killed for exceeding its budget exits ``124`` -- surfaced
        as :class:`~open_atp.backends.base.CommandTimeout`.
        """
        try:
            stdout, stderr = self.popen.communicate(timeout=self.deadline_s)
        except subprocess.TimeoutExpired:
            log.warning(
                "docker command overran its deadline; killing container",
                extra={"container": self.container},
            )
            subprocess.run(
                ["docker", "kill", self.container],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
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
        if result.exit_code == TIMEOUT_EXIT_CODE:
            raise CommandTimeout(
                f"command exceeded its {self.budget_s}s budget and was killed",
                result=result,
            )
        return result


class DockerBackend(ComputeBackend):
    """Run sandboxes as local ``docker`` containers over a bind-mounted workdir.

    The workdir is bind-mounted (not copied), so a command's file edits land directly
    on the host. Construction is offline -- it just records its knobs; the daemon is
    only contacted when a command runs.

    Parameters
    ----------
    image : ~open_atp.images.Image, default DEFAULT_IMAGE
        The sandbox image carrying Lean + Mathlib -- its tag plus the toolchain and
        Mathlib revision the verifier checks projects against. A mapping is coerced
        to an :class:`~open_atp.images.Image` (so a parsed config's nested ``image:``
        block works).
    env : Mapping[str, str], optional
        Environment variables baked into every command run in the sandbox. Defaults
        to no extra variables.
    volumes : tuple[tuple[str, str], ...], default ()
        Extra ``-v host:container`` mounts (e.g. agent credential dirs).

    Examples
    --------

    >>> from open_atp.backends.docker import DockerBackend
    >>> from open_atp.images import DEFAULT_IMAGE
    >>> backend = DockerBackend(image=DEFAULT_IMAGE)
    >>> backend.name
    'docker'
    """

    # The image runs as the non-root ``agent`` user (Lean's elan + Claude Code
    # both live under this HOME); credential mounts land here.
    container_home = "/home/agent"

    def __init__(
        self,
        *,
        image: Image | Mapping[str, object] = DEFAULT_IMAGE,
        env: Mapping[str, str] | None = None,
        volumes: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__(image=image, env=env)
        self.volumes = tuple(tuple(v) for v in volumes)

    @property
    def name(self) -> str:
        """Short identifier for the backend: ``"docker"``."""
        return "docker"

    @property
    def wallclock_overhead_s(self) -> int:
        """Docker container start/teardown have near-zero overhead; use small buffer."""
        return 30

    def _build_cmd(
        self,
        workdir: Path,
        container: str,
        env: Mapping[str, str],
        mounts: Sequence[tuple[str, str]],
    ) -> list[str]:
        """Build the ``docker run`` argv: workdir + extra mounts, env, and image.

        Parameters
        ----------
        workdir : pathlib.Path
            Host directory bind-mounted at
            :data:`~open_atp.backends.base.WORKDIR_MOUNT`.
        container : str
            ``--name`` for the container, so a run can later be ``docker kill``ed.
        env : Mapping[str, str]
            Per-call environment variables, merged over the backend's :attr:`env`.
        mounts : Sequence[tuple[str, str]]
            Extra ``(host_path, container_path)`` bind mounts appended after
            :attr:`volumes`.

        Returns
        -------
        list[str]
            The ``docker run`` argv, up to but not including the in-container command.
        """
        # Detached keep-alive container; commands land via ``docker exec``.
        cmd = ["docker", "run", "--rm", "-d"]
        cmd += ["--name", container]
        cmd += ["-v", f"{workdir.resolve()}:{WORKDIR_MOUNT}"]
        # Backend-level mounts (baked in) then per-call mounts (e.g. credential dirs).
        for host, dest in (*self.volumes, *mounts):
            cmd += ["-v", f"{host}:{dest}"]
        for key, value in {**self.env, **env}.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += [self.image.name]
        return cmd

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
            Host directory bind-mounted at
            :data:`~open_atp.backends.base.WORKDIR_MOUNT` for the session's life.
        timeout_s : int
            Unused by Docker (the container has no built-in cap).
        env : Mapping[str, str], optional
            Environment variables pinned on the container at creation, merged over the
            backend's ``env``.
        mounts : Sequence[tuple[str, str]], optional
            Extra ``(host_path, container_path)`` bind mounts wired at creation (Docker
            can't add them per-exec).

        Returns
        -------
        ComputeSession
            A live :class:`DockerSession` over the workdir.
        """
        self._require_image()
        container = f"open-atp-{uuid.uuid4().hex[:12]}"
        argv = self._build_cmd(workdir, container, env or {}, mounts or ())
        argv += ["sleep", "infinity"]
        log.debug(
            "docker session start",
            extra={
                "container": container,
                "image": self.image.name,
                "env_keys": sorted({**self.env, **(env or {})}),
            },
        )
        try:
            proc = subprocess.run(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
            )
        except FileNotFoundError as exc:
            # No ``docker`` on PATH: the substrate isn't there to run against.
            log.error("docker binary not found", extra={"container": container})
            raise ProvisionError(f"docker: {exc}") from exc
        if proc.returncode != 0:
            # Daemon down or bad mount -- the image was checked above, so the
            # container never came up for some other reason.
            stderr = proc.stderr.strip() or "(no stderr)"
            log.error(
                "docker session container failed to start",
                extra={"container": container, "stderr": stderr},
            )
            raise ProvisionError(f"docker run: {stderr}")
        return DockerSession(backend=self, container=container)

    def _require_image(self) -> None:
        """Fail fast if the sandbox image isn't present locally."""
        try:
            proc = subprocess.run(
                ["docker", "image", "inspect", self.image.name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise ProvisionError(f"docker: {exc}") from exc
        if proc.returncode != 0:
            raise ImageUnavailable(
                f"docker image {self.image.name!r} not found locally; build it "
                f"with `open-atp build-docker-image`"
            )


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

        The command is wrapped in coreutils ``timeout`` so it is killed (only the exec,
        not the container) once ``timeout_s`` elapses, leaving the session up for the
        next command.
        """
        capped = (
            f"timeout --kill-after={TIMEOUT_KILL_AFTER_S} {timeout_s} "
            f"bash -lc {shlex.quote(wrap_command(command))}"
        )
        argv = ["docker", "exec"]
        for key, value in (env or {}).items():
            argv += ["-e", f"{key}={value}"]
        argv += [self.container, "bash", "-lc", capped]
        log.debug(
            "docker exec",
            extra={
                "container": self.container,
                "env_keys": sorted(env or {}),
                "timeout_s": timeout_s,
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
            popen=popen,
            container=self.container,
            started_at=time.time(),
            budget_s=timeout_s,
            deadline_s=timeout_s + TIMEOUT_KILL_AFTER_S + EXEC_DEADLINE_MARGIN_S,
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
