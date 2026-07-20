"""ModalBackend tests: a live-Sandbox parity suite plus an offline tar round-trip.

The live tests are marked ``modal`` and skip unless Modal credentials are present
(``MODAL_TOKEN_ID`` / ``MODAL_TOKEN_SECRET`` in the env -- read from ``.env`` by
``conftest`` -- or a ``~/.modal.toml`` profile from ``modal token set``) and require
the image published via ``open-atp build-modal-image``. They mirror
``core/test_verifier.py`` so the shared ``Verifier`` is exercised on Modal exactly
as on Docker. The tar round-trip test needs no Sandbox and always runs.
"""

from __future__ import annotations

import contextvars
import os
import shutil
import tarfile
import tempfile
import threading
from pathlib import Path

import pytest

import open_atp.backends.modal as modal_mod
from open_atp.backends.base import ExecTimeout, SandboxUnreachable
from open_atp.backends.modal import (
    EXEC_DEADLINE_MARGIN_S,
    TIMEOUT_KILL_AFTER_S,
    TRANSFER_TIMEOUT_S,
    WARM_BUILD_TIMEOUT_S,
    ModalBackend,
    ModalCommandHandle,
    _modal_image_name,
    _safe_run,
    _tar_dir,
)
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


def test_wallclock_overhead_sums_provision_and_sync_phases() -> None:
    # Isolated filesystem: push + warm build + pull, plus coreutils-timeout and
    # sb.exec client-deadline buffers, each summed as a backstop -- unlike Docker's
    # few-second bind-mount slack.
    assert ModalBackend().wallclock_overhead_s == (
        TRANSFER_TIMEOUT_S
        + WARM_BUILD_TIMEOUT_S
        + TRANSFER_TIMEOUT_S
        + 3 * TIMEOUT_KILL_AFTER_S
        + 3 * EXEC_DEADLINE_MARGIN_S
    )


# --- offline unit tests with a fake Sandbox ---------------------------------
#
# The push/pull/exec plumbing is pure control flow given a stand-in Sandbox and
# process, so most of it is exercised here without Modal creds or a live Sandbox.
# `_is_dead` is driven off `poll()`, so a "dead" fake short-circuits every
# liveness-gated path (the pull skips, the bound call raises SandboxUnreachable).


class FakeProc:
    """Stand-in for a Modal ContainerProcess: an iterable stdout + a wait()."""

    def __init__(
        self,
        chunks: list[str] | None = None,
        *,
        exit_code: int = 0,
        stderr: list[str] | None = None,
        raise_on_stream: Exception | None = None,
    ) -> None:
        self._chunks = chunks or []
        self._exit_code = exit_code
        self.stderr = stderr or []
        self._raise_on_stream = raise_on_stream

    @property
    def stdout(self):
        if self._raise_on_stream is not None:
            raise self._raise_on_stream
        yield from self._chunks

    def wait(self) -> int:
        return self._exit_code


class FakeSandbox:
    """Stand-in for a Modal Sandbox: liveness via poll(), a recorded terminate()."""

    def __init__(self, *, dead: bool = False) -> None:
        self._dead = dead
        self.terminated = False

    def poll(self):
        return 1 if self._dead else None

    def terminate(self) -> None:
        self.terminated = True


def _handle(sb: FakeSandbox, proc: FakeProc) -> ModalCommandHandle:
    return ModalCommandHandle(proc=proc, sb=sb, started_at=0.0, deadline_s=60.0)


# _safe_run ---------------------------------------------------------------


def test_safe_run_returns_value_and_propagates_context() -> None:
    var: contextvars.ContextVar[str] = contextvars.ContextVar("marker")
    var.set("bound")
    sb = FakeSandbox()
    result = _safe_run(
        lambda: var.get(),
        timeout_s=5,
        sb=sb,
        label="t",
    )
    assert result == "bound"


def test_safe_run_reraises_fn_exception() -> None:
    def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        _safe_run(boom, timeout_s=5, sb=FakeSandbox(), label="t")


def test_safe_run_dead_sandbox_raises_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(modal_mod, "LIVENESS_POLL_INTERVAL_S", 0.02)
    blocked = threading.Event()
    with pytest.raises(SandboxUnreachable):
        _safe_run(
            blocked.wait,
            timeout_s=10,
            sb=FakeSandbox(dead=True),
            label="t",
        )


