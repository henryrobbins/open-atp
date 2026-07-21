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

import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from open_atp.backends.base import (
    TIMEOUT_EXIT_CODE,
    CommandHandle,
    CommandResult,
    CommandTimeout,
    ComputeSession,
)
from open_atp.backends.docker import DockerBackend
from open_atp.harness import (
    _HARNESSES,
    ClaudeCodeHarness,
    CodexHarness,
    Harness,
    MissingCredentials,
    compute_cost_usd,
)
from open_atp.harness._catalog import resolve_plugin, resolve_skill
from open_atp.harness._numina import NuminaHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.lean import LeanProject, ProofTask
from open_atp.provers.agent_prover import AgentProver
from open_atp.provers.base import ProofResult, ProofStatus
from open_atp.verify import VerificationReport

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"
STREAM = Path(__file__).parents[1] / "fixtures" / "agent_streams" / "claude_code.jsonl"

SOLVED_FILE = """\
import Mathlib

theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by
  rw [mul_comm a b, mul_assoc b a c]
"""


@pytest.fixture
def make_prover(fake_session_backend: object) -> object:
    """Build an :class:`AgentProver`. ``real=True`` uses a live Docker backend (the
    ``docker``-marked tests); otherwise the in-process fake session keeps the
    diff unit tests off Docker."""

    def _make(*, real: bool = False) -> AgentProver:
        backend = DockerBackend(image=DEFAULT_IMAGE) if real else fake_session_backend
        return AgentProver(backend=backend)

    return _make


# --- stream parsing + cost -------------------------------------------------


def test_claude_code_parse_lines_tokens_and_cost(tmp_path: Path) -> None:
    harness = ClaudeCodeHarness(model="claude-opus-4-8", effort="high")
    result = harness.parse_result(STREAM.read_text().splitlines(), tmp_path)

    assert result.input_tokens == 18432
    assert result.output_tokens == 2096
    assert result.stop_reason == "end_turn"
    # Claude Code self-reports USD; we use it verbatim.
    assert result.cost_usd == pytest.approx(0.4231)


def test_parse_ignores_blank_and_malformed_lines(tmp_path: Path) -> None:
    harness = ClaudeCodeHarness(model="claude-opus-4-8")
    lines = ["", "   ", "not json", '{"type":"system"}']
    result = harness.parse_result(lines, tmp_path)
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.cost_usd is None


def test_compute_cost_usd_known_and_unknown_model() -> None:
    # claude-opus-4-8 is (5.0, 25.0) per Mtok.
    cost = compute_cost_usd("claude-opus-4-8", 1_000_000, 1_000_000)
    assert cost == pytest.approx(30.0)
    assert compute_cost_usd("no-such-model", 1000, 1000) is None


def test_codex_cost_falls_back_to_token_table(tmp_path: Path) -> None:
    """Codex reports no USD, so prove() must estimate from token totals."""
    harness = _HARNESSES["codex"](model="gpt-5.4", effort="high")
    lines = [
        '{"type":"turn.completed","usage":{"input_tokens":2000000,'
        '"output_tokens":1000000}}',
    ]
    result = harness.parse_result(lines, tmp_path)
    assert result.cost_usd is None  # codex never self-reports
    # gpt-5.4 is (2.5, 15.0): 2M*2.5 + 1M*15 = 5 + 15 = 20.
    estimated = compute_cost_usd("gpt-5.4", result.input_tokens, result.output_tokens)
    assert estimated == pytest.approx(20.0)


# --- stage / write_prompt --------------------------------------------------


def test_claude_code_stage_writes_assets(tmp_path: Path) -> None:
    """stage_wd() writes the harness/bundle assets and (Claude-only) plugins -- but NOT
    the skills list, which the prover stages via stage_skills()."""
    harness = ClaudeCodeHarness(model="claude-opus-4-8", effort="high")
    harness.stage_wd(tmp_path)
    harness.write_prompt(tmp_path, "fill the sorrys")

    assert (tmp_path / "agent.sh").is_file()
    # Model/effort templated into the launch script.
    script = (tmp_path / "agent.sh").read_text()
    assert "claude-opus-4-8" in script and "high" in script
    assert "<<MODEL>>" not in script and "<<PLUGIN_FLAGS>>" not in script
    assert (tmp_path / "agent_prompt.txt").read_text() == "fill the sorrys"
    assert (tmp_path / ".mcp.json").is_file()
    # The default lean4 plugin (from ClaudeCodeHarness.plugins) is mounted under
    # .plugins/ and loaded via a --plugin-dir flag.
    plugin_json = tmp_path / ".plugins" / "lean4" / ".claude-plugin" / "plugin.json"
    assert plugin_json.is_file()
    assert "--plugin-dir .plugins/lean4" in script
    assert harness._static_env().get("CLAUDE_CODE_FORK_SUBAGENT") == "1"
    # stage_wd() does not mount the skills list -- that is the prover's job.
    assert not (tmp_path / ".claude" / "skills").exists()


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


