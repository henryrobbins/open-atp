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

from open_atp.auth import AuthState, AuthStatus
from open_atp.backends.base import ComputeBackend, ProvisionError
from open_atp.harness.base import MissingCredentials
from open_atp.lean import ProofTask
from open_atp.verify import VerificationReport, Verifier

log = logging.getLogger("open_atp")

#: Heading the optional per-task ``user_prompt`` is appended under.
_ADDITIONAL_INSTRUCTIONS = "\n\n# Additional instructions\n\n{user_prompt}"


class GenerationTimeout(Exception):
    """The proof generation consumed its wall-clock budget before finishing."""


class ProofStatus(enum.Enum):
    """Coarse status for a :class:`ProofResult`."""

    #: Verified proof: compiles, ``sorry``-free, no foreign axioms.
    VERIFIED = "verified"
    #: Ran to completion but the candidate did not verify -- a genuine miss.
    UNVERIFIED = "unverified"
    #: The proof generation phase consumed its wall-clock budget before finishing.
    TIMEOUT = "timeout"
    #: There was an error during proof generation or verification.
    ERROR = "error"


def _status_for_exception(exc: BaseException) -> ProofStatus:
    """Classify an exception onto a :class:`ProofStatus`."""
    return (
        ProofStatus.TIMEOUT if isinstance(exc, GenerationTimeout) else ProofStatus.ERROR
    )


