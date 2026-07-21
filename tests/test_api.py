"""Registry + prove-lifecycle tests.

Two layers, mirroring the per-prover tests:

* **Fast unit** (no Docker, no creds): :func:`standard_prover` builds each catalog
  prover with the right config + injected backend; the inherited ``prove`` lifecycle
  stages ``output_dir/{wd,logs}``, runs the subclass ``_generate``, verifies, and
  writes ``result.json``; a started run's failure comes back as a failure
  :class:`~open_atp.provers.base.ProofStatus` on the result (only a pre-run input
  rejection raises); ``ProofResult.to_dict`` round-trips to JSON. Provers are stubbed
  with a fake :class:`AutomatedProver`.
* **Docker integration** (``docker`` marker): ``prove`` over ``mil_trivial`` with a
  stubbed-remote :class:`AristotleProver` (reusing the ``_submit_and_download`` seam),
  exercising the real Docker verify.
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from open_atp.auth import AuthKind, AuthStatus
from open_atp.backends.base import (
    ComputeError,
    ExecTimeout,
    ImageUnavailable,
    ProvisionError,
    SandboxDead,
    TransferError,
)
from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover, standard_provers
from open_atp.harness import (
    ClaudeCodeHarness,
    CodexHarness,
    OpenCodeHarness,
)
from open_atp.harness.base import MissingCredentials
from open_atp.images import DEFAULT_IMAGE, Image
from open_atp.lean import LeanProject, ProofTask, ToolchainMismatch, create_project
from open_atp.provers.agent_prover import AgentProver
from open_atp.provers.aristotle import AristotleProver
from open_atp.provers.base import (
    AutomatedProver,
    GenerationTimeout,
    ProofResult,
    ProofStatus,
)
from open_atp.provers.numina import NuminaProver
from open_atp.verify import VerificationReport

FIXTURE = Path(__file__).parent / "fixtures" / "mil_trivial"

SOLVED_FILE = """\
import Mathlib

theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by
  rw [mul_comm a b, mul_assoc b a c]
