"""VibeHarness tests (Leanstral via the Mistral Vibe CLI).

Fast unit layer (no Docker, no creds, no API):

* ``configure_wd`` bootstraps a workdir-local VIBE_HOME (config that un-gates the
  builtin ``lean`` agent + the vendored ``lean-devstral`` stand-in).
* ``_agent_command`` renders the chosen agent profile + the ``-p`` run guards.
* ``parse`` pulls cost/tokens from the per-session ``meta.json`` (the vibe-specific
  seam -- the NDJSON stream carries no cost), and the final assistant text from the
  stream.
* ``prove`` diffs the workdir after a stubbed run that writes a solved file and a
  synthetic session log, with no Docker.

The live path reuses the ``agent_api`` marker (opt-in, billable, needs
``MISTRAL_API_KEY``) and runs the non-Labs ``leanstral:devstral`` stand-in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_afps.backends.docker import DockerBackend, DockerConfig
from open_afps.core.task import LeanProject, ProofTask
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_afps.provers.agent import AgentProver, AgentProverConfig
from open_afps.provers.agent.harness import HARNESSES, Harness, VibeHarness

FIXTURE = Path(__file__).parent / "fixtures" / "mil_trivial"

SOLVED_FILE = """\
import Mathlib

theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by
  rw [mul_comm a b, mul_assoc b a c]
