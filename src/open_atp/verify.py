"""Verification: the verification report and the shared verifier.

Every prover -- agentic, Numina, *and* Aristotle -- funnels its output through the
:class:`Verifier`. The checks are lifted from milp_flare's ``entrypoint.sh`` +
``FLARE._evaluate``:

1. Compile each target file with ``lake env lean <file>``.
2. Scan the compile log for ``sorry`` warnings.
3. Extract the axiom dependency list via ``#print axioms`` and compare against
   :data:`STANDARD_AXIOMS`.

It also enforces the input contract: the project's toolchain (and locked Mathlib
revision) must match the backend image's pins, else we reject before spending
compute.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from open_atp.backends.base import ComputeBackend, ComputeSession
from open_atp.backends.docker import DockerBackend
from open_atp.backends.modal import ModalBackend
from open_atp.images import DEFAULT_IMAGE, Image
from open_atp.lean import LeanProject, MathlibRevMismatch, ToolchainMismatch

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


def docker_verifier(image: Image = DEFAULT_IMAGE) -> Verifier:
    """A :class:`Verifier` backed by a local Docker sandbox running ``image``."""
    return Verifier(DockerBackend(image=image))


def modal_verifier(image: Image = DEFAULT_IMAGE) -> Verifier:
    """A :class:`Verifier` backed by a Modal Sandbox running the published ``image``.

    Needs Modal credentials and the image published via ``open-atp
    build-modal-image``. The image's ``:tag`` is dropped for the Modal name lookup.
    """
    return Verifier(ModalBackend(image=image))


class Verifier:
    """Compiles projects in a :class:`ComputeBackend` and reports their status.

    Shared by every prover for the final compile/sorry/axiom check. Use
    :func:`docker_verifier` or :func:`modal_verifier` for the common cases.

    Parameters
    ----------
    backend : ComputeBackend
        The sandbox the candidate project is compiled in. Its image carries the
        Lean toolchain + Mathlib pins every project is checked against.

    Attributes
    ----------
    image : ~open_atp.images.Image
        The image ``backend`` runs -- the compatibility contract projects must
        match (toolchain + locked Mathlib revision).
    """

    def __init__(self, backend: ComputeBackend) -> None:
        self.backend = backend

    @property
    def image(self) -> Image:
        """The image the backend runs -- the compatibility contract projects match."""
        return self.backend.image

    def check_compatible(self, project: LeanProject) -> None:
        """Reject a project whose pins differ from the backend image's.

        Matches the project's :attr:`~open_atp.lean.LeanProject.lean_toolchain`
        against the image's :attr:`~open_atp.images.Image.lean_toolchain`, and its
        locked :attr:`~open_atp.lean.LeanProject.mathlib_rev` (when the project
        records one) against the image's
        :attr:`~open_atp.images.Image.mathlib_rev`. Raises
        :class:`~open_atp.lean.ToolchainMismatch` or
        :class:`~open_atp.lean.MathlibRevMismatch` on the first mismatch.

        Examples
        --------

        >>> import tempfile
        >>> from pathlib import Path
        >>> from open_atp.backends.docker import DockerBackend
        >>> from open_atp.images import Image
        >>> from open_atp.lean import LeanProject
        >>> from open_atp.verify import Verifier
        >>> root = Path(tempfile.mkdtemp())
        >>> _ = (root / "lakefile.toml").write_text('name = "demo"\\n')
        >>> _ = (root / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\\n")
        >>> project = LeanProject(root)

        A matching pin passes silently:

        >>> image = Image(lean_toolchain="leanprover/lean4:v4.31.0")
        >>> ok = Verifier(DockerBackend(image=image))
        >>> ok.check_compatible(project)

        A differing pin is rejected up front:

        >>> bad = Verifier(DockerBackend(image=Image()))
        >>> bad.check_compatible(project)  # doctest: +ELLIPSIS
        Traceback (most recent call last):
            ...
        open_atp.lean.ToolchainMismatch: Project pins ...
        """
        image = self.image
        if project.lean_toolchain != image.lean_toolchain:
            raise ToolchainMismatch(
                f"Project pins toolchain {project.lean_toolchain!r} but backend image "
                f"supports {image.lean_toolchain!r}. Re-submit against a matching "
                "image."
            )
        if project.mathlib_rev is not None and project.mathlib_rev != image.mathlib_rev:
            raise MathlibRevMismatch(
                f"Project pins Mathlib {project.mathlib_rev!r} but backend image "
                f"supports {image.mathlib_rev!r}. Re-submit against a matching image."
            )

    def verify(
        self, project: LeanProject, *, session: ComputeSession | None = None
    ) -> VerificationReport:
        """Compile ``project`` and return a :class:`VerificationReport`.

        Standalone (``session is None``) the compile spins up its own sandbox via
        ``backend.run`` -- the path Aristotle and the split-backend case take. When a
        caller passes a live ``session`` (the agent/verify backend-reuse path), the
        compile runs in that already-hot sandbox instead, avoiding a second spin-up.

        Examples
        --------

        A real project compiles in the backend; a project with no ``.lean`` files
        short-circuits to a trivial passing report without touching the sandbox:

        >>> import tempfile
        >>> from pathlib import Path
        >>> from open_atp.backends.docker import DockerBackend
        >>> from open_atp.images import Image
        >>> from open_atp.lean import LeanProject
        >>> from open_atp.verify import Verifier
        >>> root = Path(tempfile.mkdtemp())
        >>> _ = (root / "lakefile.toml").write_text('name = "demo"\\n')
        >>> _ = (root / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\\n")
        >>> image = Image(lean_toolchain="leanprover/lean4:v4.31.0")
        >>> verifier = Verifier(DockerBackend(image=image))
        >>> report = verifier.verify(LeanProject(root))
        >>> report.verified
        True
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
