"""Compute backend abstraction.

A :class:`ComputeBackend` is the single load-bearing primitive of the platform:
"run a command over a working directory inside a Lean+Mathlib sandbox, and give me
back the output." It is used in two distinct roles:

* **Agentic execution** -- run a coding-agent launch script (AgentProver, NuminaProver).
* **Verification** -- run ``lake env lean ...`` over candidate files (every prover).
"""

from __future__ import annotations

import abc
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from open_atp.images import DEFAULT_IMAGE, Image

#: Working directory inside the sandbox -- Docker bind-mounts the host workdir here,
#: Modal pushes it here. One convention shared by both backends.
WORKDIR_MOUNT = "/workspace/wd"
#: Image-baked warm Mathlib olean cache to symlink the workdir's ``.lake`` to.
BAKED_LAKE = "/workspace/.lake"
#: Exit code coreutils ``timeout`` reports when it kills a command past its budget.
TIMEOUT_EXIT_CODE = 124
#: Grace before coreutils ``timeout`` escalates SIGTERM to SIGKILL -- a window for a
#: killed agent to flush its final usage/cost record before it dies.
TIMEOUT_KILL_AFTER_S = 30


class ComputeError(RuntimeError):
    """Parent Exception class for backend compute errors."""


class ExecTimeout(ComputeError):
    """A sandbox operation stalled past its client-side wall-clock deadline.

    An *infra* stall -- a backend operation (a push/pull, a warm build, a
    ``proc.wait``) that did not complete before the client gave up on it, typically an
    unresponsive worker. Distinct from a command using up its own budget, which the
    backend surfaces as :class:`CommandTimeout`.
    """


class SandboxDead(ComputeError):
    """The backend compute sandbox was terminated or reaped mid-run."""


class TransferError(ComputeError):
    """A file transfer into or out of the sandbox failed."""


class ProvisionError(ComputeError):
    """The sandbox/container failed to come up for a run."""


class ImageUnavailable(ProvisionError):
    """The OpenATP Docker image is not available to the backend.

    The most likely cause is the image has not yet been built. Build with the
    ``open-atp build-docker-image`` and ``open-atp build-modal-image`` commands.
    """


class CommandTimeout(Exception):
    """A sandbox command was killed for exceeding its own wall-clock budget.

    Attributes
    ----------
    result : CommandResult or None
        The command's partial output captured before it was killed, when available.
    """

    def __init__(self, message: str, *, result: CommandResult | None = None) -> None:
        super().__init__(message)
        self.result = result