"""


# --- fake prover (the unit seam) -------------------------------------------


class FakeProver(AutomatedProver):
    """An :class:`AutomatedProver` whose ``_generate`` is scripted -- no backend.

    Its ``_generate`` sets ``result.verification`` itself, so the inherited ``prove``
    never reaches the Docker verify; only :meth:`Verifier.check_compatible` (a pure
    toolchain comparison against the backend image) runs.
    """

    def __init__(
        self,
        name: str,
        *,
        verified: bool = True,
        cost_usd: float | None = 1.0,
        toolchain: str = DEFAULT_IMAGE.lean_toolchain,
        raises: Exception | None = None,
        auth: AuthStatus | None = None,
    ) -> None:
        # The ``toolchain`` knob rides on the backend image to simulate a mismatch;
        # backend construction is offline, so no Docker daemon is contacted.
        super().__init__(backend=DockerBackend(image=Image(lean_toolchain=toolchain)))
        self.name = name
        self._verified = verified
        self._cost = cost_usd
        self._raises = raises
        self._auth = auth or AuthStatus(AuthKind.API_KEY, "FAKE_API_KEY", present=True)

    def auth_status(self) -> AuthStatus:
        return self._auth

    def _generate(
        self, task: ProofTask, wd: Path, logs_dir: Path, result: ProofResult
    ) -> None:
        if self._raises is not None:
            raise self._raises
        (wd / "Out.lean").write_text("theorem t : True := trivial")
        (logs_dir / "stdout.txt").write_text("hello\n")
        result.completed_files = {"Out.lean": "theorem t : True := trivial"}
        result.cost_usd = self._cost
        result.verification = VerificationReport(
            compiles=self._verified, sorry_free=self._verified
        )


def _task() -> ProofTask:
    # mil_trivial pins v4.28.0, matching DEFAULT_IMAGE, so no mismatch up front.
    return ProofTask(LeanProject(FIXTURE))


# --- registry / factory ----------------------------------------------------


def test_standard_prover_constructs_each_catalog_prover() -> None:
    backend = DockerBackend(image=DEFAULT_IMAGE)  # no docker call here

    def build(name: str) -> AutomatedProver:
        return standard_prover(name, backend=backend)

    aristotle = build("aristotle")
    assert isinstance(aristotle, AristotleProver)
    # The toolchain contract rides on the verify backend's image, not the prover.
    assert aristotle.verifier.image.lean_toolchain == DEFAULT_IMAGE.lean_toolchain

    agent = build("claude")
    assert isinstance(agent, AgentProver) and not isinstance(agent, NuminaProver)
    assert isinstance(agent.harness, ClaudeCodeHarness)

    codex = build("codex")
    assert isinstance(codex, AgentProver)
    assert isinstance(codex.harness, CodexHarness)
    assert codex.harness.model == "gpt-5.5"

    deepseek = build("deepseek")
    assert isinstance(deepseek.harness, OpenCodeHarness)
    assert deepseek.harness.provider == "deepseek"
    assert deepseek.harness.auth == "api_key"
    assert deepseek.harness.model == "deepseek-v4-pro"

    grok = build("grok")
    assert isinstance(grok.harness, OpenCodeHarness)
    assert grok.harness.provider == "xai"
    assert grok.harness.auth == "login"
    assert grok.harness.model == "grok-4.5"

    numina = build("numina")
    assert isinstance(numina, NuminaProver)


def test_codex_home_dirs_is_concurrency_safe(tmp_path: Path) -> None:
    # A benchmark sweep shares one CodexHarness across tasks run in parallel. Every
    # concurrent _home_dirs() must return the same staged dir whose auth.json survives
    # -- without the lock the loser's TemporaryDirectory finalizer deletes it.
    import threading

    auth = tmp_path / "auth.json"
    auth.write_text('{"token": "fake"}')
    harness = CodexHarness(auth_file=auth)

    results: list[Path] = []
    barrier = threading.Barrier(8)

    def stage() -> None:
        barrier.wait()  # release all threads into the check-then-create at once
        results.append(harness._home_dirs()[0][0])

    threads = [threading.Thread(target=stage) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len({str(p) for p in results}) == 1
    assert (results[0] / "auth.json").is_file()


def test_standard_prover_uses_one_backend_for_generation_and_verify() -> None:
    backend = DockerBackend(image=DEFAULT_IMAGE)

    agent = standard_prover("claude", backend=backend)

    assert isinstance(agent, AgentProver)
    # The catalog builds the name's defaults; there is no override surface.
    assert agent.harness.model == "claude-opus-4-8"
    # One backend: generation runs in a live session over it and verification reuses
    # that same hot sandbox.
    assert agent.verifier.backend is backend


def test_standard_prover_rejects_unknown_name() -> None:
    backend = DockerBackend(image=DEFAULT_IMAGE)
    with pytest.raises(ValueError, match="unknown prover"):
        standard_prover("nope", backend=backend)
    assert {"aristotle", "claude", "numina"} <= set(standard_provers())


def test_max_duration_sums_generation_verify_and_backend_overhead() -> None:
    prover = FakeProver("fake")

    # The ceiling is the three bounded phases, no hidden padding.
    assert prover.max_duration_s == (
        prover.timeout_s
        + prover.verifier.timeout_s
        + prover.verifier.backend.wallclock_overhead_s
    )
    # Concretely for the defaults over a Docker backend (1800 + 600 + 30).
    assert prover.max_duration_s == 2430


# --- prove: lifecycle ------------------------------------------------------


def test_prove_populates_output_dir_and_verifies(tmp_path: Path) -> None:
    out = tmp_path / "run"
    result = FakeProver("agent").prove(_task(), out)

    assert isinstance(result, ProofResult)
    assert result.prover == "agent" and result.success
    assert result.status is ProofStatus.VERIFIED
    # output_dir/{wd,logs} laid out and populated.
    assert result.output_dir == out
    assert result.wd == out / "wd" and result.logs_dir == out / "logs"
    assert (result.wd / "Out.lean").is_file()
    assert (result.logs_dir / "stdout.txt").read_text() == "hello\n"
    # A self-describing result.json is written beside the logs.
    payload = json.loads((result.logs_dir / "result.json").read_text())
    assert payload["success"] is True and payload["prover"] == "agent"
    assert result.duration_s is not None


def test_prove_records_a_started_run_failure_as_a_result(tmp_path: Path) -> None:
    """A started run's failure is a record, not a raise -- partial artifacts survive."""
    boom = FakeProver("agent", raises=RuntimeError("docker down"))

    result = boom.prove(_task(), tmp_path / "run")

    assert result.status is ProofStatus.ERROR
    assert result.error == "RuntimeError" and result.error_msg == "docker down"
    assert result.verification is None and not result.success
    # The run's output layout is still pointed at, and a result.json still written.
    assert result.output_dir == tmp_path / "run" and result.logs_dir.is_dir()
    payload = json.loads((result.logs_dir / "result.json").read_text())
    assert payload["status"] == "error"
    assert payload["error"] == "RuntimeError" and payload["error_msg"] == "docker down"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        # Only a generation-budget exhaustion is its own bucket; every other
        # started-run failure -- infra stalls included -- is a plain ERROR, told apart
        # by the recorded exception class name in ``error``.
        (GenerationTimeout("out of budget"), ProofStatus.TIMEOUT),
        (ExecTimeout("t"), ProofStatus.ERROR),
        (SandboxDead("s"), ProofStatus.ERROR),
        (TransferError("pull_wd: gone"), ProofStatus.ERROR),
        (ComputeError("c"), ProofStatus.ERROR),
        (RuntimeError("boom"), ProofStatus.ERROR),
    ],
)
def test_prove_records_a_started_run_failure(
    exc: Exception, expected: ProofStatus, tmp_path: Path
) -> None:
    """A failure once the run has started comes back as a classified record."""
    result = FakeProver("agent", raises=exc).prove(
        _task(), tmp_path / type(exc).__name__
    )
    assert result.status is expected
    assert result.verification is None and not result.success
    # The class name is the search key; the message is the human "why".
    assert result.error == type(exc).__name__
    assert result.error_msg == str(exc)
    # The record is self-describing on disk too.
    payload = json.loads((result.logs_dir / "result.json").read_text())
    assert payload["status"] == expected.value


