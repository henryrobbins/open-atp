"""Verification: the verification report and the shared verifier.

Every prover -- agentic, Numina, *and* Aristotle -- funnels its output through the
:class:`Verifier`, which runs three checks:

1. Compile each target file with ``lake env lean <file>``.
2. Scan the compile log for ``sorry`` warnings.
3. Extract the axiom dependency list via ``#print axioms`` and compare against
   :data:`STANDARD_AXIOMS`.

It also enforces the input contract: the project's toolchain (and locked Mathlib
revision) must match the backend image's pins, else we reject before spending
compute.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from open_atp.backends.base import CommandTimeout, ComputeBackend, ComputeSession
from open_atp.backends.docker import DockerBackend
from open_atp.backends.modal import ModalBackend
from open_atp.images import DEFAULT_IMAGE, Image
from open_atp.lean import LeanProject, MathlibRevMismatch, ToolchainMismatch

# Axioms that a "clean" Mathlib proof is allowed to depend on. Anything else
# (notably ``sorryAx``) means the proof is not actually complete.
STANDARD_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})

log = logging.getLogger("open_atp")

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
        """The depended-on axioms outside :data:`STANDARD_AXIOMS`."""
        return tuple(a for a in self.axioms if a not in STANDARD_AXIOMS)

    @property
    def verified(self) -> bool:
        """True iff the project compiles, has no sorry, and no foreign axioms."""
        return self.compiles and self.sorry_free and not self.non_standard_axioms

    def to_dict(self) -> dict[str, object]:
        """JSON-ready summary (the full ``compile_log`` is intentionally omitted).

        Returns
        -------
        dict[str, object]
            The verdict (``verified``/``compiles``/``sorry_free``), the axiom lists,
            and ``per_file``; ``compile_log`` is excluded.
        """
        return {
            "verified": self.verified,
            "compiles": self.compiles,
            "sorry_free": self.sorry_free,
            "axioms": list(self.axioms),
            "non_standard_axioms": list(self.non_standard_axioms),
            "per_file": dict(self.per_file),
        }


def docker_verifier(image: Image = DEFAULT_IMAGE) -> Verifier:
    """A :class:`Verifier` backed by a local Docker sandbox running ``image``.

    Parameters
    ----------
    image : ~open_atp.images.Image, optional
        The image whose Lean toolchain + Mathlib pins projects are checked against.
        Default :data:`~open_atp.images.DEFAULT_IMAGE`.

    Returns
    -------
    Verifier
        A verifier over a :class:`~open_atp.backends.docker.DockerBackend`.
    """
    return Verifier(DockerBackend(image=image))


def modal_verifier(image: Image = DEFAULT_IMAGE) -> Verifier:
    """A :class:`Verifier` backed by a Modal Sandbox running the published ``image``.

    Needs Modal credentials and the image published via ``open-atp
    build-modal-image``. The image's ``:tag`` is dropped for the Modal name lookup.

    Parameters
    ----------
    image : ~open_atp.images.Image, optional
        The published image whose Lean toolchain + Mathlib pins projects are checked
        against. Default :data:`~open_atp.images.DEFAULT_IMAGE`.

    Returns
    -------
    Verifier
        A verifier over a :class:`~open_atp.backends.modal.ModalBackend`.
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
    timeout_s : int
        Wall-clock cap for the post-generation compile/axiom check, in seconds.
        Independent of a prover's (larger) generation budget. Default ``600``.

    Attributes
    ----------
    image : ~open_atp.images.Image
        The image ``backend`` runs -- the compatibility contract projects must
        match (toolchain + locked Mathlib revision).
    """

    def __init__(self, backend: ComputeBackend, *, timeout_s: int = 600) -> None:
        self.backend = backend
        self.timeout_s = timeout_s

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

        Parameters
        ----------
        project : ~open_atp.lean.LeanProject
            The candidate project whose toolchain (and locked Mathlib revision, when
            recorded) must match the backend image's pins.

        Raises
        ------
        ~open_atp.lean.ToolchainMismatch
            If the project's toolchain differs from the image's.
        ~open_atp.lean.MathlibRevMismatch
            If the project records a Mathlib revision that differs from the image's.

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
    ) -> VerificationReport | None:
        """Compile ``project`` and return a :class:`VerificationReport`.

        With no ``session`` the compile spins up its own sandbox via ``backend.run``.
        Passing a live ``session`` runs the compile in that already-hot sandbox
        instead, avoiding a second spin-up.

        Parameters
        ----------
        project : ~open_atp.lean.LeanProject
            The candidate project to compile. Checked for compatibility first.
        session : ~open_atp.backends.base.ComputeSession, optional
            A live, already-hot sandbox to compile in. When ``None`` (the default),
            ``backend.run`` spins up a fresh sandbox for this compile.

        Returns
        -------
        VerificationReport or None
            The compile/``sorry``/axiom verdict, or ``None`` when the compile is killed
            for exceeding the verifier's ``timeout_s`` -- a timed-out compile yields no
            verdict at all, not a failing one. A project with no ``.lean`` files
            short-circuits to a trivial passing report without touching the sandbox.

        Raises
        ------
        ~open_atp.lean.ToolchainMismatch
            If the project's pins differ from the backend image's (via
            :meth:`check_compatible`).
        ~open_atp.lean.MathlibRevMismatch
            If the project's locked Mathlib revision differs from the image's.

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
            log.warning(
                "no lean files; trivial pass", extra={"project": str(project.root)}
            )
            return VerificationReport(compiles=True, sorry_free=True)

        log.debug(
            "verifying",
            extra={"files": len(rel), "backend": self.backend.name},
        )

        # The compile/axiom check is the post-generation step: cap it with the
        # verifier's own timeout, not the prover's (larger) generation budget.
        script = self._compile_script(rel)
        try:
            if session is None:
                result = self.backend.run(
                    project.root, script, timeout_s=self.timeout_s
                )
            else:
                result = session.exec(script, timeout_s=self.timeout_s).wait()
        except CommandTimeout:
            log.warning(
                "verification compile exceeded its budget; no verdict",
                extra={"timeout_s": self.timeout_s, "backend": self.backend.name},
            )
            return None
        compile_log = result.stdout + ("\n" + result.stderr if result.stderr else "")

        per_file = self._parse_per_file(compile_log, rel)
        compiles = result.exit_code == 0 and all(per_file.values())
        report = VerificationReport(
            compiles=compiles,
            sorry_free=not _SORRY_RE.search(compile_log),
            axioms=self._parse_axioms(compile_log),
            compile_log=compile_log,
            per_file=per_file,
        )

        log.debug(
            "verified" if report.verified else "verification failed",
            extra={
                "verified": report.verified,
                "compiles": report.compiles,
                "sorry_free": report.sorry_free,
                "axioms": list(report.axioms),
                "duration_s": round(result.duration_s, 1),
            },
        )
        # A proof that compiles but leans on a foreign axiom (notably sorryAx) is not
        # actually complete -- easy to miss in the verdict, so call it out.
        if report.non_standard_axioms:
            log.warning(
                "proof depends on non-standard axioms",
                extra={"axioms": list(report.non_standard_axioms)},
            )
        return report

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