@dataclass
class CommandResult:
    """Outcome of a finished command run inside a backend.

    Attributes
    ----------
    exit_code : int
        The command's process exit code (``0`` on success).
    stdout : str
        The full captured standard output.
    stderr : str
        The full captured standard error.
    duration_s : float
        Wall-clock time the command took, in seconds.
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_s: float


@dataclass
class CommandHandle:
    """A live command running in a session's sandbox. Backends fill the hooks.

    Streaming matters for agents (we want incremental stdout for cost/progress
    parsing) but is equally usable for a blocking ``lake`` invocation via
    :meth:`wait`. The handle owns no teardown: the sandbox is the session's, torn
    down by :meth:`ComputeSession.close`.
    """

    def stream(self) -> Iterator[str]:
        """Yield stdout lines as they arrive.

        Yields
        ------
        str
            Each line of the command's standard output, newline-stripped, as produced.
        """
        raise NotImplementedError

    def wait(self) -> CommandResult:
        """Drain to completion and return the final result.

        Returns
        -------
        CommandResult
            Exit code, captured stdout/stderr, and wall-clock duration.
        """
        raise NotImplementedError


class ComputeSession(AbstractContextManager["ComputeSession"]):
    """A persistent sandbox over a workdir, exec'd many times, torn down once.

    The one-shot :meth:`ComputeBackend.run` creates a sandbox, runs a *single*
    command, and tears it down. A session keeps the sandbox alive so several commands
    can run against the same hot filesystem -- the agent's generation *then* the
    verifier's compile -- without paying a second spin-up.

    Lifecycle invariant: teardown lives in :meth:`close` (not in the handle's
    ``wait``), so a session MUST be used as a context manager and :meth:`close` MUST be
    idempotent -- otherwise an error between exec and close leaks the sandbox.

    Examples
    --------

    Open a session over a workdir and run several commands against the same hot
    sandbox -- here generation followed by the compile, with one spin-up (needs a
    live backend, so this is illustrative rather than a doctest):

    .. code-block:: python

        from open_atp.backends.docker import DockerBackend
        from open_atp.images import DEFAULT_IMAGE

        backend = DockerBackend(image=DEFAULT_IMAGE)
        with backend.session(workdir, timeout_s=1800) as session:
            result = session.exec("lake env lean Demo.lean", timeout_s=300).wait()
            session.sync_out()   # pull the agent's edits back to the host
        # the sandbox is torn down on exit, even if exec raised
    """

    def exec(
        self,
        command: str,
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandHandle:
        """Run ``command`` in the live sandbox; the handle does NOT tear it down.

        Parameters
        ----------
        command : str
            The shell command to run in the live sandbox.
        timeout_s : int
            Wall-clock cap for the command, in seconds. When the command exceeds this
            budget it is killed, surfaced as :class:`CommandTimeout` from
            :meth:`~CommandHandle.wait`.
        env : Mapping[str, str], optional
            Per-command environment variables, merged over the backend's ``env``.
            Default empty.

        Returns
        -------
        CommandHandle
            A live handle to stream or :meth:`~CommandHandle.wait` on. Its teardown hook
            does NOT close the session.
        """
        raise NotImplementedError

    def sync_out(self) -> None:
        """Pull the sandbox workdir back to the host (no-op when bind-mounted)."""
        raise NotImplementedError

    def sync_in(self) -> None:
        """Push the host workdir into the sandbox (no-op when bind-mounted)."""
        raise NotImplementedError

    def close(self) -> None:
        """Tear the sandbox down (pull artifacts first where needed). Idempotent."""
        raise NotImplementedError

    def __exit__(self, *exc: object) -> None:
        self.close()


def wrap_command(command: str) -> str:
    """``cd`` into the workdir mount and wire the warm Mathlib cache before ``command``.

    The one image-layout convention the backends own: symlink the workdir's ``.lake``
    to the image-baked olean cache so uploaded projects reuse it. Identical for Docker
    (bind mount) and Modal (pushed dir), so it lives here rather than in either backend,
    over the shared :data:`WORKDIR_MOUNT` / :data:`BAKED_LAKE` layout.

    Parameters
    ----------
    command : str
        The command to run once the cache is wired.

    Returns
    -------
    str
        A single shell string: the cache prep followed by ``command``.
    """
    prep = f"cd {WORKDIR_MOUNT}"
    if BAKED_LAKE:
        prep += f" && {{ [ -e {BAKED_LAKE} ] && ln -sfn {BAKED_LAKE} .lake || true; }}"
    return f"{prep}; {command}"


class ComputeBackend(abc.ABC):
    """Runs commands over a workdir in a sandbox carrying Lean + Mathlib.

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

    Attributes
    ----------
    name : str
        Short identifier, e.g. ``"docker"`` or ``"modal"``.
    wallclock_overhead_s : int
        Wall-clock time budget required beyond a command's timeout, in seconds.
    """

    #: Absolute ``$HOME`` inside the sandbox; per-run credential dirs (an agent's
    #: ``~/.codex``, say) are mounted under it. Overridden per backend.
    container_home: str = "/root"

    def __init__(
        self,
        *,
        image: Image | Mapping[str, object] = DEFAULT_IMAGE,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.image = (
            image
            if isinstance(image, Image)
            else Image(**cast("Mapping[str, str]", image))
        )
        self.env = dict(env or {})

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Short identifier, e.g. ``"docker"`` or ``"modal"``."""

    @property
    @abc.abstractmethod
    def wallclock_overhead_s(self) -> int:
        """Wall-clock time budget required beyond a command's timeout, in seconds.

        The ``timeout_s`` passed to :meth:`run`/:meth:`session` is the *command's*
        wall-clock budget. The backend may need extra time for
        spin-up, teardown, file transfer, etc. The time allotted for this overhead
        is captured here, so a caller can bound the *total* wall-clock for a run.
        """

    @abc.abstractmethod
    def session(
        self,
        workdir: Path,
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
    ) -> ComputeSession:
        """Open a persistent sandbox over ``workdir`` for multiple :meth:`exec` calls.

        Unlike :meth:`run` (one command, then teardown), the returned
        :class:`ComputeSession` stays alive until :meth:`ComputeSession.close`, so the
        same hot sandbox can run generation *and* verification back to back.

        ``env``/``mounts`` here pin the long-lived sandbox at creation (Docker bind
        mounts can only be set at ``docker run``); per-command credentials go to
        :meth:`ComputeSession.exec`'s own ``env``.

        Parameters
        ----------
        workdir : pathlib.Path
            Host directory mounted/synced into the sandbox for the session's life.
        timeout_s : int
            Wall-clock cap for the sandbox, in seconds.
        env : Mapping[str, str], optional
            Environment variables pinned on the sandbox at creation, merged over the
            backend's :attr:`env`. Default empty.
        mounts : Sequence[tuple[str, str]], optional
            Extra ``(host_path, container_path)`` mounts pinned at creation. Default
            empty.

        Returns
        -------
        ComputeSession
            A live session over the workdir; close it via
            :meth:`ComputeSession.close` (use as a context manager).
        """

    def run(
        self,
        workdir: Path,
        command: str,
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
    ) -> CommandResult:
        """Run a single ``command`` in a fresh sandbox over ``workdir``, then tear down.

        A one-shot :meth:`session`: the workdir (and any ``mounts``) is synced in,
        the command runs to completion, and the sandbox is torn down -- pulling file
        mutations (completed proofs) back to the host on the way out.

        Parameters
        ----------
        workdir : pathlib.Path
            Host directory mounted/synced into the sandbox; mutations sync back out.
        command : str
            The shell command to run inside the sandbox.
        timeout_s : int
            Wall-clock cap for the command, in seconds.
        env : Mapping[str, str], optional
            Per-call environment variables, merged over the backend's :attr:`env`.
            Default empty.
        mounts : Sequence[tuple[str, str]], optional
            Extra ``(host_path, container_path)`` bind mounts beyond the workdir.
            Default empty.

        Returns
        -------
        CommandResult
            Exit code, captured stdout/stderr, and wall-clock duration.
        """
        with self.session(
            workdir, timeout_s=timeout_s, env=env, mounts=mounts
        ) as session:
            return session.exec(command, timeout_s=timeout_s).wait()

    def test(self) -> bool:
        """Smoke-test the backend by verifying a trivial proof end to end.

        Returns
        -------
        bool
            Whether the trivial proof verified in this backend.
        """
        import tempfile

        from open_atp.lean import create_project
        from open_atp.verify import Verifier

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lean_file = tmp_path / "Trivial.lean"
            lean_file.write_text("theorem trivial_proof : True := trivial\n")
            project = create_project([lean_file], tmp_path / "project")
            report = Verifier(self).verify(project)
            return report is not None and report.verified
