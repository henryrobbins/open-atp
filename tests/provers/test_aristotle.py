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
from open_atp.verify import VerificationReport

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


def test_missing_api_key_raises_out_of_prove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No API key raises out of prove() before any network call -- not a record."""
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    prover = AristotleProver(backend=DockerBackend(image=DEFAULT_IMAGE))
    out = tmp_path / "run"

    with pytest.raises(MissingCredentials, match="ARISTOTLE_API_KEY"):
        prover.prove(ProofTask(LeanProject(FIXTURE)), out)

    # The run never completed, so no result record was written.
    assert not (out / "logs" / "result.json").exists()


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

    ``_generate`` ends with the shared verify; that half is stubbed here so the
    no-Docker unit test can assert on the extracted/reported files in isolation.
    """
    from open_atp.lean import LeanProject, ProofTask

    monkeypatch.setattr(
        AristotleProver, "_submit_and_download", _fake_result(solved=True)
    )
    prover = _make_prover()
    # Isolate the generation half: stub the shared verify so it doesn't hit Docker.
    monkeypatch.setattr(
        prover.verifier,
        "verify",
        lambda project, session=None: VerificationReport(
            compiles=True, sorry_free=True
        ),
    )
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
    from open_atp.provers.aristotle import ServiceError

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

    with pytest.raises(ServiceError, match=reason):
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
    assert not _drops("Task complete!")


# The next three tests drive the private ``_wait_until_terminal`` directly: it is a
# liveness primitive whose whole contract (settles / times out) is invisible through
# the public API, which is the sanctioned exception to testing via the public seam.


class _PollTask:
    """Stand in for ``AgentTask`` at the network boundary: status advances per poll."""

    agent_task_id = "t-1"
    percent_complete = 0
    last_updated_at = datetime(2024, 1, 1)

    def __init__(self, statuses: list[object]) -> None:
        from aristotlelib.agent_task import TaskStatus

        self.status = TaskStatus.QUEUED
        self._statuses = iter(statuses)
        self.refreshes = 0

    async def refresh(self) -> None:
        self.refreshes += 1
        self.status = next(self._statuses, self.status)


@pytest.mark.parametrize(
    "terminal",
    ["COMPLETE", "COMPLETE_WITH_ERRORS", "OUT_OF_BUDGET", "FAILED", "CANCELED"],
)
def test_wait_until_terminal_returns_on_each_terminal_status(
    terminal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Polling stops as soon as the task settles, whatever it settled as."""
    import asyncio

    from aristotlelib.agent_task import TaskStatus

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    task = _PollTask([TaskStatus.IN_PROGRESS, getattr(TaskStatus, terminal)])
    timed_out = asyncio.run(_make_prover()._wait_until_terminal(task))

    assert not timed_out
    assert task.refreshes == 2


def test_wait_until_terminal_gives_up_at_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task that never settles is bounded by ``timeout_s`` and reports timed-out.

    The regression test for the wedge: the client must stop waiting on its own
    deadline rather than blocking on a connection that never answers.
    """
    import asyncio

    from aristotlelib.agent_task import TaskStatus

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    prover = _make_prover()
    prover.timeout_s = 0  # deadline has already passed on the first check
    task = _PollTask([TaskStatus.IN_PROGRESS])

    assert asyncio.run(prover._wait_until_terminal(task)) is True
    assert task.status is TaskStatus.IN_PROGRESS


def test_wait_until_terminal_retries_a_transient_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped connection mid-poll is retried; the run still settles."""
    import asyncio

    import httpx
    from aristotlelib.agent_task import TaskStatus

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    class _FlakyTask(_PollTask):
        async def refresh(self) -> None:
            await super().refresh()
            if self.refreshes == 1:
                raise httpx.ConnectError("dropped")

    task = _FlakyTask([TaskStatus.IN_PROGRESS, TaskStatus.COMPLETE])
    timed_out = asyncio.run(_make_prover()._wait_until_terminal(task))

    assert not timed_out
    assert task.status is TaskStatus.COMPLETE


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
    assert result.cost_usd == 0.0  # Aristotle is free, so the run costs nothing


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