def _compose_prompt(prover_prompt: str, user_prompt: str | None) -> str:
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

    Parameters
    ----------
    prover : str
        Name of the prover that produced this result.
    verification : VerificationReport or None
        The shared verification of the completed project, or ``None`` when the run
        failed before a candidate could be verified (see ``error``).
    output_dir : pathlib.Path
        The run's output directory. Holds the :attr:`wd` (proof project) and
        :attr:`logs_dir` (run record) subdirectories the prover populated.
    completed_files : dict[str, str], optional
        The completed ``.lean`` sources, keyed by file path relative to the project
        root. Defaults to an empty mapping.
    cost_usd : float, optional
        Estimated USD cost of the run. ``None`` when the prover does not report cost.
    duration_s : float, optional
        Wall-clock duration of the run, in seconds.
    metadata : dict[str, object], optional
        Harness-specific run metadata (token counts, run summaries, ...). Defaults to
        an empty mapping.
    error : str, optional
        The failing exception's class name; set when status is ERROR or TIMEOUT.
    error_msg : str, optional
        The failing exception's message; set when status is ERROR or TIMEOUT.
    status : ProofStatus, default ProofStatus.ERROR
        Status of the proof generation run.
    """

    prover: str
    verification: VerificationReport | None
    output_dir: Path
    completed_files: dict[str, str] = field(default_factory=dict)
    cost_usd: float | None = None
    duration_s: float | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    error: str | None = None
    error_msg: str | None = None
    status: ProofStatus = ProofStatus.ERROR

    @property
    def wd(self) -> Path:
        """The completed working directory, ``output_dir/wd``.

        A complete lake project holding the completed ``.lean`` files -- the proof
        output.
        """
        return self.output_dir / "wd"

    @property
    def logs_dir(self) -> Path:
        """The run's logs directory, ``output_dir/logs``.

        Holds the captured agent stream (``stdout.txt``), ``stderr.txt``,
        ``result.json``, and any harness-specific rich record (Vibe's session log,
        ax-prover's per-target logs, Aristotle's events).
        """
        return self.output_dir / "logs"

    @property
    def success(self) -> bool:
        """Whether the run produced a verified proof.

        True iff :attr:`verification` exists and is
        :attr:`~open_atp.verify.VerificationReport.verified`.
        """
        return bool(self.verification and self.verification.verified)

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view: inline files, verification, cost, and artifact paths."""
        return {
            "prover": self.prover,
            "success": self.success,
            "status": self.status.value,
            "error": self.error,
            "error_msg": self.error_msg,
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
    timeout_s : int, default 1800
        Wall-clock budget for the generation run, in seconds.
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
    def auth_status(self) -> AuthStatus:
        """Report the credential this prover runs on, without attempting a run.

        Reads only the host (an env var, or the CLI credential file a ``login``
        wrote); an unauthenticated host yields a
        :attr:`~open_atp.auth.AuthState.MISSING` status rather than an exception.
        The credential is never checked against its provider, so a present,
        unexpired one can still be revoked.

        Returns
        -------
        ~open_atp.auth.AuthStatus
            Where the credential lives, whether it is there, and when it expires.
        """

    @abc.abstractmethod
    def _generate(
        self, task: ProofTask, wd: Path, logs_dir: Path, result: ProofResult
    ) -> None:
        """Generate the completed project in ``wd`` and record the run in ``result``.

        Implementations must leave ``wd`` containing the full completed project, write
        the run's logs into ``logs_dir``, fill ``result`` (``completed_files``,
        ``cost_usd``, ``metadata``), and run the shared check to set
        ``result.verification`` -- in their own live sandbox when they have one
        (``verify(..., session=session)``), else standalone
        (``self.verifier.verify(LeanProject(wd))``).
        """

    def prove(self, task: ProofTask, output_dir: Path | str) -> ProofResult:
        """Full lifecycle: reject-on-mismatch, generate, verify, write the result.

        The credential is checked up front: an expired one raises, and one with less
        than :data:`~open_atp.auth.EXPIRY_WARNING` left is logged as a warning -- a
        run outlives that window -- but does not stop the run.

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
            and :attr:`~ProofResult.logs_dir`.

        Raises
        ------
        ~open_atp.lean.ToolchainMismatch
            If the project's toolchain differs from the backend image's. Checked up
            front, before any run starts -- so this raises rather than returning an
            empty result.
        ~open_atp.lean.MathlibRevMismatch
            If the project records a Mathlib revision that differs from the backend
            image's. Checked up front, before any run starts.
        ~open_atp.harness.MissingCredentials
            If a credential the run needs is absent or already expired, or if the
            agent's provider rejected the one it was given. Either way no proof was
            attempted, so this raises rather than returning an empty result.
        ~open_atp.backends.base.ProvisionError
            If the compute sandbox fails to come up (daemon down, image missing,
            capacity). Raised before generation, so the run never started.
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
            self._check_credential()

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
                result.status = (
                    ProofStatus.VERIFIED if result.success else ProofStatus.UNVERIFIED
                )
            except (MissingCredentials, ProvisionError):
                # The run never started; no partial results to return.
                log.exception("prove could not start")
                raise
            except Exception as exc:
                # All other exceptions are from a started run with partial results
                # so we return a result with the error status instead of raising.
                log.exception("prove failed")
                result.status = _status_for_exception(exc)
                result.error = type(exc).__name__
                result.error_msg = str(exc)
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

    def _check_credential(self) -> None:
        """Reject an expired credential, and warn about one that expires mid-run.

        Nothing renews a credential mid-run: the sandbox holds a *copy*, so even a
        refreshable token refreshed in there is discarded with the container. An
        expired one therefore has no path to working, and is rejected before the
        sandbox comes up rather than 401-ing partway through a billed run.

        Anything still valid only warns -- the provider, not our reading of an
        expiry, is the authority on whether a credential works.

        Raises
        ------
        ~open_atp.harness.MissingCredentials
            If the credential's validity window has already passed.
        """
        try:
            status = self.auth_status()
        except Exception:
            # Reading the credential is best-effort here: whatever went wrong will
            # resurface as a real failure when the run resolves it for real, and a
            # pre-flight check must never be what breaks an otherwise-fine run.
            log.debug("could not read credential before run", exc_info=True)
            return

        state = status.state()
        if state not in (AuthState.EXPIRED, AuthState.EXPIRING):
            return
        remaining = status.time_remaining()
        detail = {
            "source": status.source,
            "expires_in_s": int(remaining.total_seconds()) if remaining else None,
            "refreshable": status.refreshable,
        }
        if state is AuthState.EXPIRING:
            log.warning(
                "credential expires soon; it will not survive this run", extra=detail
            )
            return

        log.error("credential expired", extra=detail)
        renew = (
            "run its CLI on this host to refresh it"
            if status.refreshable
            else "log in again"
        )
        raise MissingCredentials(
            f"the {self.name} prover's credential ({status.source}) expired; "
            f"a sandboxed run cannot refresh it -- {renew}"
        )