@pytest.mark.parametrize(
    "harness_name,dest,extra",
    [
        ("claude_code", ".claude/skills", {}),
        ("codex", ".agents/skills", {}),
        ("opencode", ".agents/skills", {"provider": "anthropic"}),
        ("vibe", ".vibe/skills", {}),
    ],
)
def test_stage_skills_copies_into_harness_location(
    tmp_path: Path, harness_name: str, dest: str, extra: dict[str, str]
) -> None:
    """The prover-resolved skills list lands in each harness's skill location."""
    harness = _HARNESSES[harness_name](model="claude-opus-4-8", effort="high", **extra)
    harness.stage_skills(tmp_path, [resolve_skill("lean-proof")])
    assert (tmp_path / dest / "lean-proof" / "SKILL.md").is_file()
    # Upstream skill `tests/` fixtures are dropped at mount time.
    assert not (tmp_path / dest / "lean-proof" / "tests").exists()


def test_axprover_base_ignores_skills(tmp_path: Path) -> None:
    """ax-prover ships its own prompts and consumes no skills (skills_dest is None)."""
    harness = _HARNESSES["axproverbase"](model="claude-opus-4-8")
    assert harness.skills_dest is None
    harness.stage_skills(tmp_path, [resolve_skill("lean-proof")])  # no-op, no error
    assert not list(tmp_path.iterdir())


def test_empty_plugins_mount_nothing_for_claude(tmp_path: Path) -> None:
    harness = ClaudeCodeHarness(model="claude-opus-4-8", effort="high", plugins=[])
    harness.stage_wd(tmp_path)
    assert not (tmp_path / ".plugins").exists()
    assert harness._plugin_flags() == ""
    # No plugin flag is appended: the launch command ends at the model/effort line
    # (the header comment may mention the flag generically; the command must not).
    script = (tmp_path / "agent.sh").read_text()
    assert script.rstrip().endswith("--effort 'high'")
    assert "CLAUDE_CODE_FORK_SUBAGENT" not in harness._static_env()


def test_claude_agent_auth_resolves_oauth_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Missing on the host and not passed explicitly -> hard failure.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(MissingCredentials, match="CLAUDE_CODE_OAUTH_TOKEN"):
        ClaudeCodeHarness(plugins=[]).agent_auth()
    # Host env var is forwarded with its value, alongside the static IS_SANDBOX.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-host")
    env = ClaudeCodeHarness(plugins=[]).agent_auth().env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-host"
    assert env["IS_SANDBOX"] == "1"
    # An explicit token wins over (and does not need) the host env var.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    explicit = ClaudeCodeHarness(plugins=[], oauth_token="tok-explicit").agent_auth()
    assert explicit.env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-explicit"


def test_numina_helper_env_forwarded_only_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.delenv("HELPER_KEY", raising=False)
    harness = NuminaHarness(helper_env_keys=("HELPER_KEY",))
    assert "HELPER_KEY" not in harness.agent_auth().env  # absent -> skipped, no raise
    monkeypatch.setenv("HELPER_KEY", "hk")
    assert harness.agent_auth().env["HELPER_KEY"] == "hk"


def test_numina_literal_env_wins_over_helper_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("HELPER_KEY", "from-host")
    harness = NuminaHarness(
        helper_env_keys=("HELPER_KEY",), env={"HELPER_KEY": "literal"}
    )
    assert harness.agent_auth().env["HELPER_KEY"] == "literal"


# --- _generate() diff logic (no Docker) ------------------------------------
#
# These exercise the generation half directly: the public ``prove`` always runs the
# shared verifier (Docker), so the no-Docker unit tests call ``_generate`` and assert
# on the filled :class:`ProofResult` instead.


def _run_generate(prover: AgentProver, tmp_path: Path) -> tuple[ProofResult, Path]:
    wd = tmp_path / "wd"
    logs_dir = tmp_path / "logs"
    wd.mkdir()
    logs_dir.mkdir()
    result = ProofResult(prover="agent", verification=None, output_dir=tmp_path)
    prover._generate(ProofTask(LeanProject(FIXTURE)), wd, logs_dir, result)
    return result, wd