@pytest.mark.parametrize(
    "exc",
    [
        MissingCredentials("no key"),
        ProvisionError("docker run: daemon down"),
        ImageUnavailable("image not built"),
    ],
)
def test_prove_reraises_a_run_that_never_started(
    exc: Exception, tmp_path: Path
) -> None:
    """Absent credentials / a failed provision raise out of prove, not a record."""
    prover = FakeProver("agent", raises=exc)
    with pytest.raises(type(exc)):
        prover.prove(_task(), tmp_path / "run")


def test_prove_rejects_toolchain_mismatch_before_generating(tmp_path: Path) -> None:
    """A pre-run input rejection raises -- there is no run to record."""
    prover = FakeProver("agent", toolchain="leanprover/lean4:v9.99.0")
    with pytest.raises(ToolchainMismatch):
        prover.prove(_task(), tmp_path / "run")
    # It failed up front -- before _generate wrote anything.
    assert not (tmp_path / "run" / "wd" / "Out.lean").exists()


def test_prove_unverified_candidate_sets_status(tmp_path: Path) -> None:
    result = FakeProver("agent", verified=False).prove(_task(), tmp_path / "run")
    assert not result.success
    assert result.status is ProofStatus.UNVERIFIED


def test_prove_verified_candidate_sets_status(tmp_path: Path) -> None:
    result = FakeProver("agent", verified=True).prove(_task(), tmp_path / "run")
    assert result.success
    assert result.status is ProofStatus.VERIFIED
    assert result.error is None and result.error_msg is None


def _expiring(minutes: float) -> AuthStatus:
    return AuthStatus(
        AuthKind.OAUTH,
        "SOME_TOKEN",
        present=True,
        expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
    )


def test_prove_warns_when_the_credential_expires_mid_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    prover = FakeProver("agent", auth=_expiring(5))

    with caplog.at_level(logging.WARNING, logger="open_atp"):
        result = prover.prove(_task(), tmp_path / "run")

    assert any("expires soon" in r.message for r in caplog.records)
    assert result.success  # a warning, not a refusal


def test_prove_stays_quiet_for_a_credential_that_outlasts_the_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    prover = FakeProver("agent", auth=_expiring(60))

    with caplog.at_level(logging.WARNING, logger="open_atp"):
        prover.prove(_task(), tmp_path / "run")

    assert not [r for r in caplog.records if "expires soon" in r.message]


