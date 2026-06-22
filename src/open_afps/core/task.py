"""Input types: the Lean project to complete and the task describing what to fill.

The input contract is a *full lake project*: it must carry its own
``lean-toolchain`` and ``lake-manifest.json``. We reject up front when the
project's pinned toolchain (and, eventually, Mathlib revision) does not match the
sandbox image we are about to run it in, rather than failing deep inside a build.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


class ToolchainMismatch(ValueError):
    """Raised when an uploaded project's toolchain does not match the image's pin."""


@dataclass(frozen=True)
class LeanProject:
    """A complete lake project on disk.

    Parameters
    ----------
    root:
        Directory containing ``lakefile.toml`` (or ``lakefile.lean``),
        ``lean-toolchain``, and ``lake-manifest.json``.
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
    def toolchain(self) -> str:
        """The pinned toolchain, e.g. ``leanprover/lean4:v4.31.0``."""
        return (self.root / "lean-toolchain").read_text().strip()

    @property
    def mathlib_rev(self) -> str | None:
        """The locked Mathlib git revision from ``lake-manifest.json``, if present."""
        manifest = self.root / "lake-manifest.json"
        if not manifest.is_file():
            return None
        data = json.loads(manifest.read_text())
        for pkg in data.get("packages", []):
            if pkg.get("name") == "mathlib":
                rev = pkg.get("rev")
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

    Parameters
    ----------
    project:
        The lake project to complete.
    targets:
        Optional explicit list of files (relative to ``project.root``) to focus on.
        When empty, every file containing ``sorry`` is fair game.
    instructions:
        Optional natural-language guidance forwarded to provers that accept a prompt
        (e.g. Aristotle, or the agent system prompt).
    """

    project: LeanProject
    targets: tuple[Path, ...] = ()
    instructions: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def resolved_targets(self) -> list[Path]:
        if self.targets:
            return [self.project.root / t for t in self.targets]
        return self.project.files_with_sorry()
