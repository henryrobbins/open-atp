"""The Lean input contract: the project to complete, the task, and staging helpers.

The input contract is a *full lake project*: it must carry its own
``lean-toolchain`` and ``lake-manifest.json``. We reject up front when the
project's pinned toolchain (or Mathlib revision) does not match the sandbox image
we are about to run it in, rather than failing deep inside a build.

A full lake project on disk is already a :class:`LeanProject` (just
``LeanProject(Path(path))``); the only nontrivial case is staging one or more bare
``.lean`` files into the pinned Mathlib skeleton, which :func:`create_project` does.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from open_atp.images import SKELETON_DIR


class ToolchainMismatch(ValueError):
    """Raised when an uploaded project's toolchain does not match the image's pin."""


class MathlibRevMismatch(ValueError):
    """Raised when an uploaded project's Mathlib revision differs from the image's."""


@dataclass(frozen=True)
class LeanProject:
    """A complete lake project on disk.

    Attributes
    ----------
    root : Path
        Directory containing ``lakefile.toml`` (or ``lakefile.lean``),
        ``lean-toolchain``, and ``lake-manifest.json``. Resolved to an absolute path.
    lean_toolchain : str
        The pinned toolchain read from ``lean-toolchain``, e.g.
        ``leanprover/lean4:v4.31.0``.
    mathlib_rev : str | None
        The pinned Mathlib revision from ``lake-manifest.json``, if present.

    Examples
    --------

    Point :class:`LeanProject` at a lake project directory and inspect it (here a
    minimal project is written to a temp dir to keep the example self-contained):

    >>> import tempfile
    >>> from pathlib import Path
    >>> from open_atp.lean import LeanProject
    >>> root = Path(tempfile.mkdtemp())
    >>> _ = (root / "lakefile.toml").write_text('name = "demo"\\n')
    >>> _ = (root / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\\n")
    >>> _ = (root / "Demo.lean").write_text("theorem t : True := by sorry\\n")
    >>> project = LeanProject(root)
    >>> project.lean_toolchain
    'leanprover/lean4:v4.31.0'
    >>> [p.name for p in project.lean_files()]
    ['Demo.lean']
    >>> [p.name for p in project.files_with_sorry()]
    ['Demo.lean']
    """

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())
        if not self._lakefile():
            raise FileNotFoundError(f"No lakefile.toml/lakefile.lean under {self.root}")
        if not (self.root / "lean-toolchain").is_file():
            raise FileNotFoundError(f"No lean-toolchain under {self.root}")

    def _lakefile(self) -> Path | None:
        for name in ("lakefile.toml", "lakefile.lean"):
            p = self.root / name
            if p.is_file():
                return p
        return None

    @property
    def lean_toolchain(self) -> str:
        """The pinned toolchain, e.g. ``leanprover/lean4:v4.31.0``."""
        return (self.root / "lean-toolchain").read_text().strip()

    @property
    def mathlib_rev(self) -> str | None:
        """The pinned Mathlib revision from ``lake-manifest.json``, if present.

        Prefers the human-declared ``inputRev`` (e.g. ``v4.28.0``) so it is
        comparable to an :class:`~open_atp.images.Image`'s declared
        :attr:`~open_atp.images.Image.mathlib_rev`, falling back to the resolved git
        ``rev`` (a commit SHA) when no ``inputRev`` is recorded.
        """
        manifest = self.root / "lake-manifest.json"
        if not manifest.is_file():
            return None
        data = json.loads(manifest.read_text())
        for pkg in data.get("packages", []):
            if pkg.get("name") == "mathlib":
                rev = pkg.get("inputRev") or pkg.get("rev")
                return rev if isinstance(rev, str) else None
        return None

    def lean_files(self) -> list[Path]:
        """All ``.lean`` source files, excluding build artifacts under ``.lake``."""
        return [p for p in self.root.rglob("*.lean") if ".lake" not in p.parts]

    def files_with_sorry(self) -> list[Path]:
        """Lean files that contain a ``sorry`` token (cheap textual scan)."""
        pat = re.compile(r"\bsorry\b")
        return [p for p in self.lean_files() if pat.search(p.read_text())]


@dataclass(frozen=True)
class ProofTask:
    """A unit of work: complete the sorrys in ``project``.

    Attributes
    ----------
    project : LeanProject
        The lake project to complete.
    name : str | None
        Optional task identifier, used to attribute log records to this task. Set by
        :func:`~open_atp.benchmark.tasks_from_dir`; ``None`` for a one-off task.
    targets : tuple[Path, ...]
        Optional explicit list of files (relative to ``project.root``) to focus on.
        When empty, every file containing ``sorry`` is fair game.
    user_prompt : str | None
        Optional per-task guidance appended below the prover's own prompt under an
        ``# Additional instructions`` heading (see
        :func:`~open_atp.provers.base.compose_prompt`). ``None`` (the common case)
        leaves the prover prompt untouched.
    metadata : dict[str, str]
        Optional free-form metadata carried alongside the task.
    """

    project: LeanProject
    name: str | None = None
    targets: tuple[Path, ...] = ()
    user_prompt: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def resolved_targets(self) -> list[Path]:
        """Absolute paths of the files to work on.

        The explicit :attr:`targets` (resolved against ``project.root``) when given,
        else every file containing ``sorry``
        (:meth:`~open_atp.lean.LeanProject.files_with_sorry`).
        """
        if self.targets:
            return [self.project.root / t for t in self.targets]
        return self.project.files_with_sorry()


def create_project(
    files: Sequence[Path | str],
    dest: Path | str,
    *,
    skeleton: Path = SKELETON_DIR,
) -> LeanProject:
    """Stage bare ``.lean`` files into the default Mathlib skeleton -> a project.

    Convenience for the *"upload one or more ``.lean`` files"* contract: copies the
    skeleton's ``lakefile.toml`` + ``lean-toolchain`` into ``dest`` and drops the
    files at its root. Limitation: this only works for the pinned toolchain/deps the
    skeleton (and the baked image) provide -- a file needing a different Mathlib
    revision or extra deps must arrive as a full lake project instead.

    Parameters
    ----------
    files : Sequence[Path | str]
        Bare ``.lean`` files to stage. Each is copied to the root of ``dest``; a
        non-``.lean`` path raises ``ValueError``.
    dest : Path | str
        Destination directory for the new project. Created if missing.
    skeleton : Path
        Project skeleton to copy the ``lakefile.toml``/``lean-toolchain`` (and, when
        present, ``lakefile.lean``/``lake-manifest.json``) from. Default
        :data:`~open_atp.images.SKELETON_DIR` -- the baked image's skeleton, only
        present in a source checkout.

    Returns
    -------
    LeanProject
        A complete project rooted at ``dest``, ready to verify.

    Examples
    --------

    Stage a bare ``.lean`` file into a skeleton to get a complete
    :class:`LeanProject` (here a minimal skeleton is written to a temp dir; in
    practice ``skeleton`` defaults to the baked image's ``SKELETON_DIR``):

    >>> import tempfile
    >>> from pathlib import Path
    >>> from open_atp.lean import create_project
    >>> skeleton = Path(tempfile.mkdtemp())
    >>> _ = (skeleton / "lakefile.toml").write_text('name = "demo"\\n')
    >>> _ = (skeleton / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\\n")
    >>> bare = Path(tempfile.mkdtemp()) / "Bare.lean"
    >>> _ = bare.write_text("theorem t : True := by sorry\\n")
    >>> dest = Path(tempfile.mkdtemp()) / "project"
    >>> project = create_project([bare], dest, skeleton=skeleton)
    >>> project.lean_toolchain
    'leanprover/lean4:v4.31.0'
    >>> [p.name for p in project.lean_files()]
    ['Bare.lean']
    """
    if not (skeleton / "lean-toolchain").is_file():
        raise FileNotFoundError(
            f"No skeleton project at {skeleton} (only available in a source "
            "checkout); submit a full lake project directory instead."
        )
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    skeleton_files = (
        "lakefile.toml",
        "lakefile.lean",
        "lean-toolchain",
        "lake-manifest.json",
    )
    for name in skeleton_files:
        src = skeleton / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    for f in files:
        f = Path(f)
        if f.suffix != ".lean":
            raise ValueError(f"Expected a .lean file, got {f}")
        shutil.copy2(f, dest / f.name)
    return LeanProject(dest)
