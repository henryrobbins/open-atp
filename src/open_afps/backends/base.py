"""Compute backend abstraction.

A :class:`ComputeBackend` is the single load-bearing primitive of the platform:
"run a command over a working directory inside a Lean+Mathlib sandbox, and give me
back the output." It is used in two distinct roles:

* **Agentic execution** -- run a coding-agent launch script (AgentProver, NuminaProver).
* **Verification** -- run ``lake env lean ...`` over candidate files (every prover).

This generalises milp_flare's agent-specific ``Runner``/``AgentRun`` into a plain
command runner. Concrete backends (Docker, Modal) are ports of milp_flare's
``runner/docker.py`` and ``runner/modal.py``.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CommandResult:
    """Outcome of a finished command run inside a backend."""

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
        """Yield stdout lines as they arrive."""
        raise NotImplementedError

    def cancel(self) -> None:
        """Best-effort termination (e.g. ``docker kill`` / sandbox terminate)."""
        raise NotImplementedError

    def wait(self) -> CommandResult:
        """Drain to completion and return the final result."""
        raise NotImplementedError

    def __exit__(self, *exc: object) -> None:
        self.cancel()


@dataclass
class BackendConfig:
    """Shared backend knobs. Subclasses add their own (Docker mounts, Modal CPU…)."""

    image: str
    timeout_s: int = 1800
    env: Mapping[str, str] = field(default_factory=dict)


def wrap_command(workdir_mount: str, baked_lake: str, command: str) -> str:
    """``cd`` into the workdir mount and wire the warm Mathlib cache before ``command``.

    The one image-layout convention the backends own (mirroring milp_flare's
    ``entrypoint.sh``): symlink the workdir's ``.lake`` to the image-baked olean cache
    so uploaded projects reuse it. Identical for Docker (bind mount) and Modal (pushed
    dir), so it lives here rather than in either backend. ``baked_lake`` empty skips
    the symlink.
    """
    prep = f"cd {workdir_mount}"
    if baked_lake:
        prep += f" && {{ [ -e {baked_lake} ] && ln -sfn {baked_lake} .lake || true; }}"
    return f"{prep}; {command}"


class ComputeBackend(abc.ABC):
    """Runs commands over a workdir in a sandbox carrying Lean + Mathlib."""

    #: Absolute ``$HOME`` inside the sandbox; per-run credential dirs (an agent's
    #: ``~/.codex``, say) are mounted under it. Overridden per backend.
    container_home: str = "/root"

    def __init__(self, config: BackendConfig) -> None:
        self.config = config

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
        """Convenience: :meth:`start` then block via :meth:`CommandHandle.wait`."""
        with self.start(
            workdir, command, env=env, mounts=mounts, timeout_s=timeout_s
        ) as handle:
            return handle.wait()
