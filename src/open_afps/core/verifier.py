"""Shared verifier: compile a candidate project in a sandbox and judge it.

Every prover -- agentic, Numina, *and* Aristotle -- funnels its output through
this. The checks are lifted from milp_flare's ``entrypoint.sh`` + ``FLARE._evaluate``:

1. Compile each target file with ``lake env lean <file>``.
2. Scan the compile log for ``sorry`` warnings.
3. Extract the axiom dependency list via ``#print axioms`` and compare against
   :data:`~open_afps.core.result.STANDARD_AXIOMS`.

It also enforces the input contract: the project's toolchain must match the
backend image's pin, else we reject before spending compute.
"""

from __future__ import annotations

import re

from open_afps.backends.base import ComputeBackend
from open_afps.core.result import VerificationReport
from open_afps.core.task import LeanProject, ToolchainMismatch

_SORRY_RE = re.compile(r"declaration uses 'sorry'|uses `sorry`")
_AXIOMS_RE = re.compile(r"depends on axioms: \[([^\]]*)\]")


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

    def verify(self, project: LeanProject) -> VerificationReport:
        """Compile ``project`` and return a :class:`VerificationReport`."""
        self.check_compatible(project)

        targets = project.lean_files()
        rel = [t.relative_to(project.root).as_posix() for t in targets]
        # Compile every file; capture a combined log. (A later optimisation can build
        # the whole project once via `lake build` instead of file-by-file.)
        cmd = " && ".join(f"lake env lean {f}" for f in rel) if rel else "true"
        result = self.backend.run(project.root, cmd)

        log = result.stdout + "\n" + result.stderr
        compiles = result.exit_code == 0
        sorry_free = not _SORRY_RE.search(log)
        axioms = self._parse_axioms(log)

        # Per-file pass/fail is coarse in phase 1 (overall compile gate); refine when
        # the backend reports per-command exit codes.
        per_file = {f: compiles for f in rel}

        return VerificationReport(
            compiles=compiles,
            sorry_free=sorry_free,
            axioms=axioms,
            compile_log=log,
            per_file=per_file,
        )

    @staticmethod
    def _parse_axioms(log: str) -> tuple[str, ...]:
        found: list[str] = []
        for m in _AXIOMS_RE.finditer(log):
            found.extend(a.strip() for a in m.group(1).split(",") if a.strip())
        return tuple(dict.fromkeys(found))  # dedupe, preserve order
