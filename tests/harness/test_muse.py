"""MuseHarness tests (Meta Muse Code CLI, Meta Model API key).

Fast unit layer (no Docker, no creds, no API):

* ``stage_wd`` writes a workdir-local ``XDG_CONFIG_HOME`` carrying the
  ``settings.json`` that declares the lean-lsp MCP server.
* ``_agent_command`` renders the model/effort and pins both XDG roots + the
  launcher's auto-update off.
* ``agent_auth`` reads the host ``META_MODEL_API_KEY`` and forwards it under the
  name the CLI reads, ``META_API_KEY``.
* ``parse_result`` sums tokens across the main *and* subagent session logs (the
  muse-specific seam -- the JSONL stream carries no usage at all) and takes the
  terminal state + final text from the stream.
* ``prove`` diffs the workdir after a stubbed run that writes a solved file and
  synthetic session logs, and relocates those logs into the run's logs dir.

The live path (the real ``muse`` CLI) lives in the single parametrized
``test_e2e_provers.py`` suite, alongside every other prover.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_atp.harness import (
    _HARNESSES,
    Harness,
    MissingCredentials,
    MuseHarness,
    compute_cost_usd,
)
from open_atp.harness.base import _is_auth_failure
from open_atp.lean import LeanProject, ProofTask
from open_atp.provers.agent_prover import AgentProver
from open_atp.provers.base import ProofResult

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"

SOLVED_FILE = """\
import Mathlib

theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by
  rw [mul_comm a b, mul_assoc b a c]
