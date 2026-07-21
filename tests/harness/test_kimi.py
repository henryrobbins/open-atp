"""KimiHarness tests (Moonshot Kimi Code CLI, OAuth-authenticated).

Fast unit layer (no Docker, no creds, no API):

* ``stage_wd`` bootstraps a workdir-local KIMI_CODE_HOME from a source data dir
  (credential + provider config + a user-scope lean-lsp ``mcp.json``), and fails
  loudly when the source has no ``credentials/``.
* ``_agent_command`` renders the model and exports the workdir-local home.
* ``parse_result`` sums tokens from the per-session ``wire.jsonl`` (the kimi-specific
  seam -- the stream-json output carries no tokens) and the final assistant text
  from the stream; cost stays ``None`` (flat-rate, no USD reported).
* ``prove`` diffs the workdir after a stubbed run that writes a solved file and a
  synthetic wire log, and relocates the session log + scrubs the credential.

The live path (the real ``kimi`` CLI) lives in the single parametrized
``test_e2e_provers.py`` suite, alongside every other prover.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from open_atp.backends.base import CommandHandle, CommandResult, ComputeSession
from open_atp.backends.docker import DockerBackend
from open_atp.harness import (
    _HARNESSES,
    Harness,
    KimiHarness,
    MissingCredentials,
)
from open_atp.images import DEFAULT_IMAGE
from open_atp.lean import LeanProject, ProofTask
from open_atp.provers.agent_prover import AgentProver
from open_atp.provers.base import ProofResult

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"

SOLVED_FILE = """\
import Mathlib

theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by
  rw [mul_comm a b, mul_assoc b a c]
