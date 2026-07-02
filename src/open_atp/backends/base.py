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
class CommandHandle(AbstractContextManager["CommandHandle"]):
    """A live, streaming command. Concrete backends subclass and fill the hooks.

    Streaming matters for agents (we want incremental stdout for cost/progress
    parsing) but is equally usable for a blocking ``lake`` invocation via
    :meth:`wait`.
    """

    def stream(self) -> Iterator[str]:
        """Yield stdout lines as they arrive.

        Yields
        ------
        str
            Each line of the command's standard output, newline-stripped, as produced.
        """
        raise NotImplementedError

    def cancel(self) -> None:
        """Best-effort termination (e.g. ``docker kill`` / sandbox terminate)."""
        raise NotImplementedError

    def wait(self) -> CommandResult:
        """Drain to completion and return the final result.

        Returns
        -------
        CommandResult
            Exit code, captured stdout/stderr, and wall-clock duration.
        """
        raise NotImplementedError

    def __exit__(self, *exc: object) -> None:
        self.cancel()


class ComputeSession(AbstractContextManager["ComputeSession"]):
    """A persistent sandbox over a workdir, exec'd many times, torn down once.

    The one-shot :meth:`ComputeBackend.start`/:meth:`ComputeBackend.run` pair creates
    a sandbox, runs a *single* command, and tears it down. A session keeps the sandbox
    alive so several commands can run against the same hot filesystem -- the agent's
    generation *then* the verifier's compile -- without paying a second spin-up.

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
        with backend.session(workdir) as session:
            with session.exec("lake env lean Demo.lean") as handle:
                result = handle.wait()
            session.sync_out()   # pull the agent's edits back to the host
        # the sandbox is torn down on exit, even if exec raised
    """

    def exec(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: int | None = None,
    ) -> CommandHandle:
        """Run ``command`` in the live sandbox; the handle does NOT tear it down.

        Parameters
        ----------
        command : str
            The shell command to run in the live sandbox.
        env : Mapping[str, str], optional
            Per-command environment variables, merged over the backend's ``env``.
            Default empty.
        timeout_s : int, optional
            Wall-clock cap for the command. ``None`` leaves it to the backend's own
            default. Default ``None``.

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


def wrap_command(workdir_mount: str, baked_lake: str, command: str) -> str:
    """``cd`` into the workdir mount and wire the warm Mathlib cache before ``command``.

    The one image-layout convention the backends own: symlink the workdir's ``.lake``
    to the image-baked olean cache so uploaded projects reuse it. Identical for Docker
    (bind mount) and Modal (pushed dir), so it lives here rather than in either backend.
    ``baked_lake`` empty skips the symlink.

    Parameters
    ----------
    workdir_mount : str
        Path inside the sandbox to ``cd`` into before running ``command``.
    baked_lake : str
        Image-baked olean cache to symlink the workdir's ``.lake`` to; empty skips the
        symlink.
    command : str
        The command to run once the cache is wired.

    Returns
    -------
    str
        A single shell string: the cache prep followed by ``command``.
    """
    prep = f"cd {workdir_mount}"
    if baked_lake:
        prep += f" && {{ [ -e {baked_lake} ] && ln -sfn {baked_lake} .lake || true; }}"
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

    @abc.abstractmethod
    def start(
        self,
        workdir: Path,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
        timeout_s: int | None = None,
    ) -> CommandHandle:
        """Launch ``command`` with ``workdir`` mounted/synced into the sandbox.

        The workdir is synced in before launch and synced back out on completion so
        that file mutations (completed proofs) are visible to the caller.

        ``mounts`` are extra ``(host_path, container_path)`` bind mounts beyond the
        workdir -- used to forward agent credential dirs (e.g. Codex's ``~/.codex``)
        per run, without baking them into the backend config.

        Parameters
        ----------
        workdir : pathlib.Path
            Host directory mounted/synced into the sandbox; file mutations are synced
            back out on completion.
        command : str
            The shell command to run inside the sandbox.
        env : Mapping[str, str], optional
            Per-call environment variables, merged over the backend's :attr:`env`.
            Default empty.
        mounts : Sequence[tuple[str, str]], optional
            Extra ``(host_path, container_path)`` bind mounts beyond the workdir (e.g.
            agent credential dirs). Default empty.
        timeout_s : int, optional
            Wall-clock cap for the command. ``None`` leaves it to the backend's own
            default. Default ``None``.

        Returns
        -------
        CommandHandle
            A live handle to stream or :meth:`~CommandHandle.wait` on.
        """

    @abc.abstractmethod
    def session(
        self,
        workdir: Path,
        *,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
        timeout_s: int | None = None,
    ) -> ComputeSession:
        """Open a persistent sandbox over ``workdir`` for multiple :meth:`exec` calls.

        Unlike :meth:`start` (one command, then teardown), the returned
        :class:`ComputeSession` stays alive until :meth:`ComputeSession.close`, so the
        same hot sandbox can run generation *and* verification back to back.

        ``env``/``mounts`` here pin the long-lived sandbox at creation (Docker bind
        mounts can only be set at ``docker run``); per-command credentials go to
        :meth:`ComputeSession.exec`'s own ``env``.

        Parameters
        ----------
        workdir : pathlib.Path
            Host directory mounted/synced into the sandbox for the session's life.
        env : Mapping[str, str], optional
            Environment variables pinned on the sandbox at creation, merged over the
            backend's :attr:`env`. Default empty.
        mounts : Sequence[tuple[str, str]], optional
            Extra ``(host_path, container_path)`` mounts pinned at creation. Default
            empty.
        timeout_s : int, optional
            Wall-clock cap for the sandbox. ``None`` leaves it to the backend's own
            default. Default ``None``.

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
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
        timeout_s: int | None = None,
    ) -> CommandResult:
        """Convenience: :meth:`start` then block via :meth:`CommandHandle.wait`.

        Parameters
        ----------
        workdir : pathlib.Path
            Host directory mounted/synced into the sandbox; mutations sync back out.
        command : str
            The shell command to run inside the sandbox.
        env : Mapping[str, str], optional
            Per-call environment variables, merged over the backend's :attr:`env`.
            Default empty.
        mounts : Sequence[tuple[str, str]], optional
            Extra ``(host_path, container_path)`` bind mounts beyond the workdir.
            Default empty.
        timeout_s : int, optional
            Wall-clock cap for the command. ``None`` leaves it to the backend's own
            default. Default ``None``.

        Returns
        -------
        CommandResult
            Exit code, captured stdout/stderr, and wall-clock duration.
        """
        with self.start(
            workdir, command, env=env, mounts=mounts, timeout_s=timeout_s
        ) as handle:
            return handle.wait()

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
            return Verifier(self).verify(project).verified
