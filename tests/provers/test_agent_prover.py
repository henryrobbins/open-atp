"""AgentProver tests.

* **Fast unit** (no Docker, no creds): parse a captured stream-json fixture into
  token totals + cost, and exercise the ``prove`` diff logic by stubbing the agent
  launch (``_run_agent``) to write a solved file into the workdir.
* **Mocked agent + real Docker verify** (``docker`` marker): a stubbed agent writes a
  real proof and the shared verifier confirms it -- no creds.

The credentialed live path (real CLI on the trivial fixture) lives in the single
parametrized ``test_e2e_provers.py`` suite, alongside every other prover/backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from open_afps.backends.docker import DockerBackend, DockerConfig
from open_afps.core.task import LeanProject, ProofTask
from open_afps.harness import (
    HARNESSES,
    AssetBundle,
    ClaudeCodeHarness,
    Harness,
    bundle_for_config,
    compute_cost_usd,
    resolve_plugin,
    resolve_skill,
)
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_afps.provers.agent_prover import AgentProver, AgentProverConfig

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"
STREAM = Path(__file__).parents[1] / "fixtures" / "agent_streams" / "claude_code.jsonl"

SOLVED_FILE = """\
import Mathlib

theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by
  rw [mul_comm a b, mul_assoc b a c]
"""


def _make_prover(*, reuse: bool = False) -> AgentProver:
    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    config = AgentProverConfig(
        image=DEFAULT_IMAGE, supported_toolchain=DEFAULT_TOOLCHAIN
    )
    # reuse=True shares one backend so prove() runs the agent and the final verify in
    # a single sandbox; reuse=False (the default here) gives generation its own
    # backend so the pure prove()-diff unit tests never touch a live backend.
    agent_backend = (
        backend if reuse else DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    )
    return AgentProver(config, backend, agent_backend)


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
    assert "<<MODEL>>" not in script and "<<PLUGIN_FLAGS>>" not in script
    assert (tmp_path / "agent_prompt.txt").read_text() == "fill the sorrys"
    assert (tmp_path / ".mcp.json").is_file()
    # The default bundle: the vendored lean-proof skill, plus the lean4 plugin
    # mounted under .plugins/ and loaded via a --plugin-dir flag.
    assert (tmp_path / ".claude" / "skills" / "lean-proof" / "SKILL.md").is_file()
    # Upstream skill `tests/` fixtures are dropped at mount time.
    assert not (tmp_path / ".claude" / "skills" / "lean-proof" / "tests").exists()
    plugin_json = tmp_path / ".plugins" / "lean4" / ".claude-plugin" / "plugin.json"
    assert plugin_json.is_file()
    assert "--plugin-dir .plugins/lean4" in script
    assert harness.static_env().get("CLAUDE_CODE_FORK_SUBAGENT") == "1"


def test_skills_resolve_by_name_and_path(tmp_path: Path) -> None:
    # By catalog name (vendored `lean-proof`).
    assert resolve_skill("lean-proof").name == "lean-proof"
    assert resolve_plugin("lean4").name == "lean4"
    # By full path: a skill dir resolves to itself (the dummy probe-skill fixture).
    probe = Path(__file__).parents[1] / "fixtures" / "skills" / "probe-skill"
    assert resolve_skill(str(probe)) == probe.resolve()
    # Unknown names are a clear error, not a silent miss.
    with pytest.raises(ValueError, match="unknown skill"):
        resolve_skill("no-such-skill")
    with pytest.raises(ValueError, match="unknown plugin"):
        resolve_plugin("no-such-plugin")


def test_config_overrides_select_skills_and_plugins() -> None:
    # Explicit lists override the bundle's defaults (names or paths).
    bundle = bundle_for_config(
        AgentProverConfig(
            image=DEFAULT_IMAGE,
            supported_toolchain=DEFAULT_TOOLCHAIN,
            skills=["lean-proof", "lean-setup"],
            plugins=[],
        )
    )
    assert [p.name for p in bundle.skills] == ["lean-proof", "lean-setup"]
    assert bundle.plugins == ()
    # Unset (None) keeps the default bundle's assets.
    default = bundle_for_config(
        AgentProverConfig(image=DEFAULT_IMAGE, supported_toolchain=DEFAULT_TOOLCHAIN)
    )
    assert [p.name for p in default.skills] == ["lean-proof"]
    assert [p.name for p in default.plugins] == ["lean4"]


def test_empty_plugins_mount_nothing_for_claude(tmp_path: Path) -> None:
    bundle = AssetBundle(name="t", skills=(resolve_skill("lean-proof"),), plugins=())
    harness = ClaudeCodeHarness(model="claude-opus-4-8", effort="high", assets=bundle)
    harness.configure_wd(tmp_path, "x")
    assert not (tmp_path / ".plugins").exists()
    assert harness._plugin_flags() == ""
    # No plugin flag is appended: the launch command ends at the model/effort line
    # (the header comment may mention the flag generically; the command must not).
    script = (tmp_path / "agent.sh").read_text()
    assert script.rstrip().endswith("--effort 'high'")
    assert "CLAUDE_CODE_FORK_SUBAGENT" not in harness.static_env()


@pytest.mark.parametrize(
    "harness_name,dest",
    [
        ("codex", ".agents/skills"),
        ("opencode", ".agents/skills"),
        ("vibe", ".vibe/skills"),
    ],
)
def test_non_claude_harnesses_mount_skills_not_plugins(
    tmp_path: Path, harness_name: str, dest: str
) -> None:
    """Skills mount into every harness; plugins are Claude-only and ignored."""
    bundle = AssetBundle(
        name="t",
        skills=(resolve_skill("lean-proof"),),
        plugins=(resolve_plugin("lean4"),),
    )
    harness = HARNESSES[harness_name](
        model="claude-opus-4-8", effort="high", assets=bundle
    )
    harness.configure_wd(tmp_path, "x")
    assert (tmp_path / dest / "lean-proof" / "SKILL.md").is_file()
    assert not (tmp_path / ".plugins").exists()


# --- prove() diff logic (no Docker) ----------------------------------------


def test_prove_reports_files_the_agent_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prove(): stage -> (stubbed) agent writes a solved file -> diff. No Docker."""

    def _fake_run_agent(
        self: AgentProver, workdir: Path, harness: Harness
    ) -> tuple[list[str], str]:
        # The real agent would edit files in place; emulate that.
        (workdir / "MILExample.lean").write_text(SOLVED_FILE)
        return STREAM.read_text().splitlines(), ""

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
    ) -> tuple[list[str], str]:
        return [], ""

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
    ) -> tuple[list[str], str]:
        (workdir / "MILExample.lean").write_text(SOLVED_FILE)
        return STREAM.read_text().splitlines(), ""

    monkeypatch.setattr(AgentProver, "_run_agent", _fake_run_agent)
    prover = _make_prover()

    result = prover.run(ProofTask(LeanProject(FIXTURE)), tmp_path / "wd")

    assert result.success, result.verification and result.verification.compile_log
    assert result.verification is not None and result.verification.verified
    assert result.prover == "agent"
    assert result.cost_usd == pytest.approx(0.4231)