"""


def _envelope(payload_type: str, payload: dict[str, object]) -> str:
    """One line of ``muse exec --json``: an envelope around a typed payload."""
    return json.dumps(
        {
            "schema_version": 1,
            "stream": {"kind": "session", "id": "sess-1"},
            "record_type": "event",
            "payload_type": payload_type,
            "payload": payload,
        }
    )


# A trimmed `muse exec --json` stdout. The run's single run.terminal.* record is
# where the terminal state and the agent's closing message live; nothing in the
# stream reports tokens or cost.
STREAM_LINES = [
    _envelope("run.lifecycle.started", {"kind": "run_started"}),
    "",
    "muse: workspace trust: trusted source=run-flag",
    _envelope("run.output.delta", {"kind": "run_output_delta", "text": "working"}),
    _envelope(
        "run.terminal.completed",
        {
            "kind": "run_terminal",
            "terminal": "completed",
            "text": "Replaced the sorry with rw.",
            "reason": None,
        },
    ),
]


def _usage(input_tokens: int, output_tokens: int, cached: int) -> str:
    """A ``goal_usage_attribution`` line as muse writes it into a session log."""
    return json.dumps(
        {
            "payload_type": "runtime.session",
            "payload": {
                "kind": "run",
                "event": {
                    "kind": "goal_usage_attribution",
                    "record": {
                        "quantity": {
                            "unit": "tokens",
                            "reported": True,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "cached_tokens": cached,
                            "reasoning_tokens": 0,
                        }
                    },
                },
            },
        }
    )


# An unreported step: a placeholder whose usage the provider never returned.
UNREPORTED = json.dumps(
    {
        "payload_type": "runtime.session",
        "payload": {
            "event": {
                "kind": "goal_usage_attribution",
                "record": {"quantity": {"reported": False, "input_tokens": 999}},
            }
        },
    }
)

MAIN_USAGE = [_usage(16337, 227, 0), UNREPORTED, _usage(16598, 39, 16305)]
SUBAGENT_USAGE = [_usage(1789, 807, 0)]

# Totals the harness must reach by summing main + subagent.
TOTAL_IN = 16337 + 16598 + 1789
TOTAL_OUT = 227 + 39 + 807
TOTAL_CACHED = 16305


def _write_session_logs(wd: Path) -> None:
    """Emulate the sandbox writing the main + one subagent session log."""
    main = wd / MuseHarness.DATA_DIR / "muse" / "sessions" / "2026" / "08" / "05" / "s1"
    sub = main / "subagent" / "sub-1"
    sub.mkdir(parents=True, exist_ok=True)
    (main / "session.jsonl").write_text("\n".join(MAIN_USAGE))
    (sub / "session.jsonl").write_text("\n".join(SUBAGENT_USAGE))


@pytest.fixture
def prover(fake_session_backend: object) -> AgentProver:
    # The in-process fake session keeps the diff unit test (which stubs _run_agent)
    # off a live backend while _generate opens its session and verifies in it.
    return AgentProver(harness=MuseHarness(), backend=fake_session_backend)


# --- construction / config -------------------------------------------------


def test_registered_and_construction_carries_muse_knobs() -> None:
    assert _HARNESSES["muse"] is MuseHarness

    harness = MuseHarness()
    # The standard prover defaults to the (heavily discounted) contributor tier,
    # whose price is the grant to train on the run's prompts and completions.
    assert harness.model == "muse-spark-1.2-contributor"
    assert harness.effort == "high"

    standard = MuseHarness(model="muse-spark-1.2", effort="xhigh")
    assert standard.model == "muse-spark-1.2"
    assert standard.effort == "xhigh"


def test_agent_command_renders_model_and_effort() -> None:
    script = MuseHarness(model="muse-spark-1.2", effort="ultra")._agent_command()
    assert "--model 'muse-spark-1.2'" in script
    assert "--reasoning-effort 'ultra'" in script
    assert "<<MODEL>>" not in script
    assert "<<EFFORT>>" not in script


def test_agent_command_pins_xdg_roots_and_disables_auto_update() -> None:
    script = MuseHarness()._agent_command()
    # Both XDG roots are workdir-local: config carries settings.json (lean-lsp),
    # data carries the session logs parse_result reads tokens from.
    assert 'export XDG_CONFIG_HOME="$PWD/.muse-config"' in script
    assert 'export XDG_DATA_HOME="$PWD/.muse-data"' in script
    # The launcher must not fork a background self-update in an ephemeral sandbox.
    assert "export MUSE_NO_AUTO_UPDATE=1" in script
    # Headless, auto-approving, and hermetic.
    assert "muse exec" in script
    assert "--json" in script
    assert "--yolo" in script
    assert "--user-input-auto-resolve" in script
    assert "--no-foreign-personal-context" in script


# --- auth ------------------------------------------------------------------


def test_auth_reads_host_env_and_forwards_cli_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("META_MODEL_API_KEY", "meta-key-123")
    auth = MuseHarness().agent_auth()
    # Read under the project-wide name, forwarded under the one muse itself reads.
    assert auth.env == {"META_API_KEY": "meta-key-123"}
    assert auth.mounts == []

    status = MuseHarness().auth_status()
    assert status.source == "META_MODEL_API_KEY"
    assert status.present


def test_auth_explicit_key_wins_and_absence_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("META_MODEL_API_KEY", "from-env")
    explicit = MuseHarness(api_key="explicit").agent_auth()
    assert explicit.env["META_API_KEY"] == "explicit"

    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    assert not MuseHarness().auth_status().present
    with pytest.raises(MissingCredentials, match="META_MODEL_API_KEY"):
        MuseHarness().agent_auth()


def test_rejected_key_phrasing_is_recognised() -> None:
    # Verbatim from a `muse exec` run against a bad key; the prover keys the
    # "credential rejected" error off this, so the phrasing must be matched.
    assert _is_auth_failure(
        "your API key from META_API_KEY was rejected — update or unset it"
    )


# --- stage -----------------------------------------------------------------


def test_stage_writes_settings_with_lean_lsp_mcp(tmp_path: Path) -> None:
    harness = MuseHarness()
    wd = tmp_path / "wd"
    wd.mkdir()
    harness.stage_wd(wd)
    harness.write_prompt(wd, "fill the sorrys")

    assert (wd / "agent.sh").is_file()
    assert (wd / "agent_prompt.txt").read_text() == "fill the sorrys"

    settings = json.loads((wd / ".muse-config" / "muse" / "settings.json").read_text())
    # muse refuses to start unless settings.json declares the schema version.
    assert settings["schema_version"] == 1
    server = settings["mcp_servers"]["lean-lsp"]
    assert server["enabled"] is True
    assert server["transport"] == "stdio"
    assert server["command"] == "lean-lsp-mcp"

    # Skills are staged by the prover (stage_skills) into muse's project scope,
    # which it discovers under the workspace root once --yolo trusts it.
    assert harness.skills_dest == ".agents/skills"


# --- parse: tokens from the session logs, terminal from the stream ---------


def test_parse_sums_tokens_across_main_and_subagent_logs(tmp_path: Path) -> None:
    wd = tmp_path / "wd"
    wd.mkdir()
    _write_session_logs(wd)

    result = MuseHarness().parse_result(STREAM_LINES, wd)
    # Subagents bill to the same account and log separately, so their tokens count.
    assert result.input_tokens == TOTAL_IN
    assert result.output_tokens == TOTAL_OUT
    # The cache-hit read is a subset of the input total, priced at the cached rate.
    assert result.cached_input_tokens == TOTAL_CACHED
    assert result.stop_reason == "completed"
    assert result.result_text == "Replaced the sorry with rw."
    # Muse reports no USD of its own; the prover estimates it from these tokens.
    assert result.cost_usd is None


def test_parse_without_session_logs_leaves_tokens_zero(tmp_path: Path) -> None:
    wd = tmp_path / "wd"
    wd.mkdir()
    result = MuseHarness().parse_result(STREAM_LINES, wd)  # no session logs written
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.cost_usd is None
    # The stream still yields the terminal state and the agent's closing message.
    assert result.stop_reason == "completed"
    assert result.result_text == "Replaced the sorry with rw."


def test_parse_reports_failed_terminal(tmp_path: Path) -> None:
    wd = tmp_path / "wd"
    wd.mkdir()
    failed = _envelope(
        "run.terminal.failed",
        {
            "kind": "run_terminal",
            "terminal": "failed",
            "text": "",
            "reason": "required MCP server `lean-lsp` failed during startup",
        },
    )
    result = MuseHarness().parse_result([failed], wd)
    assert result.stop_reason == "failed"
    # An empty closing message is reported as absent rather than "".
    assert result.result_text is None


# --- prove() diff logic (no Docker) ----------------------------------------


def test_generate_reports_changes_and_relocates_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prover: AgentProver
) -> None:
    """_generate(): stage -> stubbed agent writes a solved file + logs -> diff.

    Drives the generation half directly (no Docker verify) and asserts the muse
    session logs are relocated into the run's logs dir.
    """

    def _fake_run_agent(
        self: AgentProver,
        workdir: Path,
        harness: Harness,
        stdout_path: Path,
        session: object | None = None,
        timeout_s: int | None = None,
    ) -> tuple[list[str], str, bool]:
        (workdir / "MILExample.lean").write_text(SOLVED_FILE)
        # The real muse run writes these under the workdir-local XDG_DATA_HOME;
        # emulate it so parse_result() finds the tokens.
        _write_session_logs(workdir)
        return STREAM_LINES, "", False

    monkeypatch.setattr(AgentProver, "_run_agent", _fake_run_agent)
    wd = tmp_path / "wd"
    logs_dir = tmp_path / "logs"
    wd.mkdir()
    logs_dir.mkdir()
    result = ProofResult(prover="agent", verification=None, output_dir=tmp_path)

    prover._generate(ProofTask(LeanProject(FIXTURE)), wd, logs_dir, result)

    assert (wd / "MILExample.lean").read_text() == SOLVED_FILE
    assert list(result.completed_files) == ["MILExample.lean"]
    # The session logs were relocated out of the workdir into the run's logs dir, so
    # the downloaded wd stays the proof project.
    assert not (wd / MuseHarness.DATA_DIR / "muse" / "sessions").exists()
    assert (logs_dir / "muse-session").is_dir()
    assert len(list((logs_dir / "muse-session").rglob("session.jsonl"))) == 2
    # Tokens flow from the session logs; muse never self-reports USD, so the prover
    # estimates the cost from those tokens at the model's rate.
    assert result.cost_usd == compute_cost_usd(
        "muse-spark-1.2-contributor", TOTAL_IN, TOTAL_OUT, TOTAL_CACHED
    )
    assert result.metadata["harness"] == "muse"
    assert result.metadata["model"] == "muse-spark-1.2-contributor"
    assert result.metadata["input_tokens"] == TOTAL_IN
    assert result.metadata["cached_input_tokens"] == TOTAL_CACHED
    assert result.metadata["output_tokens"] == TOTAL_OUT
    assert result.metadata["stop_reason"] == "completed"
