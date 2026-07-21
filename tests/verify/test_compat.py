"""The input contract is enforced before any compute is spent.

No Docker: ``verify`` checks the project's pins against the image's first, so a
mismatched project is rejected without a container ever starting.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from open_atp.backends.docker import DockerBackend
from open_atp.lean import LeanProject, MathlibRevMismatch, ToolchainMismatch
from open_atp.verify import Verifier

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"


def _stage(tmp_path: Path) -> Path:
    dst = tmp_path / "proj"
    shutil.copytree(FIXTURE, dst)
    return dst


def _verifier() -> Verifier:
    return Verifier(DockerBackend())


def test_matching_pins_are_accepted(tmp_path: Path) -> None:
    _verifier().check_compatible(LeanProject(_stage(tmp_path)))


def test_toolchain_mismatch_is_rejected(tmp_path: Path) -> None:
    proj = _stage(tmp_path)
    (proj / "lean-toolchain").write_text("leanprover/lean4:v4.99.0\n")

    with pytest.raises(ToolchainMismatch, match="v4.99.0"):
        _verifier().verify(LeanProject(proj))


def test_mathlib_rev_mismatch_is_rejected(tmp_path: Path) -> None:
    """A project on the image's Lean but a different Mathlib is still rejected."""
    proj = _stage(tmp_path)
    manifest = json.loads((proj / "lake-manifest.json").read_text())
    for pkg in manifest["packages"]:
        if pkg["name"] == "mathlib":
            pkg["inputRev"] = "v4.99.0"
    (proj / "lake-manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(MathlibRevMismatch, match="v4.99.0"):
        _verifier().verify(LeanProject(proj))