def test_generate_reports_files_the_agent_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_prover: object
) -> None:
    """_generate(): stage -> (stubbed) agent writes a solved file -> diff. No Docker."""

    def _fake_run_agent(
        self: AgentProver,
        workdir: Path,
        harness: Harness,
        stdout_path: Path,
        session: object | None = None,
        timeout_s: int | None = None,
    ) -> tuple[list[str], str, bool]:
        # The real agent would edit files in place; emulate that.
        (workdir / "MILExample.lean").write_text(SOLVED_FILE)
        return STREAM.read_text().splitlines(), "", False

    monkeypatch.setattr(AgentProver, "_run_agent", _fake_run_agent)
    # No creds needed: the agent run is stubbed and the in-session verify is a no-op.
    prover = make_prover()

    result, wd = _run_generate(prover, tmp_path)

    # The prover staged its default skill (lean-proof) into the claude_code location.
    assert (wd / ".claude" / "skills" / "lean-proof" / "SKILL.md").is_file()
    # The solved file landed in the workdir and is the only reported change.
    assert (wd / "MILExample.lean").read_text() == SOLVED_FILE
    assert list(result.completed_files) == ["MILExample.lean"]
    assert "rw [mul_comm" in result.completed_files["MILExample.lean"]
    # Cost + token metadata flowed through from the parsed stream.
    assert result.cost_usd == pytest.approx(0.4231)
    assert result.metadata["harness"] == "claude_code"
    assert result.metadata["model"] == "claude-opus-4-8"
    assert result.metadata["input_tokens"] == 18432


def test_generate_reports_no_changes_when_agent_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_prover: object
) -> None:
    def _noop_run_agent(
        self: AgentProver,
        workdir: Path,
        harness: Harness,
        stdout_path: Path,
        session: object | None = None,
        timeout_s: int | None = None,
    ) -> tuple[list[str], str, bool]:
        return [], "", False

    monkeypatch.setattr(AgentProver, "_run_agent", _noop_run_agent)
    prover = make_prover()

    result, _ = _run_generate(prover, tmp_path)
    assert result.completed_files == {}
    # No stream -> zero tokens -> estimated $0.00 for the known model.
    assert result.cost_usd == pytest.approx(0.0)


# --- generation timeout + credential preflight (real classes, no Docker) -----


class _ExitCodeHandle(CommandHandle):
    """A finished command reporting a fixed exit code and no output.

    Models the real backend handles: a command that exited with the
    coreutils-``timeout`` code makes ``wait`` raise :class:`CommandTimeout`.
    """

    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code

    def stream(self) -> Iterator[str]:
        return iter(())

    def wait(self) -> CommandResult:
        result = CommandResult(
            exit_code=self._exit_code, stdout="", stderr="", duration_s=0.0
        )
        if self._exit_code == TIMEOUT_EXIT_CODE:
            raise CommandTimeout("command exceeded its budget", result=result)
        return result


class _ExitCodeSession(ComputeSession):
    """A real session whose first exec (the agent) reports ``agent_exit``; rest 0.

    Models a backend that caps the agent command: the generation exec comes back with
    a coreutils-``timeout`` exit code (raising :class:`CommandTimeout`), then the
    in-session verify exec runs normally (empty compile log -> the candidate does not
    verify).
    """

    def __init__(self, agent_exit: int) -> None:
        self._agent_exit = agent_exit
        self._execs = 0

    def exec(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: int,
    ) -> CommandHandle:
        first, self._execs = self._execs == 0, self._execs + 1
        return _ExitCodeHandle(self._agent_exit if first else 0)

    def sync_out(self) -> None:
        pass

    def sync_in(self) -> None:
        pass

    def close(self) -> None:
        pass


