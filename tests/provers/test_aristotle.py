"""AristotleProver tests.

The remote Aristotle call (``_submit_and_download``) is stubbed with a fake result
archive, so these run with no API key and no network. The full ``run()`` test still
exercises the *real* Docker verifier -- mocked remote, real local check -- which is
exactly the path every prover shares.
"""

from __future__ import annotations

import io
import tarfile
from datetime import datetime
from pathlib import Path

import pytest

from open_atp.backends.docker import DockerBackend
from open_atp.harness import MissingCredentials
from open_atp.images import DEFAULT_IMAGE
from open_atp.lean import LeanProject, ProofTask
from open_atp.provers.aristotle import AristotleProver
from open_atp.provers.base import ProofResult

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"

SOLVED_FILE = """\
import Mathlib

theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by
  rw [mul_comm a b, mul_assoc b a c]
"""

SUMMARY = "# Summary\nFilled the single sorry; project compiles.\n"


async def _noop_sleep(_seconds: float) -> None:
    """Stand in for asyncio.sleep so backoff doesn't slow the retry tests."""


def _make_prover() -> AristotleProver:
    backend = DockerBackend(image=DEFAULT_IMAGE)
    return AristotleProver(backend=backend)


def test_missing_api_key_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No API key raises out of prove() before any network call, like a mismatch."""
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    prover = AristotleProver(backend=DockerBackend(image=DEFAULT_IMAGE))
    out = tmp_path / "run"

    with pytest.raises(MissingCredentials, match="ARISTOTLE_API_KEY"):
        prover.prove(ProofTask(LeanProject(FIXTURE)), out)

    assert not (out / "wd").exists()


def _fake_result(*, solved: bool) -> object:
    """An async stand-in for ``_submit_and_download`` that writes a result archive."""
    body = SOLVED_FILE if solved else open(FIXTURE / "MILExample.lean").read()

    async def _stub(
        self: AristotleProver,
        project_dir: Path,
        prompt: str,
        dest_tar: Path,
        logs_dir: Path,
    ) -> tuple[Path, dict[str, object]]:
        # Real Aristotle archives wrap everything under a single top-level dir;
        # mirror that so the test exercises _extract_over's unwrapping.
        with tarfile.open(dest_tar, "w:gz") as tar:
            for name, text in (
                ("MILExample.lean", body),
                ("ARISTOTLE_SUMMARY.md", SUMMARY),
            ):
                data = text.encode()
                info = tarfile.TarInfo(f"{project_dir.name}_aristotle/{name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        # Stand in for the real run-info sync so prove() sees a populated logs dir.
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / "events.json").write_text("[]")
        return dest_tar, {
            "project_id": "test-123",
            "task_status": "COMPLETE",
            "logs_dir": str(logs_dir),
        }

    return _stub


def test_generate_extracts_result_and_reports_changed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_generate alone: stage -> (stub) submit -> extract -> diff. No Docker needed.

    The public ``prove`` runs the shared Docker verify, so the no-Docker unit test
    drives the generation half directly and asserts on the filled result.
    """
    from open_atp.lean import LeanProject, ProofTask

    monkeypatch.setattr(
        AristotleProver, "_submit_and_download", _fake_result(solved=True)
    )
    prover = _make_prover()
    wd = tmp_path / "wd"
    logs_dir = tmp_path / "logs"
    wd.mkdir()
    logs_dir.mkdir()
    result = ProofResult(prover="aristotle", verification=None, output_dir=tmp_path)

    prover._generate(ProofTask(LeanProject(FIXTURE)), wd, logs_dir, result)

    # The completed file landed in the workdir and is reported as changed.
    assert (wd / "MILExample.lean").read_text() == SOLVED_FILE
    assert "MILExample.lean" in result.completed_files
    assert "rw [mul_comm" in result.completed_files["MILExample.lean"]
    assert result.metadata["project_id"] == "test-123"
    # The hosted agent's summary and the run record landed in the logs dir.
    assert "Summary" in (logs_dir / "summary.md").read_text()
    assert (logs_dir / "events.json").is_file()


