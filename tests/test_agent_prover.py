"""AgentProver tests.

Two layers, mirroring the Aristotle tests:

* **Fast unit** (no Docker, no creds): parse a captured stream-json fixture into
  token totals + cost, and exercise the ``prove`` diff logic by stubbing the agent
  launch (``_run_agent``) to write a solved file into the workdir.
* **Credentialed integration** (``agent_api`` marker, opt-in): run the real Claude
  Code CLI on the trivial fixture and confirm the shared Docker verifier accepts the
  result. Excluded by default via ``addopts``; needs ``CLAUDE_CODE_OAUTH_TOKEN``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from open_afps.backends.docker import DockerBackend, DockerConfig
from open_afps.core.task import LeanProject, ProofTask
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_afps.provers.agent import AgentProver, AgentProverConfig
from open_afps.provers.agent.cost import compute_cost_usd
from open_afps.provers.agent.harness import (
    HARNESSES,
    ClaudeCodeHarness,
    Harness,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mil_trivial"
STREAM = Path(__file__).parent / "fixtures" / "agent_streams" / "claude_code.jsonl"

SOLVED_FILE = """\
import Mathlib

theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by
  rw [mul_comm a b, mul_assoc b a c]
"""


def _make_prover() -> AgentProver:
    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    config = AgentProverConfig(
        image=DEFAULT_IMAGE, supported_toolchain=DEFAULT_TOOLCHAIN
    )
    return AgentProver(config, backend)


# --- stream parsing + cost -------------------------------------------------


def test_claude_code_parse_lines_tokens_and_cost() -> None:
    harness = ClaudeCodeHarness(model="claude-opus-4-8", effort="high")
    result = harness.parse(STREAM.read_text().splitlines())

    assert result.input_tokens == 18432
    assert result.output_tokens == 2096
    assert result.stop_reason == "end_turn"
    # Claude Code self-reports USD; we use it verbatim.
    assert result.cost_usd == pytest.approx(0.4231)


def test_parse_ignores_blank_and_malformed_lines() -> None:
    harness = ClaudeCodeHarness(model="claude-opus-4-8")
    lines = ["", "   ", "not json", '{"type":"system"}']
    result = harness.parse(lines)
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.cost_usd is None


def test_compute_cost_usd_known_and_unknown_model() -> None:
    # claude-opus-4-8 is (5.0, 25.0) per Mtok.
    cost = compute_cost_usd("claude-opus-4-8", 1_000_000, 1_000_000)
    assert cost == pytest.approx(30.0)
    assert compute_cost_usd("no-such-model", 1000, 1000) is None


def test_codex_cost_falls_back_to_token_table() -> None:
    """Codex reports no USD, so prove() must estimate from token totals."""
    harness = HARNESSES["codex"](model="gpt-5.4", effort="high")
    lines = [
        '{"type":"turn.completed","usage":{"input_tokens":2000000,'
        '"output_tokens":1000000}}',
    ]
    result = harness.parse(lines)
    assert result.cost_usd is None  # codex never self-reports
    # gpt-5.4 is (2.5, 15.0): 2M*2.5 + 1M*15 = 5 + 15 = 20.
    estimated = compute_cost_usd("gpt-5.4", result.input_tokens, result.output_tokens)
    assert estimated == pytest.approx(20.0)


# --- configure_wd ----------------------------------------------------------


def test_claude_code_configure_wd_writes_assets(tmp_path: Path) -> None:
    harness = ClaudeCodeHarness(model="claude-opus-4-8", effort="high")
    harness.configure_wd(tmp_path, "fill the sorrys")

    assert (tmp_path / "agent.sh").is_file()
    # Model/effort templated into the launch script.
    script = (tmp_path / "agent.sh").read_text()
    assert "claude-opus-4-8" in script and "high" in script
    assert "<<MODEL>>" not in script
    assert (tmp_path / "agent_prompt.txt").read_text() == "fill the sorrys"
    assert (tmp_path / ".mcp.json").is_file()
    assert (tmp_path / ".claude" / "skills" / "filling-sorrys" / "SKILL.md").is_file()


# --- prove() diff logic (no Docker) ----------------------------------------


def test_prove_reports_files_the_agent_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prove(): stage -> (stubbed) agent writes a solved file -> diff. No Docker."""

    def _fake_run_agent(
        self: AgentProver, workdir: Path, harness: Harness
    ) -> list[str]:
        # The real agent would edit files in place; emulate that.
        (workdir / "MILExample.lean").write_text(SOLVED_FILE)
        return STREAM.read_text().splitlines()

    monkeypatch.setattr(AgentProver, "_run_agent", _fake_run_agent)
    # No CLAUDE_CODE_OAUTH_TOKEN needed: _run_agent (which calls auth) is stubbed.
    prover = _make_prover()
    workdir = tmp_path / "wd"

    output = prover.prove(ProofTask(LeanProject(FIXTURE)), workdir)

    # The solved file landed in the workdir and is the only reported change.
    assert (workdir / "MILExample.lean").read_text() == SOLVED_FILE
    assert list(output.completed_files) == ["MILExample.lean"]
    assert "rw [mul_comm" in output.completed_files["MILExample.lean"]
    # Cost + token metadata flowed through from the parsed stream.
    assert output.cost_usd == pytest.approx(0.4231)
    assert output.metadata["harness"] == "claude_code"
    assert output.metadata["model"] == "claude-opus-4-8"
    assert output.metadata["input_tokens"] == 18432


