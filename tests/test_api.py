"""Platform (phase-5) tests.

Two layers, mirroring the per-prover tests:

* **Fast unit** (no Docker, no creds): the registry/factory builds each prover with
  the right config + injected backend; ``solve`` fans out and aggregates; a prover
  that raises is captured (not propagated) and the others still return; ``best()``
  picks verified-then-cheapest; ``to_dict`` round-trips to JSON. Provers are stubbed
  with a fake :class:`AutomatedProver`.
* **Docker integration** (``docker`` marker): ``solve`` over ``mil_trivial`` with a
  stubbed-remote :class:`AristotleProver` (reusing phase-2's ``_submit_and_download``
  seam) + a fake verified prover, exercising the real Docker verify and the
  concurrent aggregation.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from open_afps.api import (
    Platform,
    SolveResult,
    available_provers,
    build_prover,
    stage_files,
)
from open_afps.backends.docker import DockerBackend, DockerConfig
from open_afps.core.prover import AutomatedProver
from open_afps.core.result import GenerationOutput, ProofResult, VerificationReport
from open_afps.core.task import LeanProject, ProofTask, ToolchainMismatch
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_afps.provers.agent_prover import AgentProver, AgentProverConfig
from open_afps.provers.aristotle import AristotleProver, AristotleProverConfig
from open_afps.provers.numina import NuminaProver, NuminaProverConfig

FIXTURE = Path(__file__).parent / "fixtures" / "mil_trivial"

SOLVED_FILE = """\
import Mathlib

theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by
  rw [mul_comm a b, mul_assoc b a c]