def _fake_no_output(reason: str) -> object:
    """An async stand-in for ``_submit_and_download`` that produced no candidate."""

    async def _stub(
        self: AristotleProver,
        project_dir: Path,
        prompt: str,
        dest_tar: Path,
        logs_dir: Path,
    ) -> tuple[None, dict[str, object]]:
        return None, {"project_id": "test-123", "error": reason}

    return _stub


def test_generate_raises_when_aristotle_produces_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No candidate archive is an error, not a silent verify of the original project."""
    from open_atp.provers.aristotle import AristotleNoOutput

    reason = "Aristotle produced no output files."
    monkeypatch.setattr(
        AristotleProver, "_submit_and_download", _fake_no_output(reason)
    )
    prover = _make_prover()
    wd = tmp_path / "wd"
    logs_dir = tmp_path / "logs"
    wd.mkdir()
    logs_dir.mkdir()
    result = ProofResult(prover="aristotle", verification=None, output_dir=tmp_path)

    with pytest.raises(AristotleNoOutput, match=reason):
        prover._generate(ProofTask(LeanProject(FIXTURE)), wd, logs_dir, result)

    # The reason and run metadata survive on the result for the caller to inspect.
    assert result.metadata["error"] == reason


def test_is_transient_distinguishes_dropped_links_from_real_errors() -> None:
    """Transport failures and 5xx retry; a 4xx (bad key/missing project) fails fast."""
    import httpx
    from aristotlelib.api_request import AristotleAPIError

    from open_atp.provers.aristotle import _is_transient

    assert _is_transient(httpx.RemoteProtocolError("stream dropped"))
    assert _is_transient(httpx.ConnectError("refused"))
    assert _is_transient(AristotleAPIError("Request failed: ...", status_code=None))
    assert _is_transient(AristotleAPIError("server", status_code=503))
    assert not _is_transient(AristotleAPIError("bad key", status_code=401))
    assert not _is_transient(ValueError("unrelated"))


def test_quiet_aristotle_logger_drops_expected_noise() -> None:
    """The filter drops the .lake / dropped-connection noise, keeps everything else."""
    import logging

    from open_atp.provers.aristotle import _quiet_aristotle_logger

    _quiet_aristotle_logger()
    _quiet_aristotle_logger()  # idempotent: no duplicate filter
    logger = logging.getLogger("aristotle")

    def _drops(msg: str) -> bool:
        record = logger.makeRecord(
            "aristotle", logging.WARNING, __file__, 0, msg, (), None
        )
        return not all(f.filter(record) for f in logger.filters)

    assert _drops("WARNING: Your project contains .lean files but no .lake folder.")
    assert _drops("Connection to server was interrupted. Use 'aristotle show x'.")
    assert not _drops("Task complete!")


def test_wait_until_terminal_resumes_after_dropped_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped stream returns a still-running task; we re-attach until it settles.

    Mirrors the production bug: ``wait_for_completion`` swallows the dropped link and
    returns IN_PROGRESS, so without resuming we'd report the run failed even though it
    completes server-side.
    """
    import asyncio

    from aristotlelib.agent_task import TaskStatus

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    # First wait drops the link (status stays IN_PROGRESS); the second wait completes.
    statuses = iter([TaskStatus.IN_PROGRESS, TaskStatus.COMPLETE])

    class _FakeTask:
        agent_task_id = "t-1"
        status = TaskStatus.QUEUED
        percent_complete = 0
        last_updated_at = datetime(2024, 1, 1)
        waits = 0

        async def wait_for_completion(self) -> None:
            self.waits += 1

        async def refresh(self) -> None:
            self.status = next(statuses)

    task = _FakeTask()
    asyncio.run(_make_prover()._wait_until_terminal(task))

    assert task.waits == 2  # resumed exactly once after the drop
    assert task.status is TaskStatus.COMPLETE


