"""ModalBackend tests: a live-Sandbox parity suite plus an offline tar round-trip.

The live tests are marked ``modal`` and skip unless Modal credentials are present
(``MODAL_TOKEN_ID`` / ``MODAL_TOKEN_SECRET`` in the env -- read from ``.env`` by
``conftest`` -- or a ``~/.modal.toml`` profile from ``modal token set``) and require
the image published via ``open-atp build-modal-image``. They mirror
``core/test_verifier.py`` so the shared ``Verifier`` is exercised on Modal exactly
as on Docker. The tar round-trip test needs no Sandbox and always runs.
"""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from pathlib import Path

import pytest

from open_atp.backends.modal import _modal_image_name, _tar_dir
from open_atp.lean import LeanProject, ToolchainMismatch

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"

SOLVED_PROOF = """\
theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by
  rw [mul_comm a b, mul_assoc b a c]
"""


def _modal_configured() -> bool:
    """Whether Modal has credentials: env tokens or a ``~/.modal.toml`` profile.

    Modal auth lands in either place (``modal token set`` writes the toml), so gating
    only on the env vars wrongly skips a CLI-authenticated machine.
    """
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return True
    return (Path.home() / ".modal.toml").is_file()


_HAVE_MODAL_CREDS = _modal_configured()


def _stage(tmp_path: Path) -> Path:
    """Copy the fixture into a temp dir so .lake symlinks don't touch the fixture."""
    dst = tmp_path / "proj"
    shutil.copytree(FIXTURE, dst)
    return dst


# --- offline unit test: tar push/pull round-trips without a live Sandbox ----


def test_tar_dir_round_trips(tmp_path: Path) -> None:
    """``_tar_dir`` blob extracts back to identical contents (the push/pull core)."""
    src = tmp_path / "wd"
    (src / "sub").mkdir(parents=True)
    (src / "MILExample.lean").write_text("import Mathlib\n")
    (src / "sub" / "note.txt").write_text("hello\n")

    blob = _tar_dir(src)

    out = tmp_path / "out"
    out.mkdir()
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        tmp.write(blob)
        tmp.flush()
        with tarfile.open(tmp.name, mode="r:gz") as tf:
            tf.extractall(out, filter="data")

    assert (out / "MILExample.lean").read_text() == "import Mathlib\n"
    assert (out / "sub" / "note.txt").read_text() == "hello\n"


def test_modal_image_name_strips_tag() -> None:
    assert _modal_image_name("open-atp:latest") == "open-atp"
    assert _modal_image_name("open-atp") == "open-atp"
    assert _modal_image_name("registry/org/img:v1") == "registry/org/img"


# --- live Sandbox parity suite ----------------------------------------------


def _live(fn: object) -> object:
    """Mark a test ``modal`` (so ``-m 'not modal'`` skips it) + skip without creds."""
    skip = pytest.mark.skipif(
        not _HAVE_MODAL_CREDS,
        reason="Modal not configured (no MODAL_TOKEN_* env and no ~/.modal.toml)",
    )
    return pytest.mark.modal(skip(fn))


@_live
def test_backend_run_smoke(tmp_path: Path) -> None:
    """``ModalBackend.run`` executes a command over a pushed workdir on Modal."""
    from open_atp.backends.modal import ModalBackend
    from open_atp.images import DEFAULT_IMAGE

    proj = _stage(tmp_path)
    backend = ModalBackend(image=DEFAULT_IMAGE)
    result = backend.run(proj, "lake env lean MILExample.lean", timeout_s=600)

    # The sorry'd fixture compiles (exit 0) but warns about `sorry`.
    assert result.exit_code == 0, result.stdout + result.stderr
    log = result.stdout + result.stderr
    assert "sorry" in log.lower()


@_live
def test_sorry_theorem_compiles_but_is_not_verified(tmp_path: Path) -> None:
    from open_atp.verify import modal_verifier

    project = LeanProject(_stage(tmp_path))
    report = modal_verifier().verify(project)

    assert report.compiles, report.compile_log
    assert not report.sorry_free
    assert not report.verified


@_live
def test_completed_theorem_is_verified(tmp_path: Path) -> None:
    from open_atp.verify import modal_verifier

    proj = _stage(tmp_path)
    (proj / "MILExample.lean").write_text("import Mathlib\n\n" + SOLVED_PROOF)

    report = modal_verifier().verify(LeanProject(proj))

    assert report.compiles, report.compile_log
    assert report.sorry_free
    assert report.verified


@_live
def test_toolchain_mismatch_is_rejected(tmp_path: Path) -> None:
    from open_atp.verify import modal_verifier

    proj = _stage(tmp_path)
    (proj / "lean-toolchain").write_text("leanprover/lean4:v4.99.0\n")

    with pytest.raises(ToolchainMismatch):
        modal_verifier().verify(LeanProject(proj))


@_live
def test_session_runs_many_commands_in_one_sandbox(tmp_path: Path) -> None:
    """One Sandbox, two execs: an edit then an in-session verify, terminated once."""
    from open_atp.backends.modal import ModalBackend
    from open_atp.images import DEFAULT_IMAGE
    from open_atp.verify import Verifier

    proj = _stage(tmp_path)
    (proj / "MILExample.lean").write_text("import Mathlib\n\n" + SOLVED_PROOF)
    backend = ModalBackend(image=DEFAULT_IMAGE)
    verifier = Verifier(backend)

    with backend.session(proj, timeout_s=600) as session:
        # A first exec (stands in for the agent run) then an in-session verify -- both
        # in one Sandbox, terminated once on close.
        assert session.exec("true").wait().exit_code == 0
        report = verifier.verify(LeanProject(proj), session=session)

    assert report.compiles, report.compile_log
    assert report.sorry_free
    assert report.verified
