"""AxProverHarness tests (ax-prover-base via the ``ax-prover prove`` CLI).

Fast unit layer (no Docker, no creds, no API):

* ``configure_wd`` writes ``axprover.yaml`` overriding only the deltas (model +
  provider_config, optional max_iterations) over ax-prover's bundled default.yaml.
* ``_render_config`` maps the open-afps model id to ax-prover's ``provider:model``
  string and ``effort`` to each provider's reasoning knob.
* ``_agent_command`` is the self-discovering launch script (no <<MODEL>> subst).
* ``parse`` sums tokens from the per-target ``ax_output.*.json`` ``-o`` files (the
  stdout stream carries none) and leaves cost None for the prover to derive.
* ``prove`` diffs the workdir after a stubbed run that writes a solved file and a
  synthetic ``-o`` output file, with no Docker.

The live path reuses the ``agent_api`` marker (opt-in, billable, needs an
ANTHROPIC_API_KEY) and runs ax-prover end-to-end in the sandbox.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_afps.backends.docker import DockerBackend, DockerConfig
from open_afps.core.task import LeanProject, ProofTask
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_afps.provers.agent import AgentProver, AgentProverConfig
from open_afps.provers.agent.harness import HARNESSES, AxProverHarness, Harness

FIXTURE = Path(__file__).parent / "fixtures" / "mil_trivial"

SOLVED_FILE = """\
import Mathlib

theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by
  rw [mul_comm a b, mul_assoc b a c]
"""

# ax-prover streams human-readable logs (not a JSON event stream); parse() keeps the
# last non-empty line as result text and reads tokens from the usage files instead.
STREAM_LINES = [
    "Proving: MILExample:mul_comm_assoc",
    "",
    "Running ax-prover...",
    "✓ Proven: MILExample:mul_comm_assoc",
]


def _write_usage(wd: Path, target: str, input_tokens: int, output_tokens: int) -> None:
    """Emulate ax-prover's ``-o`` output: ax_output.<target>.json, a {location:
    {success, ..., input_tokens, output_tokens}} map (parse sums across entries)."""
    (wd / f"ax_output.{target}.json").write_text(
        json.dumps(
            {
                f"MILExample:{target}": {
                    "success": True,
                    "error": None,
                    "summary": "",
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }
            }
        )
    )


def _make_prover() -> AgentProver:
    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    config = AgentProverConfig(
        image=DEFAULT_IMAGE,
        supported_toolchain=DEFAULT_TOOLCHAIN,
        harness="axprover",
        model="claude-opus-4-8",
        effort="high",
    )
    return AgentProver(config, backend)


# --- construction / config -------------------------------------------------


def test_registered_and_from_config_carries_max_iterations() -> None:
    assert HARNESSES["axprover"] is AxProverHarness

    config = AgentProverConfig(
        image=DEFAULT_IMAGE,
        supported_toolchain=DEFAULT_TOOLCHAIN,
        harness="axprover",
        model="claude-opus-4-8",
        effort="high",
        max_iterations=20,
    )
    harness = AxProverHarness.from_config(config)
    assert isinstance(harness, AxProverHarness)
    assert harness.model == "claude-opus-4-8"
    assert harness.effort == "high"
    assert harness.max_iterations == 20


def test_auth_spec_requires_a_provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = AxProverHarness("claude-opus-4-8")
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        harness.auth_spec()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert harness.auth_spec().env == ["ANTHROPIC_API_KEY"]


def test_agent_command_is_self_discovering_script() -> None:
    script = AxProverHarness("claude-opus-4-8")._agent_command()
    # --config must precede the `prove` subcommand; warm-cache + robustness flags.
    assert "ax-prover --config axprover.yaml prove" in script
    assert "--skip-build" in script
    assert "grep -rl" in script and "sorry" in script
    assert "<<MODEL>>" not in script and "<<EFFORT>>" not in script


# --- config rendering ------------------------------------------------------


def test_render_config_anthropic_model_and_effort(tmp_path: Path) -> None:
    harness = AxProverHarness("claude-opus-4-8", effort="high", max_iterations=15)
    harness.configure_wd(tmp_path, "ignored prompt")

    assert (tmp_path / "agent.sh").is_file()
    cfg = json.loads((tmp_path / "axprover.yaml").read_text())  # JSON is valid YAML
    # prover_llm points at a FRESH llm_configs key via interpolation so default.yaml's
    # claude_opus_4_5 config can't deep-merge stale keys (e.g. thinking.budget_tokens)
    # into ours -- see _render_config for the full rationale.
    assert cfg["prover"]["prover_llm"] == "${llm_configs.open_afps}"
    llm = cfg["llm_configs"]["open_afps"]
    assert llm["model"] == "anthropic:claude-opus-4-8"
    assert llm["provider_config"]["effort"] == "high"
    assert llm["provider_config"]["thinking"] == {"type": "adaptive"}
    # Regression: the adaptive-thinking config must not carry budget_tokens, which the
    # API rejects under thinking.type: adaptive ("Extra inputs are not permitted").
    assert "budget_tokens" not in llm["provider_config"]["thinking"]
    assert cfg["prover"]["max_iterations"] == 15
    # parse() looks here for the usage side-channel files.
    assert harness._wd == tmp_path


def test_render_config_provider_prefix_and_knob_per_provider() -> None:
    assert "openai:gpt-5.2" == AxProverHarness("gpt-5.2")._ax_model()
    assert "google_genai:gemini-3-pro" == AxProverHarness("gemini-3-pro")._ax_model()

    openai_cfg = AxProverHarness("gpt-5.2", effort="high")._provider_config()
    assert openai_cfg["reasoning"] == {"effort": "high"}
    google_cfg = AxProverHarness("gemini-3-pro", effort="medium")._provider_config()
    assert google_cfg["thinking_level"] == "medium"


def test_render_config_omits_max_iterations_when_unset(tmp_path: Path) -> None:
    AxProverHarness("claude-opus-4-8").configure_wd(tmp_path, "prompt")
    cfg = json.loads((tmp_path / "axprover.yaml").read_text())
    assert "max_iterations" not in cfg["prover"]


# --- parse: tokens from the usage files, cost left to the prover ------------


def test_parse_sums_tokens_across_usage_files(tmp_path: Path) -> None:
    harness = AxProverHarness("claude-opus-4-8")
    harness.configure_wd(tmp_path, "prompt")
    _write_usage(tmp_path, "A_lean", 1000, 200)
    _write_usage(tmp_path, "B_lean", 500, 50)

    result = harness.parse(STREAM_LINES)
    assert result.input_tokens == 1500
    assert result.output_tokens == 250
    # ax-prover never self-reports USD; the prover derives it from the token table.
    assert result.cost_usd is None
    assert result.result_text == "✓ Proven: MILExample:mul_comm_assoc"


def test_parse_without_usage_files_reports_zero_tokens(tmp_path: Path) -> None:
    harness = AxProverHarness("claude-opus-4-8")
    harness.configure_wd(tmp_path, "prompt")  # no usage files written
    result = harness.parse(STREAM_LINES)
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.cost_usd is None


# --- prove() diff logic (no Docker) ----------------------------------------


def test_prove_reports_changes_and_token_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prove(): stage -> stubbed agent writes a solved file + usage file -> diff."""

    def _fake_run_agent(
        self: AgentProver, workdir: Path, harness: Harness
    ) -> list[str]:
        (workdir / "MILExample.lean").write_text(SOLVED_FILE)
        # The real run writes this; emulate it so parse() finds the tokens.
        assert isinstance(harness, AxProverHarness)
        _write_usage(workdir, "MILExample_lean", 1_000_000, 100_000)
        return STREAM_LINES

    monkeypatch.setattr(AgentProver, "_run_agent", _fake_run_agent)
    prover = _make_prover()
    workdir = tmp_path / "wd"

    output = prover.prove(ProofTask(LeanProject(FIXTURE)), workdir)

    assert (workdir / "MILExample.lean").read_text() == SOLVED_FILE
    assert list(output.completed_files) == ["MILExample.lean"]
    # cost_usd is derived from the token table for claude-opus-4-8 (5/25 per Mtok).
    expected = 1_000_000 * 5.0 / 1e6 + 100_000 * 25.0 / 1e6
    assert output.cost_usd == pytest.approx(expected)
    assert output.metadata["harness"] == "axprover"
    assert output.metadata["model"] == "claude-opus-4-8"
    assert output.metadata["input_tokens"] == 1_000_000


