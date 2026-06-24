"""Verification: the output types and the shared verifier.

Every prover -- agentic, Numina, *and* Aristotle -- funnels its output through the
:class:`Verifier`. The checks are lifted from milp_flare's ``entrypoint.sh`` +
``FLARE._evaluate``:

1. Compile each target file with ``lake env lean <file>``.
2. Scan the compile log for ``sorry`` warnings.
3. Extract the axiom dependency list via ``#print axioms`` and compare against
   :data:`STANDARD_AXIOMS`.

It also enforces the input contract: the project's toolchain must match the
backend image's pin, else we reject before spending compute.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from open_atp.backends.base import ComputeBackend, ComputeSession
from open_atp.backends.docker import DockerBackend, DockerConfig
from open_atp.backends.modal import ModalBackend, ModalConfig
from open_atp.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_atp.lean import LeanProject, ToolchainMismatch

# Axioms that a "clean" Mathlib proof is allowed to depend on. Anything else
# (notably ``sorryAx``) means the proof is not actually complete.
STANDARD_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})

_SORRY_RE = re.compile(r"declaration uses 'sorry'|uses `sorry`")
_AXIOMS_RE = re.compile(r"depends on axioms: \[([^\]]*)\]")


@dataclass(frozen=True)
class VerificationReport:
    """Result of compiling a candidate project in a sandbox.

    Produced by :class:`Verifier` and shared by every prover, including Aristotle.

    Attributes
    ----------
    compiles : bool
        Whether the whole project built successfully.
    sorry_free : bool
        Whether the build is free of ``sorry`` (no incomplete proofs remain).
    axioms : tuple[str, ...]
        Every axiom the compiled project depends on, as reported by Lean.
    compile_log : str
        The full build log. Omitted from :meth:`to_dict`.
    per_file : dict[str, bool]
        Per-file compile status, keyed by file path relative to the project root.
    non_standard_axioms : tuple[str, ...]
        The axioms outside :data:`STANDARD_AXIOMS` -- notably ``sorryAx``, which
        means the proof is not actually complete.
    verified : bool
        True iff the project compiles, has no ``sorry``, and uses no axioms
        outside :data:`STANDARD_AXIOMS`.
    """

    compiles: bool
    sorry_free: bool
    axioms: tuple[str, ...] = ()
    compile_log: str = ""
    per_file: dict[str, bool] = field(default_factory=dict)

    @property
    def non_standard_axioms(self) -> tuple[str, ...]:
        return tuple(a for a in self.axioms if a not in STANDARD_AXIOMS)

    @property
    def verified(self) -> bool:
        """True iff the project compiles, has no sorry, and no foreign axioms."""
        return self.compiles and self.sorry_free and not self.non_standard_axioms

    def to_dict(self) -> dict[str, object]:
        """JSON-ready summary (the full ``compile_log`` is intentionally omitted)."""
        return {
            "verified": self.verified,
            "compiles": self.compiles,
            "sorry_free": self.sorry_free,
            "axioms": list(self.axioms),
            "non_standard_axioms": list(self.non_standard_axioms),
            "per_file": dict(self.per_file),
        }


@dataclass
class ProofResult:
    """What a prover returns from :meth:`~open_atp.provers.base.AutomatedProver.prove`.

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
        Set when the prover raised before producing a result (Docker down, API error,
        toolchain mismatch). :attr:`verification` is ``None`` and :attr:`success` is
        ``False``.
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
        :attr:`~VerificationReport.verified`.
    """

    prover: str
    verification: VerificationReport | None
    output_dir: Path
    completed_files: dict[str, str] = field(default_factory=dict)
    cost_usd: float | None = None
    duration_s: float | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    error: str | None = None

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


def docker_verifier(
    image: str = DEFAULT_IMAGE, toolchain: str = DEFAULT_TOOLCHAIN
) -> Verifier:
    """A :class:`Verifier` backed by a local Docker sandbox running ``image``."""
    backend = DockerBackend(DockerConfig(image=image))
    return Verifier(backend, supported_toolchain=toolchain)


def modal_verifier(
    image: str = DEFAULT_IMAGE, toolchain: str = DEFAULT_TOOLCHAIN
) -> Verifier:
    """A :class:`Verifier` backed by a Modal Sandbox running the published ``image``.

    Needs Modal credentials and the image published via ``open-atp
    build-modal-image``. The image's ``:tag`` is dropped for the Modal name lookup.
    """
    backend = ModalBackend(ModalConfig(image=image))
    return Verifier(backend, supported_toolchain=toolchain)


class Verifier:
    """Compiles projects in a :class:`ComputeBackend` and reports their status."""

    def __init__(
        self, backend: ComputeBackend, *, supported_toolchain: str | None = None
    ) -> None:
        self.backend = backend
        # The toolchain baked into ``backend.config.image``. When set, projects whose
        # pin differs are rejected up front (the "reject if the image isn't correct"
        # contract).
        self.supported_toolchain = supported_toolchain

    def check_compatible(self, project: LeanProject) -> None:
        if self.supported_toolchain and project.toolchain != self.supported_toolchain:
            raise ToolchainMismatch(
                f"Project pins {project.toolchain!r} but backend image supports "
                f"{self.supported_toolchain!r}. Re-submit against a matching image."
            )

    def verify(
        self, project: LeanProject, *, session: ComputeSession | None = None
    ) -> VerificationReport:
        """Compile ``project`` and return a :class:`VerificationReport`.

        Standalone (``session is None``) the compile spins up its own sandbox via
        ``backend.run`` -- the path Aristotle and the split-backend case take. When a
        caller passes a live ``session`` (the agent/verify backend-reuse path), the
        compile runs in that already-hot sandbox instead, avoiding a second spin-up.
        """
        self.check_compatible(project)

        rel = [t.relative_to(project.root).as_posix() for t in project.lean_files()]
        if not rel:
            return VerificationReport(compiles=True, sorry_free=True)

        script = self._compile_script(rel)
        if session is None:
            result = self.backend.run(project.root, script)
        else:
            with session.exec(script) as handle:
                result = handle.wait()
        log = result.stdout + ("\n" + result.stderr if result.stderr else "")

        per_file = self._parse_per_file(log, rel)
        compiles = result.exit_code == 0 and all(per_file.values())
        return VerificationReport(
            compiles=compiles,
            sorry_free=not _SORRY_RE.search(log),
            axioms=self._parse_axioms(log),
            compile_log=log,
            per_file=per_file,
        )

    @staticmethod
    def _compile_script(rel: list[str]) -> str:
        """Compile each file, bracketing it with markers so we can read exit codes.

        Mirrors milp_flare's ``entrypoint.sh``: every file runs (``;`` not ``&&``) so
        one failure doesn't mask the rest, and the overall status is the OR of the
        per-file exit codes.
        """
        lines = ["fail=0"]
        for f in rel:
            lines += [
                f'echo "=== FILE {f} ==="',
                f'lake env lean "{f}" 2>&1',
                "rc=$?",
                'echo "=== EXIT $rc ==="',
                '[ "$rc" -ne 0 ] && fail=1',
            ]
        lines.append("exit $fail")
        return "\n".join(lines)

    @staticmethod
    def _parse_per_file(log: str, rel: list[str]) -> dict[str, bool]:
        # Walk the FILE/EXIT markers in order; a file passes iff its exit code is 0.
        per_file: dict[str, bool] = {}
        current: str | None = None
        for line in log.splitlines():
            if line.startswith("=== FILE "):
                current = line[len("=== FILE ") : -len(" ===")]
            elif line.startswith("=== EXIT ") and current is not None:
                per_file[current] = line[len("=== EXIT ") : -len(" ===")].strip() == "0"
                current = None
        # Files with no recorded marker (shouldn't happen) default to failing.
        for f in rel:
            per_file.setdefault(f, False)
        return per_file

    @staticmethod
    def _parse_axioms(log: str) -> tuple[str, ...]:
        found: list[str] = []
        for m in _AXIOMS_RE.finditer(log):
            found.extend(a.strip() for a in m.group(1).split(",") if a.strip())
        return tuple(dict.fromkeys(found))  # dedupe, preserve order
