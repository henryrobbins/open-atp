"""Base prover abstraction.

An :class:`AutomatedProver` is a *candidate generator*: it takes a
:class:`~open_afps.core.task.ProofTask` and produces completed Lean files. The base
class owns the shared lifecycle -- generate, then verify in the sandbox -- so that
subclasses only implement ``prove`` and every prover (including Aristotle) gets the
same final check for free.
"""

from __future__ import annotations

import abc
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from open_afps.backends.base import ComputeBackend
from open_afps.core.result import GenerationOutput, ProofResult
from open_afps.core.task import ProofTask
from open_afps.core.verifier import Verifier


def logs_dir_for(workdir: Path) -> Path:
    """The logs directory paired with ``workdir`` (``<run>/<label>/logs``).

    The platform stages each run as ``<run>/<label>/{wd,logs}/``; ``workdir`` is the
    ``wd`` half (the proof project) and this is its ``logs`` sibling. Provers that
    write rich logs inside the sandbox (Vibe, ax-prover, Aristotle) relocate them
    here so ``download_wd`` stays the proof project and ``download_logs`` carries the
    full record. Kept in one place so every prover agrees on the convention.
    """
    return workdir.parent / "logs"


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

    #: Filename for the run's captured primary output under ``logs/``. For agentic
    #: provers this is the streamed event JSONL; Aristotle (no stream) overrides it
    #: with its run summary.
    stream_log_name: str = "stdout.jsonl"

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
    def prove(self, task: ProofTask, workdir: Path) -> GenerationOutput:
        """Produce completed files for ``task`` inside ``workdir``.

        Implementations must leave ``workdir`` containing the full completed project
        so the verifier can compile it in place, and return a
        :class:`GenerationOutput` describing what was produced.
        """

    def run(self, task: ProofTask, workdir: Path) -> ProofResult:
        """Full lifecycle: reject-on-mismatch, generate, verify, package result."""
        self.verifier.check_compatible(task.project)

        # Create the logs sibling up front so ``prove`` (which relocates harness-
        # specific rich logs here) and the post-run materialization both have a target.
        logs_dir = logs_dir_for(workdir)
        logs_dir.mkdir(parents=True, exist_ok=True)

        start = time.monotonic()
        output = self.prove(task, workdir)
        duration = time.monotonic() - start

        # Verify the project now living in workdir (subclasses sync results there).
        from open_afps.core.task import LeanProject

        # A prover that ran its generation and the final check in one shared sandbox
        # (the agent/verify backend-reuse path) reports the report on ``output``; reuse
        # it rather than spinning a second sandbox. Otherwise verify standalone --
        # Aristotle (no sandbox) and the split-backend case land here.
        report = output.verification or self.verifier.verify(LeanProject(workdir))

        result = ProofResult(
            prover=self.name,
            verification=report,
            completed_files=output.completed_files,
            cost_usd=output.cost_usd,
            duration_s=duration,
            logs=output.logs,
            artifacts_dir=workdir,
            logs_dir=logs_dir,
            metadata=output.metadata,
        )
        self._write_logs(output, result, logs_dir)
        return result

    def _write_logs(
        self, output: GenerationOutput, result: ProofResult, logs_dir: Path
    ) -> None:
        """Materialize the common log files alongside any relocated rich records.

        The captured primary output (``output.logs`` -- the streamed agent JSONL) and
        ``stderr`` are written uniformly; harness-specific records were already moved
        into ``logs_dir`` by ``prove``. ``result.json`` is a self-describing summary so
        a downloaded logs dir stands on its own.
        """
        (logs_dir / self.stream_log_name).write_text(output.logs)
        if output.stderr:
            (logs_dir / "stderr.txt").write_text(output.stderr)
        (logs_dir / "result.json").write_text(
            json.dumps(result.to_dict(), indent=2, default=str)
        )
