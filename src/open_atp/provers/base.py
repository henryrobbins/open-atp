"""Base prover abstraction.

An :class:`AutomatedProver` is a *candidate generator*: it takes a
:class:`~open_atp.lean.ProofTask` and a caller-chosen output directory, fills
the project's ``sorry``\\s, verifies the result in a shared sandbox, and returns a
:class:`ProofResult`. The base class owns the shared lifecycle
-- stage the output layout, generate, then verify -- so subclasses only implement
``_generate`` and every prover (including Aristotle) gets the same final check for
free. Concrete provers live alongside this base in ``open_atp.provers``.

The public entry point is :meth:`AutomatedProver.prove`. A caller constructs a prover
directly (or via :func:`open_atp.standard_prover`) and calls it::

    result = prover.prove(task, output_dir)

``prove`` populates ``output_dir/{wd,logs}/``: ``wd`` is the completed lake project
and ``logs`` is the run record.
"""

from __future__ import annotations

import abc
import enum
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from open_atp.backends.base import ComputeBackend, ComputeError, ExecTimeout
from open_atp.lean import LeanProject, ProofTask
from open_atp.verify import VerificationReport, Verifier

log = logging.getLogger("open_atp")

#: Heading the optional per-task ``user_prompt`` is appended under.
_ADDITIONAL_INSTRUCTIONS = "\n\n# Additional instructions\n\n{user_prompt}"


class ProofStatus(enum.Enum):
    """Coarse outcome bucket for a :class:`ProofResult`.

    Deliberately small: each member is a *distinct caller action* (score it, retry
    it, retry with more budget, file a bug), not a distinct cause. Same-bucket runs
    are told apart by the free-text :attr:`ProofResult.error`, so the enum stays the
    index and the string carries the "why".

    Every bucket is an outcome of a run that *started*: :meth:`AutomatedProver.prove`
    rejects an incompatible input (toolchain / Mathlib pin mismatch) by raising, before
    any run exists, so there is no ``input_error`` bucket.
    """

    #: Verified proof: compiles, ``sorry``-free, no foreign axioms.
    VERIFIED = "verified"
    #: Ran to completion but the candidate did not verify -- a genuine miss.
    UNVERIFIED = "unverified"
    #: Generation or verification exceeded its wall-clock budget.
    TIMEOUT = "timeout"
    #: The compute substrate failed (sandbox disconnect, worker gone, pull failed).
    INFRA_ERROR = "infra_error"
    #: Any other failure of a started run -- an unexpected prover bug, or a caller
    #: surfacing an escaped exception it could not classify.
    ERROR = "error"


def _status_for_exception(exc: BaseException) -> ProofStatus:
    """Classify an exception from a *started* run onto a :class:`ProofStatus`.

    Maps the backend's typed operational failures (timeout, sandbox loss); anything
    else is :attr:`ProofStatus.ERROR`.
    """
    if isinstance(exc, ExecTimeout):
        return ProofStatus.TIMEOUT
    if isinstance(exc, ComputeError):
        return ProofStatus.INFRA_ERROR
    return ProofStatus.ERROR


def compose_prompt(prover_prompt: str, user_prompt: str | None) -> str:
    """Combine a prover's own prompt with the task's optional user prompt.

    ``prover_prompt`` is the prover-specific instruction set handed to the agent;
    ``user_prompt`` is the optional per-task guidance from
    :attr:`~open_atp.lean.ProofTask.user_prompt`. When present it is appended under an
    ``# Additional instructions`` heading; when absent (the common case) the prover
    prompt is returned unchanged.
    """
    if user_prompt:
        return prover_prompt + _ADDITIONAL_INSTRUCTIONS.format(user_prompt=user_prompt)
    return prover_prompt