def test_prove_survives_an_unreadable_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-flight read that blows up must not be what fails the run."""

    def boom() -> AuthStatus:
        raise OSError("credential registry unreachable")

    prover = FakeProver("agent")
    monkeypatch.setattr(prover, "auth_status", boom)

    assert prover.prove(_task(), tmp_path / "run").success


def _render(table: object) -> str:
    """Render a rich table through a non-tty console: markup collapses to glyphs."""
    from rich.console import Console

    console = Console(
        file=io.StringIO(), width=200, force_terminal=False, color_system=None
    )
    console.print(table)
    return console.file.getvalue()  # type: ignore[attr-defined]


def test_proof_table_status_cell_reflects_the_outcome(tmp_path: Path) -> None:
    """The CLI summary table shows verified / unverified / errored status per run."""
    from open_atp.__main__ import _proof_table

    verified = FakeProver("agent", verified=True).prove(_task(), tmp_path / "ok")
    assert "verified" in _render(_proof_table(verified))

    unverified = FakeProver("agent", verified=False).prove(_task(), tmp_path / "miss")
    assert "unverified" in _render(_proof_table(unverified))

    errored = FakeProver("agent", raises=RuntimeError("docker down")).prove(
        _task(), tmp_path / "boom"
    )
    rendered = _render(_proof_table(errored))
    # The non-terminal status prints its label and names the failing exception class.
    assert "error" in rendered and "RuntimeError" in rendered


# --- serialization ---------------------------------------------------------


def test_to_dict_round_trips_to_json(tmp_path: Path) -> None:
    result = FakeProver("agent", cost_usd=1.5).prove(_task(), tmp_path / "run")

    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["prover"] == "agent"
    assert payload["success"] is True
    assert payload["verification"]["verified"] is True
    assert payload["cost_usd"] == 1.5
    assert payload["wd"] == str(result.wd)
    assert payload["logs_dir"] == str(result.logs_dir)
    assert "logs" not in payload  # the in-memory log string is gone


def test_to_dict_carries_error_for_a_failed_result(tmp_path: Path) -> None:
    result = ProofResult(
        prover="agent",
        verification=None,
        output_dir=tmp_path / "run",
        error="RuntimeError",
        error_msg="nope",
    )
    payload = result.to_dict()
    assert payload["success"] is False
    assert payload["verification"] is None
    assert payload["error"] == "RuntimeError" and payload["error_msg"] == "nope"


# --- staging bare files ----------------------------------------------------


def test_create_project_builds_a_project_from_bare_lean(tmp_path: Path) -> None:
    project = create_project([FIXTURE / "MILExample.lean"], tmp_path / "staged")
    assert isinstance(project, LeanProject)
    assert (project.root / "MILExample.lean").is_file()
    assert (project.root / "lean-toolchain").is_file()
    assert project.lean_toolchain == DEFAULT_IMAGE.lean_toolchain


def test_create_project_rejects_non_lean(tmp_path: Path) -> None:
    (tmp_path / "foo.txt").write_text("nope")
    with pytest.raises(ValueError, match="Expected a .lean file"):
        create_project([tmp_path / "foo.txt"], tmp_path / "staged")


# --- Docker integration: real verify ---------------------------------------


def _fake_aristotle_remote(*, solved: bool) -> object:
    """Async stand-in for AristotleProver._submit_and_download (writes an archive)."""
    body = SOLVED_FILE if solved else (FIXTURE / "MILExample.lean").read_text()

    async def _stub(
        self: AristotleProver,
        project_dir: Path,
        prompt: str,
        dest_tar: Path,
        logs_dir: Path,
    ) -> tuple[Path, dict[str, object]]:
        with tarfile.open(dest_tar, "w:gz") as tar:
            data = body.encode()
            info = tarfile.TarInfo(f"{project_dir.name}_aristotle/MILExample.lean")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        return dest_tar, {"project_id": "test-123", "task_status": "COMPLETE"}

    return _stub


@pytest.mark.docker
def test_prove_aristotle_with_real_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stubbed-remote Aristotle generation + the real Docker verify."""
    monkeypatch.setattr(
        AristotleProver, "_submit_and_download", _fake_aristotle_remote(solved=True)
    )
    backend = DockerBackend(image=DEFAULT_IMAGE)
    prover = standard_prover("aristotle", backend=backend)

    result = prover.prove(_task(), tmp_path / "run")

    report = result.verification
    assert result.success, report and report.compile_log
    assert report is not None and report.verified
    assert (result.wd / "MILExample.lean").is_file()


@pytest.mark.docker
def test_prove_catches_real_unverified_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sorry left in by the remote is caught by the real verifier."""
    monkeypatch.setattr(
        AristotleProver, "_submit_and_download", _fake_aristotle_remote(solved=False)
    )
    backend = DockerBackend(image=DEFAULT_IMAGE)
    prover = standard_prover("aristotle", backend=backend)

    result = prover.prove(_task(), tmp_path / "run")
    assert not result.success
    assert result.verification is not None and not result.verification.sorry_free