"""


# --- fake prover (the unit seam) -------------------------------------------


class FakeProver(AutomatedProver):
    """An :class:`AutomatedProver` whose ``run`` is fully scripted -- no backend."""

    def __init__(
        self,
        name: str,
        *,
        verified: bool = True,
        cost_usd: float | None = 1.0,
        duration_s: float | None = 1.0,
        raises: Exception | None = None,
    ) -> None:
        # Deliberately skip super().__init__: no backend/Verifier needed.
        self.name = name
        self._verified = verified
        self._cost = cost_usd
        self._duration = duration_s
        self._raises = raises

    def prove(self, task: ProofTask, workdir: Path) -> GenerationOutput:
        raise NotImplementedError  # pragma: no cover

    def run(self, task: ProofTask, workdir: Path) -> ProofResult:
        if self._raises is not None:
            raise self._raises
        report = VerificationReport(compiles=self._verified, sorry_free=self._verified)
        return ProofResult(
            prover=self.name,
            verification=report,
            cost_usd=self._cost,
            duration_s=self._duration,
            artifacts_dir=workdir,
        )


def _task() -> ProofTask:
    # mil_trivial pins v4.28.0, matching DEFAULT_TOOLCHAIN, so no mismatch up front.
    return ProofTask(LeanProject(FIXTURE))


# --- registry / factory ----------------------------------------------------


def test_build_prover_constructs_each_registered_prover() -> None:
    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))  # no docker call here

    def build(spec: str) -> AutomatedProver:
        return build_prover(spec, verification_backend=backend)

    aristotle = build("aristotle")
    assert isinstance(aristotle, AristotleProver)
    assert isinstance(aristotle.config, AristotleProverConfig)
    assert aristotle.config.supported_toolchain == DEFAULT_TOOLCHAIN

    agent = build("agent")
    assert isinstance(agent, AgentProver) and not isinstance(agent, NuminaProver)
    assert isinstance(agent.config, AgentProverConfig)
    assert agent.config.harness == "claude_code"

    codex = build("agent:codex")
    assert isinstance(codex, AgentProver)
    assert codex.config.harness == "codex"

    opencode = build("agent:opencode")
    assert opencode.config.harness == "opencode"

    numina = build("numina")
    assert isinstance(numina, NuminaProver)
    assert isinstance(numina.config, NuminaProverConfig)


def test_build_prover_applies_overrides_and_injects_agent_backend() -> None:
    verify = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    agent_be = DockerBackend(DockerConfig(image="other:tag"))

    agent = build_prover(
        "agent",
        verification_backend=verify,
        agent_backend=agent_be,
        overrides={"model": "claude-sonnet-4-6", "effort": "low"},
    )
    assert isinstance(agent, AgentProver)
    assert agent.config.model == "claude-sonnet-4-6"
    assert agent.config.effort == "low"
    # Generation backend is the injected one; verification stays the verify backend.
    assert agent.agent_backend is agent_be
    assert agent.verifier.backend is verify


def test_build_prover_rejects_unknown_spec() -> None:
    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    with pytest.raises(ValueError, match="Unknown prover"):
        build_prover("nope", verification_backend=backend)
    assert set(available_provers()) >= {"aristotle", "agent", "numina"}


# --- solve: fan-out + aggregation ------------------------------------------


def _platform(tmp_path: Path) -> Platform:
    return Platform(runs_dir=tmp_path / "runs")


def test_solve_fans_out_and_isolates_workdirs(tmp_path: Path) -> None:
    platform = _platform(tmp_path)
    p1 = FakeProver("agent", cost_usd=2.0)
    p2 = FakeProver("aristotle", cost_usd=None)

    result = platform.solve(_task(), [p1, p2])

    assert isinstance(result, SolveResult)
    assert {r.prover for r in result.results} == {"agent", "aristotle"}
    # Each prover got its own workdir under runs/<id>/<label>/.
    dirs = {r.artifacts_dir for r in result.results}
    assert len(dirs) == 2
    for d in dirs:
        # Each run stages as runs/<id>/<label>/wd, so the workdir's grandparent is
        # the run dir.
        assert d is not None and d.is_dir() and d.parent.parent == result.run_dir


def test_solve_duplicate_names_get_distinct_workdirs(tmp_path: Path) -> None:
    platform = _platform(tmp_path)
    result = platform.solve(_task(), [FakeProver("agent"), FakeProver("agent")])
    labels = sorted(r.prover for r in result.results)
    assert labels == ["agent", "agent_1"]
    assert len({r.artifacts_dir for r in result.results}) == 2


def test_download_wd_and_logs_copy_each_bucket(tmp_path: Path) -> None:
    """ProofResult.download_{wd,logs} copy the proof project and run record apart."""
    wd = tmp_path / "run" / "agent" / "wd"
    logs = tmp_path / "run" / "agent" / "logs"
    wd.mkdir(parents=True)
    logs.mkdir(parents=True)
    (wd / "MILExample.lean").write_text("theorem t : True := trivial")
    (logs / "stdout.jsonl").write_text('{"type":"result"}')

    result = ProofResult(
        prover="agent",
        verification=VerificationReport(compiles=True, sorry_free=True),
        artifacts_dir=wd,
        logs_dir=logs,
    )

    result.download_wd(tmp_path / "out_wd")
    result.download_logs(tmp_path / "out_logs")

    assert (tmp_path / "out_wd" / "MILExample.lean").is_file()
    assert (tmp_path / "out_logs" / "stdout.jsonl").is_file()
    # The buckets stay separate -- logs never leak into the downloaded workdir.
    assert not (tmp_path / "out_wd" / "stdout.jsonl").exists()


def test_download_raises_without_artifacts(tmp_path: Path) -> None:
    """A result that produced nothing to download fails loudly, not silently."""
    result = ProofResult(prover="agent", verification=None)
    with pytest.raises(FileNotFoundError):
        result.download_wd(tmp_path / "out_wd")
    with pytest.raises(FileNotFoundError):
        result.download_logs(tmp_path / "out_logs")


def test_solve_result_download_fans_out_per_prover(tmp_path: Path) -> None:
    """SolveResult.download_{wd,logs} write one subdir per prover, skipping empties."""
    a_wd = tmp_path / "a" / "wd"
    a_logs = tmp_path / "a" / "logs"
    a_wd.mkdir(parents=True)
    a_logs.mkdir(parents=True)
    (a_wd / "P.lean").write_text("x")
    (a_logs / "stdout.jsonl").write_text("y")

    good = ProofResult(
        prover="agent", verification=None, artifacts_dir=a_wd, logs_dir=a_logs
    )
    empty = ProofResult(prover="aristotle", verification=None)  # nothing to download
    solve = SolveResult(run_id="r", results=[good, empty])

    solve.download_wd(tmp_path / "wd_out")
    solve.download_logs(tmp_path / "logs_out")

    assert (tmp_path / "wd_out" / "agent" / "P.lean").is_file()
    assert (tmp_path / "logs_out" / "agent" / "stdout.jsonl").is_file()
    # The prover with no artifacts is silently skipped, not errored.
    assert not (tmp_path / "wd_out" / "aristotle").exists()


def test_solve_isolates_a_failing_prover(tmp_path: Path) -> None:
    platform = _platform(tmp_path)
    boom = FakeProver("agent", raises=RuntimeError("docker down"))
    ok = FakeProver("aristotle", verified=True)

    result = platform.solve(_task(), [boom, ok])

    failed = next(r for r in result.results if r.prover == "agent")
    assert failed.error is not None and "docker down" in failed.error
    assert failed.verification is None and not failed.success
    # The sibling still ran and verified.
    survived = next(r for r in result.results if r.prover == "aristotle")
    assert survived.success
    assert [r.prover for r in result.verified()] == ["aristotle"]


def test_best_picks_verified_then_cheapest_then_fastest(tmp_path: Path) -> None:
    platform = _platform(tmp_path)
    provers = [
        FakeProver("expensive", verified=True, cost_usd=10.0, duration_s=1.0),
        FakeProver("cheap", verified=True, cost_usd=1.0, duration_s=9.0),
        FakeProver("unverified", verified=False, cost_usd=0.01, duration_s=1.0),
        FakeProver("noprice", verified=True, cost_usd=None, duration_s=0.1),
    ]
    result = platform.solve(_task(), provers)

    best = result.best()
    assert best is not None and best.prover == "cheap"  # cheapest *verified*
    # total_cost_usd sums only known costs (unverified counted, noprice skipped).
    assert result.total_cost_usd == pytest.approx(10.0 + 1.0 + 0.01)


def test_best_breaks_cost_ties_by_duration(tmp_path: Path) -> None:
    platform = _platform(tmp_path)
    provers = [
        FakeProver("slow", verified=True, cost_usd=1.0, duration_s=9.0),
        FakeProver("fast", verified=True, cost_usd=1.0, duration_s=1.0),
    ]
    result = platform.solve(_task(), provers)
    best = result.best()
    assert best is not None and best.prover == "fast"


def test_best_is_none_when_nothing_verifies(tmp_path: Path) -> None:
    platform = _platform(tmp_path)
    result = platform.solve(_task(), [FakeProver("a", verified=False)])
    assert result.best() is None
    assert result.verified() == []


def test_solve_rejects_toolchain_mismatch_before_fanning_out(
    tmp_path: Path,
) -> None:
    platform = Platform(toolchain="leanprover/lean4:v9.99.0", runs_dir=tmp_path)
    with pytest.raises(ToolchainMismatch):
        platform.solve(_task(), [FakeProver("agent")])


def test_solve_builds_provers_from_string_specs(tmp_path: Path) -> None:
    """String specs are routed through the factory (not just pre-built instances)."""
    platform = _platform(tmp_path)
    built: list[str] = []

    real_build = platform.build

    def spy(spec: str, overrides=None) -> AutomatedProver:  # type: ignore[no-untyped-def]
        built.append(spec)
        return FakeProver(spec)

    platform.build = spy  # type: ignore[method-assign]
    result = platform.solve(_task(), ["agent", "aristotle"])
    assert built == ["agent", "aristotle"]
    assert {r.prover for r in result.results} == {"agent", "aristotle"}
    platform.build = real_build  # type: ignore[method-assign]


# --- serialization ---------------------------------------------------------


def test_to_dict_round_trips_to_json(tmp_path: Path) -> None:
    platform = _platform(tmp_path)
    provers = [
        FakeProver("agent", verified=True, cost_usd=1.5, duration_s=2.0),
        FakeProver("aristotle", raises=RuntimeError("nope")),
    ]
    result = platform.solve(_task(), provers)

    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["run_id"] == result.run_id
    assert payload["best"] == "agent"
    assert payload["verified"] == ["agent"]
    by_name = {r["prover"]: r for r in payload["results"]}
    assert by_name["agent"]["success"] is True
    assert by_name["agent"]["verification"]["verified"] is True
    assert by_name["agent"]["cost_usd"] == 1.5
    assert by_name["aristotle"]["error"] is not None
    assert by_name["aristotle"]["verification"] is None


def test_proof_result_truncates_long_logs() -> None:
    big = "x" * 10_000
    out = ProofResult(prover="p", verification=None, logs=big).to_dict(log_limit=100)
    assert isinstance(out["logs"], str)
    assert len(out["logs"]) < len(big)
    assert "truncated" in out["logs"]


# --- staging bare files ----------------------------------------------------


def test_stage_files_builds_a_project_from_bare_lean(tmp_path: Path) -> None:
    project = stage_files([FIXTURE / "MILExample.lean"], tmp_path / "staged")
    assert isinstance(project, LeanProject)
    assert (project.root / "MILExample.lean").is_file()
    assert (project.root / "lean-toolchain").is_file()
    assert project.toolchain == DEFAULT_TOOLCHAIN


def test_stage_files_rejects_non_lean(tmp_path: Path) -> None:
    (tmp_path / "foo.txt").write_text("nope")
    with pytest.raises(ValueError, match="Expected a .lean file"):
        stage_files([tmp_path / "foo.txt"], tmp_path / "staged")


# --- Docker integration: real verify + concurrent aggregation --------------


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
def test_solve_multi_prover_with_real_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent solve: stubbed-remote Aristotle (real Docker verify) + a fake."""
    monkeypatch.setattr(
        AristotleProver, "_submit_and_download", _fake_aristotle_remote(solved=True)
    )
    platform = Platform(runs_dir=tmp_path / "runs")
    fake = FakeProver("fake", verified=True, cost_usd=0.5)

    result = platform.solve(_task(), ["aristotle", fake], max_workers=2)

    aristotle = next(r for r in result.results if r.prover == "aristotle")
    report = aristotle.verification
    assert aristotle.success, report and report.compile_log
    assert report is not None and report.verified
    # Both provers verified; aggregation + best() over the real result.
    assert {r.prover for r in result.verified()} == {"aristotle", "fake"}
    best = result.best()
    assert best is not None and best.prover == "fake"  # priced cheapest


@pytest.mark.docker
def test_solve_catches_real_unverified_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sorry left in by the remote is caught by the real verifier, not propagated."""
    monkeypatch.setattr(
        AristotleProver, "_submit_and_download", _fake_aristotle_remote(solved=False)
    )
    platform = Platform(runs_dir=tmp_path / "runs")
    result = platform.solve(_task(), ["aristotle"])
    aristotle = result.results[0]
    assert not aristotle.success
    assert aristotle.verification is not None and not aristotle.verification.sorry_free
