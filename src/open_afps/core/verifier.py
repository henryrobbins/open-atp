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
from open_afps.backends.docker import DockerBackend, DockerConfig
from open_afps.backends.modal import ModalBackend, ModalConfig
from open_afps.core.result import VerificationReport
from open_afps.core.task import LeanProject, ToolchainMismatch
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN

_SORRY_RE = re.compile(r"declaration uses 'sorry'|uses `sorry`")
_AXIOMS_RE = re.compile(r"depends on axioms: \[([^\]]*)\]")


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

    Needs Modal credentials and the image published via ``open-afps
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

    def verify(self, project: LeanProject) -> VerificationReport:
        """Compile ``project`` and return a :class:`VerificationReport`."""
        self.check_compatible(project)

        rel = [t.relative_to(project.root).as_posix() for t in project.lean_files()]
        if not rel:
            return VerificationReport(compiles=True, sorry_free=True)

        result = self.backend.run(project.root, self._compile_script(rel))
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
