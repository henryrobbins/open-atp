"""Registry + prove-lifecycle tests.

Two layers, mirroring the per-prover tests:

* **Fast unit** (no Docker, no creds): :func:`standard_prover` builds each catalog
  prover with the right config + injected backend; the inherited ``prove`` lifecycle
  stages ``output_dir/{wd,logs}``, runs the subclass ``_generate``, verifies, and
  writes ``result.json``; a prover that raises propagates (no orchestration layer
  swallows it); ``ProofResult.to_dict`` round-trips to JSON. Provers are stubbed with
  a fake :class:`AutomatedProver`.
* **Docker integration** (``docker`` marker): ``prove`` over ``mil_trivial`` with a
  stubbed-remote :class:`AristotleProver` (reusing the ``_submit_and_download`` seam),
  exercising the real Docker verify.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover, standard_provers
from open_atp.harness import (
    ClaudeCodeHarness,
    CodexHarness,
    OpenCodeHarness,
)
from open_atp.images import DEFAULT_IMAGE, Image
from open_atp.lean import LeanProject, ProofTask, ToolchainMismatch, create_project
from open_atp.provers.agent_prover import AgentProver
from open_atp.provers.aristotle import AristotleProver
from open_atp.provers.base import AutomatedProver, ProofResult
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
    ) -> None:
        # The ``toolchain`` knob rides on the backend image to simulate a mismatch;
        # backend construction is offline, so no Docker daemon is contacted.
        super().__init__(backend=DockerBackend(image=Image(lean_toolchain=toolchain)))
        self.name = name
        self._verified = verified
        self._cost = cost_usd
        self._raises = raises

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

    opencode = build("opencode")
    assert isinstance(opencode.harness, OpenCodeHarness)

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


# --- prove: lifecycle ------------------------------------------------------


def test_prove_populates_output_dir_and_verifies(tmp_path: Path) -> None:
    out = tmp_path / "run"
    result = FakeProver("agent").prove(_task(), out)

    assert isinstance(result, ProofResult)
    assert result.prover == "agent" and result.success
    # output_dir/{wd,logs} laid out and populated.
    assert result.output_dir == out
    assert result.wd == out / "wd" and result.logs_dir == out / "logs"
    assert (result.wd / "Out.lean").is_file()
    assert (result.logs_dir / "stdout.txt").read_text() == "hello\n"
    # A self-describing result.json is written beside the logs.
    payload = json.loads((result.logs_dir / "result.json").read_text())
    assert payload["success"] is True and payload["prover"] == "agent"
    assert result.duration_s is not None


def test_prove_propagates_a_failing_generate(tmp_path: Path) -> None:
    """No orchestration layer swallows the error: prove raises straight through."""
    boom = FakeProver("agent", raises=RuntimeError("docker down"))
    with pytest.raises(RuntimeError, match="docker down"):
        boom.prove(_task(), tmp_path / "run")


def test_prove_rejects_toolchain_mismatch_before_generating(tmp_path: Path) -> None:
    prover = FakeProver("agent", toolchain="leanprover/lean4:v9.99.0")
    with pytest.raises(ToolchainMismatch):
        prover.prove(_task(), tmp_path / "run")
    # It failed up front -- before _generate wrote anything.
    assert not (tmp_path / "run" / "wd" / "Out.lean").exists()


def test_prove_runs_standalone_verify_when_generate_leaves_it_unset(
    tmp_path: Path,
) -> None:
    """If _generate does not set verification, prove falls back to the verifier."""
    calls: list[LeanProject] = []

    class NoVerifyProver(FakeProver):
        def _generate(self, task, wd, logs_dir, result):  # type: ignore[no-untyped-def]
            # Stage a real lake project so the base's LeanProject(wd) is valid.
            shutil.copytree(task.project.root, wd, dirs_exist_ok=True)
            result.completed_files = {"MILExample.lean": "x"}

    prover = NoVerifyProver("agent")
    report = VerificationReport(compiles=True, sorry_free=True)
    prover.verifier.verify = lambda project: (  # type: ignore[method-assign]
        calls.append(project) or report
    )
    result = prover.prove(_task(), tmp_path / "run")
    assert result.verification is report and len(calls) == 1


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
        error="RuntimeError: nope",
    )
    payload = result.to_dict()
    assert payload["success"] is False
    assert payload["verification"] is None
    assert payload["error"] == "RuntimeError: nope"


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