def test_wait_until_terminal_reconnects_past_budget_while_progressing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long run that keeps advancing reconnects without limit: the budget is a stall
    counter, not a lifetime cap.

    The stream drops on every attempt and the task stays IN_PROGRESS far longer than
    ``max_resume_attempts`` -- but because ``percent_complete`` advances each refresh,
    the stall budget keeps resetting, so we never give up before it completes.
    """
    import asyncio

    from aristotlelib.agent_task import TaskStatus

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    class _ProgressingTask:
        agent_task_id = "t-3"
        status = TaskStatus.IN_PROGRESS
        last_updated_at = datetime(2024, 1, 1)
        percent_complete = 0
        waits = 0

        async def wait_for_completion(self) -> None:
            self.waits += 1

        async def refresh(self) -> None:
            # Advance until well past the budget, then settle.
            if self.percent_complete >= 90:
                self.status = TaskStatus.COMPLETE
            else:
                self.percent_complete += 10

    prover = _make_prover()
    prover.max_resume_attempts = 3
    task = _ProgressingTask()
    asyncio.run(prover._wait_until_terminal(task))

    assert task.status is TaskStatus.COMPLETE
    assert task.waits > prover.max_resume_attempts  # never gave up despite the cap


def test_wait_until_terminal_is_bounded_by_timeout() -> None:
    """A hung wait is cancellable at the deadline, so ``timeout_s`` can cap it.

    ``_submit_and_download`` wraps the wait in ``asyncio.wait_for(..., timeout_s)``;
    this checks the wait actually yields at its await points so the cap fires instead
    of blocking forever.
    """
    import asyncio

    from aristotlelib.agent_task import TaskStatus

    class _HangingTask:
        agent_task_id = "t-4"
        status = TaskStatus.IN_PROGRESS
        percent_complete = 0
        last_updated_at = datetime(2024, 1, 1)

        async def wait_for_completion(self) -> None:
            await asyncio.sleep(3600)  # never settles within the test

        async def refresh(self) -> None:
            pass

    async def _run() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                _make_prover()._wait_until_terminal(_HangingTask()), timeout=0.05
            )

    asyncio.run(_run())


def test_wait_until_terminal_gives_up_after_resume_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the stream never settles, stop after the budget instead of looping forever."""
    import asyncio

    from aristotlelib.agent_task import TaskStatus

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    class _StuckTask:
        agent_task_id = "t-2"
        status = TaskStatus.IN_PROGRESS
        percent_complete = 50
        last_updated_at = datetime(2024, 1, 1)
        waits = 0

        async def wait_for_completion(self) -> None:
            self.waits += 1

        async def refresh(self) -> None:
            pass  # never progresses, never reaches a terminal state

    prover = _make_prover()
    prover.max_resume_attempts = 3
    task = _StuckTask()
    asyncio.run(prover._wait_until_terminal(task))

    assert task.waits == 3  # bounded by the resume budget; no infinite loop


@pytest.mark.docker
def test_prove_end_to_end_verifies_completed_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full run(): mocked remote returns a real proof; Docker verifier confirms it."""
    from open_atp.lean import LeanProject, ProofTask

    monkeypatch.setattr(
        AristotleProver, "_submit_and_download", _fake_result(solved=True)
    )
    prover = _make_prover()

    result = prover.prove(ProofTask(LeanProject(FIXTURE)), tmp_path / "out")

    assert result.success, result.verification and result.verification.compile_log
    assert result.verification is not None and result.verification.verified
    assert result.prover == "aristotle"
    assert result.cost_usd is None  # Aristotle exposes no per-run cost


@pytest.mark.docker
def test_prove_reports_unverified_when_sorry_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Aristotle returns the file still sorry'd, the verifier catches it."""
    from open_atp.lean import LeanProject, ProofTask

    monkeypatch.setattr(
        AristotleProver, "_submit_and_download", _fake_result(solved=False)
    )
    prover = _make_prover()

    result = prover.prove(ProofTask(LeanProject(FIXTURE)), tmp_path / "out")

    assert not result.success
    assert result.verification is not None and not result.verification.sorry_free