@dataclass
class ProofResult:
    """What a prover returns from :meth:`AutomatedProver.prove`.

    The prover writes its artifacts into the caller-chosen :attr:`output_dir`, laid
    out as ``output_dir/{wd,logs}/``: ``wd`` is the completed lake project (the proof
    output) and ``logs`` is the run record (the streamed agent ``stdout.txt``,
    ``stderr.txt``, ``result.json``, and any harness-specific rich logs). This object
    just records where those live, plus the verification verdict and run metadata.

    Attributes
    ----------
    prover : str
        Name of the prover that produced this result.
    verification : VerificationReport or None
        The shared verification of the completed project, or ``None`` when the run
        failed before a candidate could be verified (see :attr:`error`).
    output_dir : pathlib.Path
        The run's output directory. Holds the :attr:`wd` (proof project) and
        :attr:`logs_dir` (run record) subdirectories the prover populated.
    completed_files : dict[str, str]
        The completed ``.lean`` sources, keyed by file path relative to the project
        root.
    cost_usd : float, optional
        Estimated USD cost of the run. ``None`` when the prover does not report cost.
    duration_s : float, optional
        Wall-clock duration of the run, in seconds.
    metadata : dict[str, object]
        Harness-specific run metadata (token counts, run summaries, ...).
    error : str, optional
        A one-line ``"<ExcType>: <message>"`` summary, set when a started run failed
        (a timeout, an infra failure, or an unexpected error); ``None`` on a clean
        verify. The full traceback goes to the logs -- this is the short human-facing
        detail that rides in :meth:`to_dict`. :attr:`status` is the coarse bucket, this
        the "why".
    status : ProofStatus
        Coarse outcome bucket (see :class:`ProofStatus`).
    wd : pathlib.Path
        The completed working directory (``output_dir/wd``) -- a complete lake project
        with the completed ``.lean`` files. The proof output.
    logs_dir : pathlib.Path
        The run's logs directory (``output_dir/logs``) -- the captured agent stream
        (``stdout.txt``), ``stderr.txt``, ``result.json``, and any harness-specific
        rich record (Vibe's session log, ax-prover's per-target logs, Aristotle's
        events).
    success : bool
        True iff :attr:`verification` exists and is
        :attr:`~open_atp.verify.VerificationReport.verified`.
    """

    prover: str
    verification: VerificationReport | None
    output_dir: Path
    completed_files: dict[str, str] = field(default_factory=dict)
    cost_usd: float | None = None
    duration_s: float | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    error: str | None = None
    status: ProofStatus = ProofStatus.ERROR

    @classmethod
    def errored(cls, prover: str, output_dir: Path, exc: BaseException) -> ProofResult:
        """Build a minimal failed result from an exception that escaped :meth:`prove`.

        For the two cases a caller cannot get a run record from ``prove`` itself: an
        input rejected before the run started, and a run abandoned past a hard
        wall-clock ceiling. The exception is classified onto :attr:`status` and
        summarized in :attr:`error`; :attr:`verification` stays ``None`` and no run
        artifacts are attached.
        """
        return cls(
            prover=prover,
            verification=None,
            output_dir=output_dir,
            error=f"{type(exc).__name__}: {exc}",
            status=_status_for_exception(exc),
        )

    @property
    def wd(self) -> Path:
        """The completed proof project (``output_dir/wd``)."""
        return self.output_dir / "wd"

    @property
    def logs_dir(self) -> Path:
        """The run's logs directory (``output_dir/logs``)."""
        return self.output_dir / "logs"

    @property
    def success(self) -> bool:
        return bool(self.verification and self.verification.verified)

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view: inline files, verification, cost, and artifact paths."""
        return {
            "prover": self.prover,
            "success": self.success,
            "status": self.status.value,
            "error": self.error,
            "verification": self.verification.to_dict() if self.verification else None,
            "completed_files": dict(self.completed_files),
            "cost_usd": self.cost_usd,
            "duration_s": self.duration_s,
            "output_dir": str(self.output_dir),
            "wd": str(self.wd),
            "logs_dir": str(self.logs_dir),
            "metadata": dict(self.metadata),
        }


class AutomatedProver(abc.ABC):
    """Generate candidate proofs, then verify them in a shared sandbox.

    The sandbox image (its tag plus the Lean toolchain + Mathlib pins the shared
    verifier checks every project against) comes from ``backend`` -- a prover inherits
    whatever image its backend runs.

    Parameters
    ----------
    backend : ComputeBackend
        The one backend for this prover. Agentic provers reuse it (via a live session)
        for generation, then verify in that hot sandbox; Aristotle uses it only for the
        final check.
    timeout_s : int
        Wall-clock budget for the generation run, in seconds. Default ``1800``.

    Attributes
    ----------
    max_duration_s : int
        Maximum wall-clock duration of a healthy :meth:`prove` run, in seconds.
    """

    name: str = "base"

    def __init__(
        self,
        *,
        backend: ComputeBackend,
        timeout_s: int = 1800,
    ) -> None:
        # timeout_s bounds generation; the Verifier keeps its own (600s default)
        # budget for the post-generation compile/axiom check.
        self.timeout_s = timeout_s
        self.verifier = Verifier(backend)

    @property
    def max_duration_s(self) -> int:
        """Maximum wall-clock duration of a healthy :meth:`prove` run, in seconds.

        The total wall-clock time is the sum of:
        - the proof generation budget
        - post-generation verification
        - and the backend's overhead
        """
        return (
            self.timeout_s
            + self.verifier.timeout_s
            + self.verifier.backend.wallclock_overhead_s
        )

    @abc.abstractmethod
    def _generate(
        self, task: ProofTask, wd: Path, logs_dir: Path, result: ProofResult
    ) -> None:
        """Generate the completed project in ``wd`` and record the run in ``result``.

        Implementations must leave ``wd`` containing the full completed project so the
        verifier can compile it in place, write the run's logs into ``logs_dir``, and
        fill ``result`` (``completed_files``, ``cost_usd``, ``metadata``). A prover that
        already verified the candidate in its own live sandbox sets
        ``result.verification`` itself; otherwise :meth:`prove` runs the shared check.
        """

    def prove(self, task: ProofTask, output_dir: Path | str) -> ProofResult:
        """Full lifecycle: reject-on-mismatch, generate, verify, write the result.

        Parameters
        ----------
        task : ~open_atp.lean.ProofTask
            The unit of work: the lake project to complete, the optional
            :attr:`~open_atp.lean.ProofTask.targets` to focus on, and any
            :attr:`~open_atp.lean.ProofTask.user_prompt` guidance.
        output_dir : pathlib.Path or str
            Caller-chosen output directory, populated as ``output_dir/{wd,logs}/``:
            ``wd`` is the completed lake project (the proof output) and ``logs`` is
            the run record (the agent ``stdout.txt``/``stderr.txt``, ``result.json``,
            and any harness-specific rich logs).

        Returns
        -------
        ProofResult
            The outcome of the run, pointing at the populated :attr:`~ProofResult.wd`
            and :attr:`~ProofResult.logs_dir`. Once a run starts, its failure is a
            *record*, not a raise: a timeout, an infra failure, or an unexpected error
            comes back with the matching :attr:`~ProofResult.status` and a
            :attr:`~ProofResult.error` summary, keeping the partial workdir, logs, and
            cost for inspection.

        Raises
        ------
        ~open_atp.lean.ToolchainMismatch
            If the project's toolchain differs from the backend image's. Checked up
            front, before any run starts -- so this raises rather than returning an
            empty result.
        ~open_atp.lean.MathlibRevMismatch
            If the project records a Mathlib revision that differs from the backend
            image's. Checked up front, before any run starts.
        """
        # Bind task (when named) + prover + a per-run id onto the context so every
        # downstream event -- including backend/verify records that never see ``self``
        # -- self-attributes. Runs execute in their own thread (benchmark) or the main
        # thread (CLI), each with an isolated contextvars context, so the binding never
        # bleeds across runs.
        binding = {"prover": self.name, "run_id": uuid.uuid4().hex[:12]}
        if task.name is not None:
            binding["task"] = task.name
        with structlog.contextvars.bound_contextvars(**binding):
            self.verifier.check_compatible(task.project)

            output_dir = Path(output_dir)
            wd = output_dir / "wd"
            logs_dir = output_dir / "logs"
            wd.mkdir(parents=True, exist_ok=True)
            logs_dir.mkdir(parents=True, exist_ok=True)

            result = ProofResult(
                prover=self.name, verification=None, output_dir=output_dir
            )
            log.info(
                "prove",
                extra={
                    "backend": self.verifier.backend.name,
                    "timeout_s": self.timeout_s,
                    "output_dir": str(output_dir),
                },
            )
            start = time.monotonic()
            try:
                self._generate(task, wd, logs_dir, result)
                # An agentic prover ran generation and the final check in one live
                # session and set ``result.verification`` itself; reuse it rather than
                # spinning a second sandbox. Only Aristotle (network generation, no
                # session) lands here and gets the standalone check.
                if result.verification is None:
                    result.verification = self.verifier.verify(LeanProject(wd))
                result.status = (
                    ProofStatus.VERIFIED if result.success else ProofStatus.UNVERIFIED
                )
            except Exception as exc:
                # The run started, so its failure is a record, not a raise: the partial
                # workdir/logs/cost stay on ``result`` for the caller to inspect. The
                # traceback is logged here; ``error`` carries the one-line summary.
                log.exception("prove failed")
                result.status = _status_for_exception(exc)
                result.error = f"{type(exc).__name__}: {exc}"
            result.duration_s = time.monotonic() - start

            # A self-describing summary so a downloaded logs dir stands on its own.
            (logs_dir / "result.json").write_text(
                json.dumps(result.to_dict(), indent=2, default=str)
            )
            log.info(
                "prove complete",
                extra={
                    "status": result.status.value,
                    "success": result.success,
                    "cost_usd": result.cost_usd,
                    "duration_s": round(result.duration_s, 1),
                },
            )
            return result
