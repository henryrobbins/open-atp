"""Base prover abstraction.

An :class:`AutomatedProver` is a *candidate generator*: it takes a
:class:`~open_afps.core.task.ProofTask` and produces completed Lean files. The base
class owns the shared lifecycle -- generate, then verify in the sandbox -- so that
subclasses only implement ``prove`` and every prover (including Aristotle) gets the
same final check for free.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from pathlib import Path

from open_afps.backends.base import ComputeBackend
from open_afps.core.result import ProofResult
from open_afps.core.task import ProofTask
from open_afps.core.verifier import Verifier


@dataclass
class AutomatedProverConfig:
    """Base configuration shared by all provers.

    Subclasses extend with their own knobs:

    * ``AgentProverConfig``: harness (claude/opencode/codex), effort, skills, MCP.
    * ``NuminaProverConfig``: extends the agent config + max_rounds, helper API keys.
    * ``AristotleProverConfig``: api key, mode, poll interval.
    """

    # Sandbox image carrying the supported Lean toolchain + Mathlib. Also the image
    # the shared verifier checks every project against.
    image: str
    # Toolchain pinned inside ``image``; projects must match it (else verifier rejects).
    supported_toolchain: str
    timeout_s: int = 1800
    env: dict[str, str] = field(default_factory=dict)


class AutomatedProver(abc.ABC):
    """Generate candidate proofs, then verify them in a shared sandbox."""

    name: str = "base"

    def __init__(
        self, config: AutomatedProverConfig, verification_backend: ComputeBackend
    ) -> None:
        self.config = config
        # The backend used for the *final check*. Agentic provers additionally run
        # their generation in a backend; that is the subclass's concern.
        self.verifier = Verifier(
            verification_backend, supported_toolchain=config.supported_toolchain
        )

    @abc.abstractmethod
    def prove(self, task: ProofTask, workdir: Path) -> dict[str, str]:
        """Produce completed files for ``task`` inside ``workdir``.

        Returns a mapping of project-relative path -> file contents for the files the
        prover modified. Implementations should leave ``workdir`` containing the full
        completed project so the verifier can compile it in place.
        """

    def run(self, task: ProofTask, workdir: Path) -> ProofResult:
        """Full lifecycle: reject-on-mismatch, generate, verify, package result."""
        self.verifier.check_compatible(task.project)

        start = time.monotonic()
        completed = self.prove(task, workdir)
        duration = time.monotonic() - start

        # Verify the project now living in workdir (subclasses sync results there).
        from open_afps.core.task import LeanProject

        report = self.verifier.verify(LeanProject(workdir))

        return ProofResult(
            prover=self.name,
            verification=report,
            completed_files=completed,
            duration_s=duration,
            artifacts_dir=workdir,
        )
