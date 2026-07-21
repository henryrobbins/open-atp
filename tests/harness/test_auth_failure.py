"""What each agent CLI does when its credentials are bad.

Every harness is launched in the Docker sandbox with a deliberately invalid
credential and a throwaway prompt, so the run fails at authentication before any
tokens are billed. Each run's exit code / stdout / stderr are written to
``tests/.runs/auth-probe/<harness>.txt`` for inspection, and the output must be
recognized by :func:`~open_atp.harness.is_auth_failure` -- the matcher
``AgentProver`` raises :class:`~open_atp.harness.MissingCredentials` from. This is
what keeps that matcher honest as the CLIs reword their errors.

Marked ``docker`` (needs the built image, which carries the agent CLIs) but not
``agent_api``: no real credential is used and nothing is billed.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from open_atp.backends.base import CommandResult, CommandTimeout
from open_atp.backends.docker import DockerBackend
from open_atp.harness import (
    AxProverBaseHarness,
    ClaudeCodeHarness,
    CodexHarness,
    Harness,
    KimiHarness,
    OpenCodeHarness,
    VibeHarness,
    is_auth_failure,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"
ARTIFACTS = Path(__file__).parents[1] / ".runs" / "auth-probe"

#: A credential that is well-formed enough to be sent and always rejected.
BOGUS = "open-atp-invalid-credential"

#: Kept trivial so a credential that unexpectedly *works* costs a few tokens
#: rather than a proof attempt.
PROBE_PROMPT = "Reply with the single word OK and stop."

#: Auth rejection is one round trip; anything slower is the CLI hanging.
TIMEOUT_S = 240


def _claude(_: Path) -> Harness:
    return ClaudeCodeHarness(plugins=[], oauth_token=BOGUS)


def _codex(tmp: Path) -> Harness:
    auth = tmp / "auth.json"
    auth.write_text(json.dumps({"OPENAI_API_KEY": BOGUS}))
    auth.chmod(0o600)
    return CodexHarness(auth_file=auth)


def _opencode(_: Path) -> Harness:
    return OpenCodeHarness(api_key=BOGUS)


def _vibe(_: Path) -> Harness:
    return VibeHarness(mistral_api_key=BOGUS)


def _axprover(_: Path) -> Harness:
    return AxProverBaseHarness(provider_api_key=BOGUS)


#: Kimi resolves its model alias from ``config.toml``, so the staged home needs one
#: or the run fails on config before it ever authenticates. Mirrors the provider +
#: model entries ``kimi login`` writes, minus every other model.
_KIMI_CONFIG = """\
default_model = "kimi-code/k3"

[providers."managed:kimi-code"]
type = "kimi"
api_key = ""
base_url = "https://api.kimi.com/coding/v1"

[providers."managed:kimi-code".oauth]
storage = "file"
key = "oauth/kimi-code"

[models."kimi-code/k3"]
provider = "managed:kimi-code"
model = "k3"
max_context_size = 262144
capabilities = [ "thinking", "always_thinking", "tool_use" ]
support_efforts = [ "low", "high", "max" ]
default_effort = "high"
"""


def _kimi(tmp: Path) -> Harness:
    home = tmp / "kimi-home"
    (home / "credentials").mkdir(parents=True)
    (home / "config.toml").write_text(_KIMI_CONFIG)
    (home / "credentials" / "kimi-code.json").write_text(
        json.dumps(
            {
                "access_token": BOGUS,
                "refresh_token": BOGUS,
                "expires_at": 4102444800,
                "scope": "",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        )
    )
    return KimiHarness(home_dir=home)


# ``grok`` is the opencode harness on ``auth="login"``, which reads the host's real
# opencode credential store; the api_key row above covers the same CLI's failure text.
HARNESSES: list[pytest.param] = [
    pytest.param(_claude, id="claude_code"),
    pytest.param(_codex, id="codex"),
    pytest.param(_opencode, id="opencode"),
    pytest.param(_vibe, id="vibe"),
    pytest.param(_axprover, id="axproverbase"),
    pytest.param(_kimi, id="kimi"),
]


def _record(name: str, result: CommandResult, lines: list[str]) -> None:
    """Save the probe's raw output so the auth-failure patterns can be read off it."""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / f"{name}.txt").write_text(
        f"exit_code: {result.exit_code}\n"
        f"duration_s: {result.duration_s:.1f}\n"
        f"--- stdout ({len(lines)} lines) ---\n" + "\n".join(lines) + "\n"
        f"--- stderr ---\n{result.stderr}\n"
    )


@pytest.mark.docker
@pytest.mark.parametrize("build", HARNESSES)
def test_bogus_credentials_fail_fast(
    build: Callable[[Path], Harness], tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """Launch the harness with an invalid credential and record how it fails."""
    harness = build(tmp_path)
    wd = tmp_path / "wd"
    shutil.copytree(FIXTURE, wd)
    harness.stage_wd(wd)
    harness.write_prompt(wd, PROBE_PROMPT)

    backend = DockerBackend()
    auth = harness.agent_auth()
    mounts = [
        (str(src), f"{backend.container_home}/{dest}") for src, dest in auth.mounts
    ]
    lines: list[str] = []
    with backend.session(wd, mounts=mounts, timeout_s=TIMEOUT_S) as session:
        handle = session.exec(harness.command, timeout_s=TIMEOUT_S, env=auth.env)
        lines.extend(handle.stream())
        try:
            result = handle.wait()
        except CommandTimeout as exc:
            result = exc.result or CommandResult(124, "", "", 0.0)

    name = request.node.callspec.id
    _record(name, result, lines)
    # Not the exit code: ax-prover's launch script keeps going past a failed target
    # and exits 0 either way, so the output is what AgentProver keys the credential
    # check off.
    assert is_auth_failure("\n".join(lines) + "\n" + result.stderr), (
        f"{name} rejected the credential with an unrecognized message"
    )
