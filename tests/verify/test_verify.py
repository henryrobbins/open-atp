"""Phase-1 end-to-end test: verify a trivial Mathematics-in-Lean project in Docker.

Requires the ``open-atp:latest`` image (``docker build -t open-atp:latest images/``).
Marked ``docker`` so it can be skipped with ``-m 'not docker'``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from open_atp.backends.docker import DockerBackend, DockerConfig
from open_atp.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_atp.lean import LeanProject, ToolchainMismatch
from open_atp.verify import Verifier, docker_verifier

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"

SOLVED_PROOF = """\
theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by
  rw [mul_comm a b, mul_assoc b a c]
"""


def _stage(tmp_path: Path) -> Path:
    """Copy the fixture into a temp dir so .lake symlinks don't touch the fixture."""
    dst = tmp_path / "proj"
    shutil.copytree(FIXTURE, dst)
    return dst


pytestmark = pytest.mark.docker


def test_sorry_theorem_compiles_but_is_not_verified(tmp_path: Path) -> None:
    project = LeanProject(_stage(tmp_path))
    report = docker_verifier().verify(project)

    # A `sorry` compiles (with a warning), so the file builds...
    assert report.compiles, report.compile_log
    # ...but it is not sorry-free, hence not verified.
    assert not report.sorry_free
    assert not report.verified


def test_completed_theorem_is_verified(tmp_path: Path) -> None:
    proj = _stage(tmp_path)
    (proj / "MILExample.lean").write_text("import Mathlib\n\n" + SOLVED_PROOF)

    report = docker_verifier().verify(LeanProject(proj))

    assert report.compiles, report.compile_log
    assert report.sorry_free
    assert report.verified


def test_toolchain_mismatch_is_rejected(tmp_path: Path) -> None:
    proj = _stage(tmp_path)
    (proj / "lean-toolchain").write_text("leanprover/lean4:v4.99.0\n")

    with pytest.raises(ToolchainMismatch):
        docker_verifier().verify(LeanProject(proj))


# --- session primitive ------------------------------------------------------


def test_session_persists_state_across_execs(tmp_path: Path) -> None:
    """One live container: a file written by one exec is visible to the next."""
    proj = _stage(tmp_path)
    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    with backend.session(proj) as session:
        session.exec("echo hello > marker.txt").wait()
        result = session.exec("cat marker.txt").wait()

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "hello" in result.stdout
    # Bind mount: the write also landed on the host workdir.
    assert (proj / "marker.txt").read_text().strip() == "hello"


def test_verify_in_session_matches_standalone(tmp_path: Path) -> None:
    """A solved project verifies the same whether checked in a session or standalone."""
    proj = _stage(tmp_path)
    (proj / "MILExample.lean").write_text("import Mathlib\n\n" + SOLVED_PROOF)

    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    verifier = Verifier(backend, supported_toolchain=DEFAULT_TOOLCHAIN)
    with backend.session(proj) as session:
        report = verifier.verify(LeanProject(proj), session=session)

    assert report.compiles, report.compile_log
    assert report.sorry_free
    assert report.verified