def test_prove_reports_no_changes_when_agent_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _noop_run_agent(
        self: AgentProver, workdir: Path, harness: Harness
    ) -> list[str]:
        return []

    monkeypatch.setattr(AgentProver, "_run_agent", _noop_run_agent)
    prover = _make_prover()

    output = prover.prove(ProofTask(LeanProject(FIXTURE)), tmp_path / "wd")
    assert output.completed_files == {}
    # No stream -> zero tokens -> estimated $0.00 for the known model.
    assert output.cost_usd == pytest.approx(0.0)


# --- mocked agent + real Docker verify --------------------------------------


@pytest.mark.docker
def test_run_end_to_end_verifies_mocked_agent_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full run(): a mocked agent writes a real proof; the Docker verifier confirms.

    Mocked agent, real local check -- the path every prover shares, with no creds.
    """

    def _fake_run_agent(
        self: AgentProver, workdir: Path, harness: Harness
    ) -> list[str]:
        (workdir / "MILExample.lean").write_text(SOLVED_FILE)
        return STREAM.read_text().splitlines()

    monkeypatch.setattr(AgentProver, "_run_agent", _fake_run_agent)
    prover = _make_prover()

    result = prover.run(ProofTask(LeanProject(FIXTURE)), tmp_path / "wd")

    assert result.success, result.verification and result.verification.compile_log
    assert result.verification is not None and result.verification.verified
    assert result.prover == "agent"
    assert result.cost_usd == pytest.approx(0.4231)


# --- credentialed integration (opt-in) -------------------------------------


@pytest.mark.agent_api
@pytest.mark.docker
def test_live_agent_solves_trivial_theorem(tmp_path: Path) -> None:
    import os

    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        pytest.skip("CLAUDE_CODE_OAUTH_TOKEN not set (add it to .env)")

    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    config = AgentProverConfig(
        image=DEFAULT_IMAGE,
        supported_toolchain=DEFAULT_TOOLCHAIN,
        harness="claude_code",
        model="claude-opus-4-8",
        effort="high",
    )
    prover = AgentProver(config, backend)

    result = prover.run(ProofTask(LeanProject(FIXTURE)), tmp_path / "wd")

    assert result.completed_files, "agent returned no changed files"
    assert result.success, result.verification and result.verification.compile_log
    assert result.prover == "agent"