class _ExitCodeBackend(DockerBackend):
    """A DockerBackend whose session reports a chosen exit code for the agent exec."""

    def __init__(self, agent_exit: int, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._agent_exit = agent_exit

    def session(
        self,
        workdir: Path,
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
    ) -> ComputeSession:
        return _ExitCodeSession(self._agent_exit)


def test_generation_timeout_becomes_timeout_status(tmp_path: Path) -> None:
    """An agent killed at its deadline that then fails to verify is TIMEOUT, recorded.

    Drives the real ``prove`` lifecycle (real ``_run_agent`` + verify) over a session
    that hands back coreutils-``timeout`` exit 124 for the agent exec; no monkeypatch.
    """
    prover = AgentProver(backend=_ExitCodeBackend(124, image=DEFAULT_IMAGE))

    result = prover.prove(ProofTask(LeanProject(FIXTURE)), tmp_path / "out")

    assert result.status is ProofStatus.TIMEOUT
    assert result.error == "GenerationTimeout"
    assert result.error_msg is not None and "budget" in result.error_msg
    assert not result.success
    # It is a record, not a raise: the run's artifacts still land.
    payload = json.loads((result.logs_dir / "result.json").read_text())
    assert payload["status"] == "timeout" and payload["error"] == "GenerationTimeout"


def test_generation_timeout_that_still_verifies_is_not_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadline-killed agent whose salvaged proof verifies is VERIFIED, not TIMEOUT.

    The timeout only reclassifies a run that also failed to verify -- a killed agent
    that happened to leave a complete proof still wins.
    """
    prover = AgentProver(backend=_ExitCodeBackend(124, image=DEFAULT_IMAGE))
    # The agent exec still times out (124); force the verify verdict to pass so the
    # salvaged candidate verifies despite the deadline kill.
    report = VerificationReport(compiles=True, sorry_free=True)
    monkeypatch.setattr(prover.verifier, "verify", lambda project, session=None: report)

    result = prover.prove(ProofTask(LeanProject(FIXTURE)), tmp_path / "out")

    assert result.success and result.status is ProofStatus.VERIFIED
    assert result.error is None


def test_missing_credentials_raises_out_of_prove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent credential raises out of prove() rather than becoming a record."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    prover = AgentProver(backend=DockerBackend(image=DEFAULT_IMAGE))
    out = tmp_path / "run"

    with pytest.raises(MissingCredentials, match="CLAUDE_CODE_OAUTH_TOKEN"):
        prover.prove(ProofTask(LeanProject(FIXTURE)), out)

    # The run never completed, so no result record was written.
    assert not (out / "logs" / "result.json").exists()


def test_codex_missing_auth_file_raises_out_of_prove(tmp_path: Path) -> None:
    """A codex harness with no auth.json raises MissingCredentials out of prove().

    The codex CLI authenticates from a mounted ``auth.json``; resolving mounts is a
    pre-run credential step, so its absence raises rather than becoming a record.
    """
    harness = CodexHarness(auth_file=tmp_path / "nonexistent" / "auth.json")
    prover = AgentProver(backend=DockerBackend(image=DEFAULT_IMAGE), harness=harness)
    out = tmp_path / "run"

    with pytest.raises(MissingCredentials, match="auth.json"):
        prover.prove(ProofTask(LeanProject(FIXTURE)), out)

    assert not (out / "logs" / "result.json").exists()


# --- mocked agent + real Docker verify --------------------------------------


@pytest.mark.docker
def test_prove_reuses_one_sandbox_for_generation_and_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_prover: object
) -> None:
    """Full prove(): generation and the final verify run in ONE live sandbox.

    A stubbed agent writes a real proof and auth is stubbed (no creds/credential
    mounts); the shared verifier then compiles in the *same* session the agent ran
    in -- the no-second-spin-up path every agentic prover takes -- and confirms it.
    """

    def _fake_run_agent(
        self: AgentProver,
        workdir: Path,
        harness: Harness,
        stdout_path: Path,
        session: object | None = None,
        timeout_s: int | None = None,
    ) -> tuple[list[str], str, bool]:
        # Generation hands the live session through to the agent run.
        assert session is not None
        (workdir / "MILExample.lean").write_text(SOLVED_FILE)
        return STREAM.read_text().splitlines(), "", False

    monkeypatch.setattr(AgentProver, "_run_agent", _fake_run_agent)
    # No real credential resolution/mounts -- keeps the session sandbox dependency-free.
    monkeypatch.setattr(AgentProver, "_auth", lambda self, harness: ({}, []))
    prover = make_prover(real=True)

    result = prover.prove(ProofTask(LeanProject(FIXTURE)), tmp_path / "out")

    assert result.success, result.verification and result.verification.compile_log
    assert result.verification is not None and result.verification.verified
    assert result.prover == "agent"
    assert result.cost_usd == pytest.approx(0.4231)
    # The proof landed in output_dir/wd and the run record in output_dir/logs.
    assert (result.wd / "MILExample.lean").is_file()
    assert (result.logs_dir / "result.json").is_file()