"""

# A trimmed `kimi -p --output-format stream-json` stdout: an assistant message and
# the trailing resume-hint meta line. It carries no token usage (that's in the wire
# log); only string `content` sets result_text (a tool_calls turn does not).
STREAM_LINES = [
    json.dumps({"role": "assistant", "tool_calls": [{"type": "function"}]}),
    "",
    "not json",
    json.dumps({"role": "assistant", "content": "Done. Replaced sorry with rw."}),
    json.dumps(
        {"role": "meta", "type": "session.resume_hint", "session_id": "session_x"}
    ),
]

# A trimmed session wire log: a per-step end (finishReason) and the turn's
# usage.record. input = inputOther + inputCacheRead + inputCacheCreation.
WIRE_LINES = [
    json.dumps(
        {
            "type": "agent.event",
            "event": {
                "type": "step.end",
                "finishReason": "end_turn",
                "usage": {"inputOther": 2646, "output": 27, "inputCacheRead": 17920},
            },
        }
    ),
    json.dumps(
        {
            "type": "usage.record",
            "model": "kimi-code/kimi-for-coding",
            "usage": {
                "inputOther": 2646,
                "output": 27,
                "inputCacheRead": 17920,
                "inputCacheCreation": 0,
            },
            "usageScope": "turn",
        }
    ),
]


def _fake_source_home(root: Path) -> Path:
    """A minimal ``~/.kimi-code`` to stage from: credential + config + device id."""
    home = root / "kimi-src"
    (home / "credentials").mkdir(parents=True)
    (home / "credentials" / "kimi-code.json").write_text('{"access_token": "tok"}')
    (home / "config.toml").write_text('default_model = "kimi-code/kimi-for-coding"\n')
    (home / "device_id").write_text("device-123")
    return home


def _write_wire_log(wd: Path, lines: list[str]) -> None:
    """Emulate the sandbox writing a session log under the workdir-local home."""
    sess = (
        wd
        / KimiHarness.KIMI_HOME_DIR
        / "sessions"
        / "wd_test_abc"
        / "session_test"
        / "agents"
        / "main"
    )
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "wire.jsonl").write_text("\n".join(lines))


class _AuthFailHandle(CommandHandle):
    """A finished command reporting a fixed exit code and stderr, no stdout."""

    def __init__(self, exit_code: int, stderr: str) -> None:
        self._exit_code = exit_code
        self._stderr = stderr

    def stream(self) -> Iterator[str]:
        return iter(())

    def wait(self) -> CommandResult:
        return CommandResult(
            exit_code=self._exit_code, stdout="", stderr=self._stderr, duration_s=0.0
        )


class _AuthFailSession(ComputeSession):
    """A session whose first exec (the agent) fails with the given exit + stderr.

    Later execs (the in-session verify) succeed, so the agent's auth failure -- not the
    compile check -- is what the test exercises.
    """

    def __init__(self, exit_code: int, stderr: str) -> None:
        self._exit_code = exit_code
        self._stderr = stderr
        self._execs = 0

    def exec(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: int,
    ) -> CommandHandle:
        first, self._execs = self._execs == 0, self._execs + 1
        if first:
            return _AuthFailHandle(self._exit_code, self._stderr)
        return _AuthFailHandle(0, "")

    def sync_out(self) -> None:
        pass

    def sync_in(self) -> None:
        pass

    def close(self) -> None:
        pass


class _AuthFailBackend(DockerBackend):
    """A DockerBackend whose session fails the agent exec with a given exit + stderr."""

    def __init__(self, exit_code: int, stderr: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._exit_code = exit_code
        self._stderr = stderr

    def session(
        self,
        workdir: Path,
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
    ) -> ComputeSession:
        return _AuthFailSession(self._exit_code, self._stderr)


@pytest.fixture
def prover(fake_session_backend: object, tmp_path: Path) -> AgentProver:
    # The in-process fake session keeps the diff unit test (which stubs _run_agent)
    # off a live backend while _generate opens its session and verifies in it.
    harness = KimiHarness(home_dir=_fake_source_home(tmp_path))
    return AgentProver(harness=harness, backend=fake_session_backend)


# --- construction / config -------------------------------------------------


def test_registered_and_construction_carries_kimi_knobs(tmp_path: Path) -> None:
    assert _HARNESSES["kimi"] is KimiHarness

    home = _fake_source_home(tmp_path)
    harness = KimiHarness(model="kimi-code/k3", home_dir=home)
    assert isinstance(harness, KimiHarness)
    assert harness.model == "kimi-code/k3"
    assert harness._source_home() == home


def test_source_home_defaults_to_env_then_dotdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIMI_CODE_HOME", "/custom/kimi")
    assert KimiHarness()._source_home() == Path("/custom/kimi")
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    assert KimiHarness()._source_home() == Path.home() / ".kimi-code"


def test_agent_command_renders_model_and_workdir_home() -> None:
    harness = KimiHarness(model="kimi-code/kimi-for-coding")
    script = harness._agent_command()
    assert "--model 'kimi-code/kimi-for-coding'" in script
    assert "<<MODEL>>" not in script
    assert 'export KIMI_CODE_HOME="$PWD/.kimi-home"' in script
    assert "--output-format stream-json" in script
    # No background cron daemon / auto-updater in an ephemeral sandbox: they do
    # unwanted network/CPU work and a lingering child destabilizes the sandbox.
    assert "export KIMI_DISABLE_CRON=1" in script
    assert "export KIMI_CODE_NO_AUTO_UPDATE=1" in script


# --- stage -----------------------------------------------------------------


def test_stage_requires_credentials(tmp_path: Path) -> None:
    # A source home with no credentials/ dir fails loudly rather than deep in a run.
    empty = tmp_path / "no-creds"
    empty.mkdir()
    wd = tmp_path / "wd"
    wd.mkdir()
    harness = KimiHarness(home_dir=empty)
    with pytest.raises(MissingCredentials, match="credentials"):
        harness.stage_wd(wd)


def test_stage_bootstraps_workdir_local_home(tmp_path: Path) -> None:
    harness = KimiHarness(home_dir=_fake_source_home(tmp_path))
    wd = tmp_path / "wd"
    wd.mkdir()
    harness.stage_wd(wd)
    harness.write_prompt(wd, "fill the sorrys")

    assert (wd / "agent.sh").is_file()
    assert (wd / "agent_prompt.txt").read_text() == "fill the sorrys"

    home = wd / ".kimi-home"
    # Credential + provider config + device id copied from the source home.
    assert (
        home / "credentials" / "kimi-code.json"
    ).read_text() == '{"access_token": "tok"}'
    assert (home / "config.toml").is_file()
    assert (home / "device_id").read_text() == "device-123"

    # User-scope MCP: lean-lsp with a raised startup/tool timeout for the cold
    # Mathlib load (mirrors the opencode/vibe fix); Kimi's fields are in ms.
    mcp = json.loads((home / "mcp.json").read_text())
    server = mcp["mcpServers"]["lean-lsp"]
    assert server["command"] == "lean-lsp-mcp"
    assert server["startupTimeoutMs"] == 180_000
    assert server["toolTimeoutMs"] == 180_000

    # Skills are staged by the prover (stage_skills) into the user-scope skills dir
    # under KIMI_CODE_HOME -- auto-discovered without a .git anchor.
    assert harness.skills_dest == ".kimi-home/skills"


# --- parse: tokens from the wire log, text from the stream -----------------


def test_parse_reads_tokens_from_wire_log(tmp_path: Path) -> None:
    harness = KimiHarness(home_dir=_fake_source_home(tmp_path))
    wd = tmp_path / "wd"
    wd.mkdir()
    _write_wire_log(wd, WIRE_LINES)

    result = harness.parse_result(STREAM_LINES, wd)
    assert result.input_tokens == 2646 + 17920  # inputOther + inputCacheRead
    assert result.output_tokens == 27
    assert result.stop_reason == "end_turn"
    assert result.result_text == "Done. Replaced sorry with rw."
    # Kimi bills a flat subscription rate and reports no USD.
    assert result.cost_usd is None


def test_parse_without_wire_log_leaves_tokens_zero(tmp_path: Path) -> None:
    harness = KimiHarness(home_dir=_fake_source_home(tmp_path))
    wd = tmp_path / "wd"
    wd.mkdir()
    result = harness.parse_result(STREAM_LINES, wd)  # no wire log written
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.cost_usd is None
    # The stream still yields the final assistant text.
    assert result.result_text == "Done. Replaced sorry with rw."


# --- runtime auth failure --------------------------------------------------

# A trimmed `kimi -p` stderr when the staged OAuth token is expired/absent: the CLI
# refuses to run and exits non-zero.
LOGIN_REQUIRED_STDERR = (
    'auth.login_required: OAuth provider "managed:kimi-code" requires login before '
    "it can be used.\nSee log: /workspace/wd/.kimi-home/logs/kimi-code.log"
)


def test_check_auth_raises_on_login_required() -> None:
    # The token is only validated when `kimi -p` runs, so an expired login surfaces
    # as a non-zero exit carrying `login_required` -- mapped onto MissingCredentials
    # so the run is an error, not a genuine unverified miss.
    with pytest.raises(MissingCredentials, match="login"):
        KimiHarness().check_auth(1, LOGIN_REQUIRED_STDERR)


def test_check_auth_ignores_clean_exit_and_unrelated_failures() -> None:
    harness = KimiHarness()
    # A clean exit is never an auth failure, whatever the stderr says.
    harness.check_auth(0, LOGIN_REQUIRED_STDERR)
    # A non-zero exit for some other reason is a real run failure, not missing creds.
    harness.check_auth(1, "lake env lean failed: elaboration error")


def test_runtime_auth_failure_raises_out_of_prove(tmp_path: Path) -> None:
    """A login_required agent exit raises MissingCredentials out of prove().

    Drives the real ``prove`` lifecycle over a session whose agent exec exits 1 with
    the ``login_required`` stderr; the run is reported as an error (a re-raise), not a
    silent unverified proof attempt.
    """
    harness = KimiHarness(home_dir=_fake_source_home(tmp_path))
    backend = _AuthFailBackend(1, LOGIN_REQUIRED_STDERR, image=DEFAULT_IMAGE)
    prover = AgentProver(harness=harness, backend=backend)

    with pytest.raises(MissingCredentials, match="login"):
        prover.prove(ProofTask(LeanProject(FIXTURE)), tmp_path / "out")


# --- prove() diff logic (no Docker) ----------------------------------------


def test_generate_reports_changes_and_relocates_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prover: AgentProver
) -> None:
    """_generate(): stage -> stubbed agent writes a solved file + wire log -> diff.

    Drives the generation half directly (no Docker verify) and asserts the kimi
    session log is relocated into the logs dir and the credential is scrubbed.
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
        # The real kimi run writes this under the workdir-local home; emulate it so
        # parse_result() finds the tokens.
        assert isinstance(harness, KimiHarness)
        _write_wire_log(workdir, WIRE_LINES)
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
    # The session log was relocated out of the workdir into the run's logs dir, and
    # the credential scrubbed, so the downloaded wd stays the proof project and
    # carries no token.
    assert not (wd / ".kimi-home" / "sessions").exists()
    assert not (wd / ".kimi-home" / "credentials").exists()
    assert (logs_dir / "kimi-session").is_dir()
    assert list((logs_dir / "kimi-session").rglob("wire.jsonl"))
    # Tokens flow from the wire log; kimi never self-reports USD, so cost stays None.
    assert result.cost_usd is None
    assert result.metadata["harness"] == "kimi"
    assert result.metadata["model"] == "kimi-code/kimi-for-coding"
    assert result.metadata["input_tokens"] == 2646 + 17920
    assert result.metadata["output_tokens"] == 27
    assert result.metadata["stop_reason"] == "end_turn"