# --- live integration (opt-in) ---------------------------------------------

# Backend dimension: each value carries its own opt-out marker so a run can pick
# one with ``-m 'agent_api and docker'`` / ``-m 'agent_api and modal'`` (mirrors
# test_agent_capabilities.py).
BACKENDS = [
    pytest.param("docker", marks=pytest.mark.docker),
    pytest.param("modal", marks=pytest.mark.modal),
]


def _modal_configured() -> bool:
    import os

    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return True
    return (Path.home() / ".modal.toml").is_file()


def _backend_available(backend: str) -> bool:
    import shutil

    if backend == "docker":
        return shutil.which("docker") is not None
    if backend == "modal":
        return _modal_configured()
    return False


def _make_live_backend(backend: str):
    if backend == "docker":
        return DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    if backend == "modal":
        from open_afps.backends.modal import ModalBackend, ModalConfig

        return ModalBackend(ModalConfig(image=DEFAULT_IMAGE))
    raise AssertionError(backend)


@pytest.mark.agent_api
@pytest.mark.parametrize("backend", BACKENDS)
def test_live_axprover_solves_trivial_theorem(backend: str, tmp_path: Path) -> None:
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set (add it to .env)")
    if not _backend_available(backend):
        pytest.skip(f"backend {backend} not available")

    config = AgentProverConfig(
        image=DEFAULT_IMAGE,
        supported_toolchain=DEFAULT_TOOLCHAIN,
        harness="axprover",
        model="claude-opus-4-8",
        effort="high",
        max_iterations=10,
    )
    prover = AgentProver(config, _make_live_backend(backend))

    result = prover.run(ProofTask(LeanProject(FIXTURE)), tmp_path / "wd")

    assert result.completed_files, "ax-prover returned no changed files"
    assert result.success, result.verification and result.verification.compile_log
    assert result.prover == "agent"
