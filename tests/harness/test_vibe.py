"""VibeHarness tests (Leanstral via the Mistral Vibe CLI).

Fast unit layer (no Docker, no creds, no API):

* ``configure_wd`` bootstraps a workdir-local VIBE_HOME (config that un-gates the
  builtin ``lean`` agent + the vendored ``lean-standin`` stand-in, with the model
  templated in).
* ``_agent_command`` renders the chosen agent profile + the ``-p`` run guards.
* ``parse`` pulls cost/tokens from the per-session ``meta.json`` (the vibe-specific
  seam -- the NDJSON stream carries no cost), and the final assistant text from the
  stream.
* ``prove`` diffs the workdir after a stubbed run that writes a solved file and a
  synthetic session log, with no Docker.

The live path (the non-Labs ``vibe`` stand-in on the real Mistral Vibe CLI) lives in
the single parametrized ``test_e2e_provers.py`` suite, alongside every other prover.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_atp.backends.docker import DockerBackend, DockerConfig
from open_atp.harness import HARNESSES, Harness, VibeHarness
from open_atp.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_atp.lean import LeanProject, ProofTask
from open_atp.provers.agent_prover import AgentProver, AgentProverConfig
from open_atp.verify import ProofResult

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"

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
        agent="lean-standin",
        model="magistral-medium-latest",
    )
    # Distinct agent backend so prove() takes the non-reuse path (no live session) --
    # the diff unit test stubs _run_agent and must not touch a real backend.
    agent_backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    return AgentProver(config, backend, agent_backend)


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
    harness = VibeHarness("magistral-medium-latest", agent="lean-standin")
    script = harness._agent_command()
    assert "--agent lean-standin" in script
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
    harness = VibeHarness("magistral-medium-latest", agent="lean-standin")
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
    # Raised MCP tool timeout: cold Mathlib load exceeds vibe's 60s default (mirrors
    # the opencode fix). Vibe's field is in seconds.
    assert "tool_timeout_sec = 180" in config

    standin = tmp_path / ".vibe" / "agents" / "lean-standin.toml"
    assert standin.is_file()
    standin_text = standin.read_text()
    assert 'system_prompt_id = "lean"' in standin_text
    # Vibe has no --model flag, so the model is templated into the profile.
    assert 'name = "magistral-medium-latest"' in standin_text
    assert "<<MODEL>>" not in standin_text

    # The default skill (lean-proof) is staged under VIBE_HOME/skills (vibe's user
    # skills dir, loaded without project-folder trust), matching the other harnesses.
    assert (tmp_path / ".vibe" / "skills" / "lean-proof" / "SKILL.md").is_file()

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


def test_generate_reports_changes_and_session_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_generate(): stage -> stubbed agent writes a solved file + session log -> diff.

    Drives the generation half directly (no Docker verify) and asserts the vibe
    session log is relocated into the logs dir.
    """

    def _fake_run_agent(
        self: AgentProver,
        workdir: Path,
        harness: Harness,
        stdout_path: Path,
        session: object | None = None,
    ) -> tuple[list[str], str]:
        (workdir / "MILExample.lean").write_text(SOLVED_FILE)
        # The real vibe run writes this; emulate it so parse() finds the cost.
        assert isinstance(harness, VibeHarness)
        _write_session_log(harness._session_log_dir, _session_stats())
        return STREAM_LINES, ""

    monkeypatch.setattr(AgentProver, "_run_agent", _fake_run_agent)
    prover = _make_prover()
    wd = tmp_path / "wd"
    logs_dir = tmp_path / "logs"
    wd.mkdir()
    logs_dir.mkdir()
    result = ProofResult(prover="agent", verification=None, output_dir=tmp_path)

    prover._generate(ProofTask(LeanProject(FIXTURE)), wd, logs_dir, result)

    assert (wd / "MILExample.lean").read_text() == SOLVED_FILE
    assert list(result.completed_files) == ["MILExample.lean"]
    # The session log was relocated out of the workdir into the run's logs dir, so
    # the wd stays the proof project and the logs dir carries the record.
    assert not (wd / ".vibe" / "logs").exists()
    assert (logs_dir / "vibe-session").is_dir()
    assert list((logs_dir / "vibe-session").rglob("meta.json"))
    # Cost flows straight from the session log (vibe never self-reports in stdout).
    assert result.cost_usd == pytest.approx(0.0119204)
    assert result.metadata["harness"] == "vibe"
    assert result.metadata["model"] == "magistral-medium-latest"
    assert result.metadata["input_tokens"] == 26951
