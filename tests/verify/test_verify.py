"""Phase-1 end-to-end test: verify a trivial Mathematics-in-Lean project in Docker.

Requires the ``open-atp:latest`` image (``docker build -t open-atp:latest images/``).
Marked ``docker`` so it can be skipped with ``-m 'not docker'``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from open_atp.backends.docker import DockerBackend
from open_atp.images import DEFAULT_IMAGE
from open_atp.lean import LeanProject
from open_atp.verify import STANDARD_AXIOMS, Verifier, docker_verifier

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
    # The axiom check ran and found only what a Mathlib proof may rest on.
    assert set(report.axioms) == STANDARD_AXIOMS
    assert not report.non_standard_axioms


def test_foreign_axiom_is_not_verified(tmp_path: Path) -> None:
    """A proof resting on a declared axiom compiles cleanly but must not verify."""
    proj = _stage(tmp_path)
    (proj / "MILExample.lean").write_text(
        "import Mathlib\n\n"
        "axiom cheat (a b : ℝ) : a * b = b * a\n\n"
        "theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by\n"
        "  rw [cheat a b, mul_assoc b a c]\n"
    )

    report = docker_verifier().verify(LeanProject(proj))

    # Nothing else catches this: it compiles and there is no `sorry` anywhere.
    assert report.compiles, report.compile_log
    assert report.sorry_free
    assert report.non_standard_axioms == ("cheat",)
    assert not report.verified


def test_file_without_trailing_newline_is_verified(tmp_path: Path) -> None:
    """The axiom probe is appended, so the candidate's last line must stay its own.

    The file ends on an identifier (``end MILExample``) with no newline: glued to the
    probe's leading ``open``, that would tokenize as one name and fail to parse.
    """
    proj = _stage(tmp_path)
    (proj / "MILExample.lean").write_text(
        "import Mathlib\n\nnamespace MILExample\n\n" + SOLVED_PROOF + "\nend MILExample"
    )

    report = docker_verifier().verify(LeanProject(proj))

    assert report.compiles, report.compile_log
    assert report.verified
    assert set(report.axioms) == STANDARD_AXIOMS


# --- session primitive ------------------------------------------------------


def test_session_persists_state_across_execs(tmp_path: Path) -> None:
    """One live container: a file written by one exec is visible to the next."""
    proj = _stage(tmp_path)
    backend = DockerBackend(image=DEFAULT_IMAGE)
    with backend.session(proj, timeout_s=600) as session:
        session.exec("echo hello > marker.txt", timeout_s=60).wait()
        result = session.exec("cat marker.txt", timeout_s=60).wait()

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "hello" in result.stdout
    # Bind mount: the write also landed on the host workdir.
    assert (proj / "marker.txt").read_text().strip() == "hello"


def test_verify_in_session_matches_standalone(tmp_path: Path) -> None:
    """A solved project verifies the same whether checked in a session or standalone."""
    proj = _stage(tmp_path)
    (proj / "MILExample.lean").write_text("import Mathlib\n\n" + SOLVED_PROOF)

    backend = DockerBackend(image=DEFAULT_IMAGE)
    verifier = Verifier(backend)
    with backend.session(proj, timeout_s=600) as session:
        report = verifier.verify(LeanProject(proj), session=session)

    assert report.compiles, report.compile_log
    assert report.sorry_free
    assert report.verified