def test_safe_run_timeout_raises_exec_timeout(monkeypatch) -> None:
    monkeypatch.setattr(modal_mod, "LIVENESS_POLL_INTERVAL_S", 0.02)
    blocked = threading.Event()
    with pytest.raises(ExecTimeout):
        _safe_run(
            blocked.wait,
            timeout_s=0.05,
            sb=FakeSandbox(dead=False),
            label="t",
        )


# stream / drain / collect ---------------------------------------------------


def test_stream_rebuffers_chunks_into_lines() -> None:
    # Modal yields arbitrary chunks: one holds two lines, the next splits a line.
    proc = FakeProc(chunks=['{"a":1}\n{"b":2}\n{"c', '":3}\n'])
    handle = _handle(FakeSandbox(), proc)
    assert list(handle.stream()) == ['{"a":1}', '{"b":2}', '{"c":3}']


def test_stream_maps_errors_via_raise_stream_error() -> None:
    proc = FakeProc(raise_on_stream=RuntimeError("dropped"))
    handle = _handle(FakeSandbox(dead=True), proc)
    with pytest.raises(SandboxUnreachable):
        list(handle.stream())


def test_stream_reaped_sandbox_raises_unreachable(monkeypatch) -> None:
    # The liveness-polled stall detection in _safe_stream is a concurrency primitive:
    # drive it through the public stream() with a stdout that never yields (an
    # unreachable worker delivers no EOF) and a reaped Sandbox, and it must abort at the
    # poll rather than hang. blocked is never set; the producer daemon thread is
    # abandoned, matching _safe_run.
    monkeypatch.setattr(modal_mod, "LIVENESS_POLL_INTERVAL_S", 0.02)
    blocked = threading.Event()

    class BlockingProc(FakeProc):
        @property
        def stdout(self):
            blocked.wait()
            yield ""

    handle = _handle(FakeSandbox(dead=True), BlockingProc())
    with pytest.raises(SandboxUnreachable):
        list(handle.stream())


def test_collect_result_drains_and_reads_stderr() -> None:
    # Dead sandbox => _pull_stderr short-circuits to "", so this stays fully offline.
    proc = FakeProc(chunks=["line1\n", "trailing-no-newline"], exit_code=7)
    handle = _handle(FakeSandbox(dead=True), proc)
    result = handle.wait()
    assert result.exit_code == 7
    assert result.stdout == "line1\ntrailing-no-newline"
    assert result.stderr == ""


# handle teardown semantics --------------------------------------------------


def test_handle_wait_leaves_sandbox_up() -> None:
    # The session, not the handle, owns teardown: wait collects the result and leaves
    # the Sandbox alive for the next exec.
    sb = FakeSandbox(dead=True)
    handle = _handle(sb, FakeProc(chunks=["ok\n"]))
    assert handle.wait().stdout == "ok"
    assert not sb.terminated


# backend + session (offline surface) ----------------------------------------


def test_sync_out_raises_transfer_error_on_dead_sandbox(tmp_path: Path) -> None:
    from open_atp.backends.base import TransferError
    from open_atp.backends.modal import ModalSession

    # sync_out is the result-bearing pull: a dead Sandbox means the workdir can't be
    # retrieved, so it fails loudly rather than silently returning an empty workdir.
    sb = FakeSandbox(dead=True)
    session = ModalSession(backend=ModalBackend(), sb=sb, workdir=tmp_path)
    with pytest.raises(TransferError, match="pull_wd"):
        session.sync_out()


def test_session_close_swallows_pull_failure_and_terminates(tmp_path: Path) -> None:
    from open_atp.backends.modal import ModalSession

    # close()'s teardown pull is best-effort: on a dead Sandbox it must not raise
    # (masking the real outcome), and termination still runs.
    sb = FakeSandbox(dead=True)
    session = ModalSession(backend=ModalBackend(), sb=sb, workdir=tmp_path)
    session.close()
    assert sb.terminated


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
        assert session.exec("true", timeout_s=60).wait().exit_code == 0
        report = verifier.verify(LeanProject(proj), session=session)

    assert report.compiles, report.compile_log
    assert report.sorry_free
    assert report.verified
