"""Every agent CLI, launched against a deliberately invalid credential.

Each run fails at authentication before any tokens are billed, and ``prove`` must
surface that as :class:`~open_atp.harness.MissingCredentials` rather than an
ordinary unverified miss. This is what keeps that check honest as the CLIs reword
their errors: they phrase a 401 six different ways, some on stdout and some only
on stderr, and ax-prover exits 0 regardless. Each run's logs are kept under
``tests/.runs/auth-probe/<harness>/`` for inspection.

Marked ``docker`` (needs the built image, which carries the agent CLIs) but not
``agent_api``: no real credential is used and nothing is billed.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from open_atp.backends.docker import DockerBackend
from open_atp.harness import (
    AxProverBaseHarness,
    ClaudeCodeHarness,
    CodexHarness,
    Harness,
    KimiHarness,
    MissingCredentials,
    OpenCodeHarness,
    VibeHarness,
)
from open_atp.lean import LeanProject, ProofTask
from open_atp.provers.agent_prover import AgentProver

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"
ARTIFACTS = Path(__file__).parents[1] / ".runs" / "auth-probe"

#: A credential that is well-formed enough to be sent and always rejected.
BOGUS = "open-atp-invalid-credential"

#: Rejection is one round trip, plus whatever start-up the CLI does first
#: (ax-prover warms up LeanSearch and its REPL cache before its first API call).
TIMEOUT_S = 300


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


@pytest.mark.docker
@pytest.mark.parametrize("build", HARNESSES)
def test_bogus_credential_raises_missing_credentials(
    build: Callable[[Path], Harness], tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """The real CLI rejects the credential and ``prove`` reports it as such."""
    prover = AgentProver(
        backend=DockerBackend(), harness=build(tmp_path), timeout_s=TIMEOUT_S
    )
    run = tmp_path / "run"

    with pytest.raises(MissingCredentials, match="rejected"):
        prover.prove(ProofTask(LeanProject(FIXTURE)), run)

    # Keep the CLI's own words around: they are the evidence behind the matcher.
    kept = ARTIFACTS / request.node.callspec.id
    shutil.rmtree(kept, ignore_errors=True)
    shutil.copytree(run / "logs", kept)