"""

# A trimmed vibe `--output streaming` stream: system + user + an assistant message.
STREAM_LINES = [
    json.dumps({"role": "system", "content": "You are Mistral Vibe..."}),
    json.dumps({"role": "user", "content": "Fill the sorrys."}),
    "",
    "not json",
    json.dumps(
        {
            "role": "assistant",
            "content": "Done. Replaced sorry with rw.",
            "tool_calls": None,
            "message_id": "abc",
        }
    ),
]


def _session_stats() -> dict[str, object]:
    return {
        "steps": 3,
        "session_prompt_tokens": 26951,
        "session_completion_tokens": 570,
        "input_price_per_million": 0.4,
        "output_price_per_million": 2.0,
        "session_total_llm_tokens": 27521,
        "session_cost": 0.0119204,
    }


def _write_session_log(log_dir: Path, stats: dict[str, object]) -> None:
    """Emulate a vibe session log under VIBE_HOME/logs/session/<id>/meta.json."""
    sess = log_dir / "session_20260622_000000_deadbeef"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "meta.json").write_text(json.dumps({"stats": stats}))


def _make_prover() -> AgentProver:
    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    config = AgentProverConfig(
        image=DEFAULT_IMAGE,
        supported_toolchain=DEFAULT_TOOLCHAIN,
        harness="vibe",
        agent="lean-devstral",
        model="devstral-medium-latest",
    )
    return AgentProver(config, backend)


# --- construction / config -------------------------------------------------


def test_registered_and_from_config_carries_vibe_knobs() -> None:
    assert HARNESSES["vibe"] is VibeHarness

    config = AgentProverConfig(
        image=DEFAULT_IMAGE,
        supported_toolchain=DEFAULT_TOOLCHAIN,
        harness="vibe",
        agent="lean",
        model="labs-leanstral-2603",
        max_turns=8,
        max_price=0.5,
    )
    harness = VibeHarness.from_config(config)
    assert isinstance(harness, VibeHarness)
    assert harness.agent == "lean"
    assert harness.max_turns == 8
    assert harness.max_price == 0.5


def test_agent_command_renders_agent_and_run_guards() -> None:
    harness = VibeHarness(
        "labs-leanstral-2603", agent="lean", max_turns=8, max_price=0.5
    )
    script = harness._agent_command()
    assert "--agent lean" in script
    assert "<<AGENT>>" not in script and "<<EXTRA>>" not in script
    assert "--max-turns 8" in script
    assert "--max-price 0.5" in script
    assert 'export VIBE_HOME="$PWD/.vibe"' in script


def test_agent_command_omits_unset_guards() -> None:
    harness = VibeHarness("labs-leanstral-2603", agent="lean-devstral")
    script = harness._agent_command()
    assert "--agent lean-devstral" in script
    assert "--max-turns" not in script
    assert "--max-price" not in script


def test_auth_spec_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = VibeHarness("labs-leanstral-2603")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MISTRAL_API_KEY"):
        harness.auth_spec()
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-test")
    assert harness.auth_spec().env == ["MISTRAL_API_KEY"]


# --- configure_wd ----------------------------------------------------------


def test_configure_wd_bootstraps_workdir_local_vibe_home(tmp_path: Path) -> None:
    harness = VibeHarness("devstral-medium-latest", agent="lean-devstral")
    harness.configure_wd(tmp_path, "fill the sorrys")

    assert (tmp_path / "agent.sh").is_file()
    assert (tmp_path / "agent_prompt.txt").read_text() == "fill the sorrys"

    config = (tmp_path / ".vibe" / "config.toml").read_text()
    assert 'installed_agents = ["lean"]' in config
    assert "[session_logging]" in config
    # Ungate mutating tools in `vibe -p` (otherwise `edit` is denied) and wire in the
    # lean-lsp compile-check loop -- both live on the base config so they cover the
    # builtin `lean` agent and the stand-ins alike.
    assert "bypass_tool_permissions = true" in config
    assert "[[mcp_servers]]" in config
    assert 'command = "lean-lsp-mcp"' in config

    standin = tmp_path / ".vibe" / "agents" / "lean-devstral.toml"
    assert standin.is_file()
    assert 'system_prompt_id = "lean"' in standin.read_text()

    # The filling-sorrys skill is staged under VIBE_HOME/skills (vibe's user skills
    # dir, loaded without project-folder trust), matching the other harnesses.
    assert (tmp_path / ".vibe" / "skills" / "filling-sorrys" / "SKILL.md").is_file()

    # parse() looks here for the session log written back from the sandbox.
    assert harness._session_log_dir == tmp_path / ".vibe" / "logs" / "session"


# --- parse: cost from the session log, text from the stream ----------------


def test_parse_reads_cost_and_tokens_from_session_log(tmp_path: Path) -> None:
    harness = VibeHarness("labs-leanstral-2603")
    harness.configure_wd(tmp_path, "prompt")
    _write_session_log(harness._session_log_dir, _session_stats())

    result = harness.parse(STREAM_LINES)
    assert result.cost_usd == pytest.approx(0.0119204)
    assert result.input_tokens == 26951
    assert result.output_tokens == 570
    assert result.result_text == "Done. Replaced sorry with rw."


def test_parse_without_session_log_leaves_cost_none(tmp_path: Path) -> None:
    harness = VibeHarness("labs-leanstral-2603")
    harness.configure_wd(tmp_path, "prompt")  # no log written
    result = harness.parse(STREAM_LINES)
    assert result.cost_usd is None
    assert result.input_tokens == 0


# --- prove() diff logic (no Docker) ----------------------------------------


def test_prove_reports_changes_and_session_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prove(): stage -> stubbed agent writes a solved file + session log -> diff."""

    def _fake_run_agent(
        self: AgentProver, workdir: Path, harness: Harness
    ) -> list[str]:
        (workdir / "MILExample.lean").write_text(SOLVED_FILE)
        # The real vibe run writes this; emulate it so parse() finds the cost.
        assert isinstance(harness, VibeHarness)
        _write_session_log(harness._session_log_dir, _session_stats())
        return STREAM_LINES

    monkeypatch.setattr(AgentProver, "_run_agent", _fake_run_agent)
    prover = _make_prover()
    workdir = tmp_path / "wd"

    output = prover.prove(ProofTask(LeanProject(FIXTURE)), workdir)

    assert (workdir / "MILExample.lean").read_text() == SOLVED_FILE
    assert list(output.completed_files) == ["MILExample.lean"]
    # Cost flows straight from the session log (vibe never self-reports in stdout).
    assert output.cost_usd == pytest.approx(0.0119204)
    assert output.metadata["harness"] == "vibe"
    assert output.metadata["model"] == "devstral-medium-latest"
    assert output.metadata["input_tokens"] == 26951


# --- live integration (opt-in) ---------------------------------------------


@pytest.mark.agent_api
@pytest.mark.docker
def test_live_vibe_solves_trivial_theorem(tmp_path: Path) -> None:
    import os

    if not os.environ.get("MISTRAL_API_KEY"):
        pytest.skip("MISTRAL_API_KEY not set (add it to .env)")

    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    # Stand-in profile: non-Labs model, runnable today. Switch agent="lean"
    # (model labs-leanstral-2603) once Labs is enabled.
    config = AgentProverConfig(
        image=DEFAULT_IMAGE,
        supported_toolchain=DEFAULT_TOOLCHAIN,
        harness="vibe",
        agent="lean-devstral",
        model="devstral-medium-latest",
        max_price=0.5,
    )
    prover = AgentProver(config, backend)

    result = prover.run(ProofTask(LeanProject(FIXTURE)), tmp_path / "wd")

    assert result.completed_files, "vibe returned no changed files"
    assert result.success, result.verification and result.verification.compile_log
    assert result.prover == "agent"