@pytest.mark.docker
def test_run_reuses_one_sandbox_for_generation_and_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reuse=True: generation and the final verify run in ONE live sandbox.

    A stubbed agent writes a real proof and auth is stubbed (no creds/credential
    mounts); the shared verifier then compiles in the *same* session the agent ran
    in -- the no-second-spin-up path -- and confirms it.
    """

    def _fake_run_agent(
        self: AgentProver,
        workdir: Path,
        harness: Harness,
        session: object | None = None,
    ) -> tuple[list[str], str]:
        # The reuse path hands the live session through to the agent run.
        assert session is not None
        (workdir / "MILExample.lean").write_text(SOLVED_FILE)
        return STREAM.read_text().splitlines(), ""

    monkeypatch.setattr(AgentProver, "_run_agent", _fake_run_agent)
    # No real credential resolution/mounts -- keeps the session sandbox dependency-free.
    monkeypatch.setattr(AgentProver, "_auth", lambda self, harness: ({}, []))
    prover = _make_prover(reuse=True)

    result = prover.run(ProofTask(LeanProject(FIXTURE)), tmp_path / "wd")

    assert result.success, result.verification and result.verification.compile_log
    assert result.verification is not None and result.verification.verified
    assert result.cost_usd == pytest.approx(0.4231)
