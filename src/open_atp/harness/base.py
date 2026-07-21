"""The harness contract: the *agent* concern of the agent prover.

A :class:`Harness` knows, for one agent CLI:

* how to populate the working directory from its assets (launch script, MCP
  config, skills) -- :meth:`Harness.stage_wd` -- and where to write the prompt the
  prover hands it -- :meth:`Harness.write_prompt`;
* the bash command that launches the agent -- :attr:`Harness.command`;
* which credentials to resolve and forward -- :meth:`Harness.agent_auth`; and
* how to read token/cost totals out of the agent's streamed JSON
  -- :meth:`Harness.parse_result`.

The *compute* concern (where that command runs, with Lean+Mathlib and a warm
cache) lives in the injected :class:`~open_atp.backends.base.ComputeBackend`.

The skills to mount are owned by
the prover (``AgentProver.skills``, resolved to source dirs and handed to
:meth:`Harness.stage_skills`); plugins are Claude-only and live on
``ClaudeCodeHarness.plugins``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from open_atp.auth import AuthKind, AuthStatus

log = logging.getLogger("open_atp")


class MissingCredentials(Exception):
    """A credential a prover needs to run is absent."""


#: What a rejected credential looks like in an agent CLI's output. Every CLI reports a
#: 401 differently -- ``authentication_failed`` (Claude Code), ``Incorrect API key``
#: (Codex), ``Authentication Fails`` (OpenCode/DeepSeek), ``Invalid API key`` (Vibe),
#: ``invalid x-api-key`` (ax-prover/Anthropic), ``auth.login_required`` (Kimi) -- and
#: the bare status code is too common a substring to match on alone.
#:
#: An OAuth login can also fail at the *refresh* step rather than on the request: the
#: provider answers the refresh with an ``invalid_grant`` error (RFC 6749) and the CLI
#: never reaches a 401, so none of the rejected-key phrasings appear.
_AUTH_FAILURE = re.compile(
    "|".join(
        (
            r"authentication[ _]?(?:failed|error|fails)",
            r"(?:invalid|incorrect|expired|missing)[ _-]"
            r"(?:api[ _-]?key|x-api-key|bearer token|credentials?|token)",
            r"401 unauthorized",
            r"login[ ._]?required",
            r"requires login",
            r"invalid_grant",
            r"token[ _]refresh[ _]failed",
            r"(?:api[ _-]?key|token|credentials?) (?:h(?:as|ave) )?(?:been )?revoked",
        )
    ),
    re.IGNORECASE,
)


def _is_auth_failure(output: str) -> bool:
    """Whether a line of agent output shows the credential was rejected."""
    return bool(_AUTH_FAILURE.search(output))


#: API key env vars used by providers used in standard provers
_PROVIDER_ENV = {"deepseek": "DEEPSEEK_API_KEY"}

#: The registry opencode reads provider `env` arrays from to auto-detect keys.
_MODELS_DEV_URL = "https://models.dev/api.json"


@lru_cache(maxsize=1)
def _models_dev_env() -> dict[str, list[str]]:
    """Fetch provider id -> env array from models.dev, cached."""
    try:
        with urllib.request.urlopen(_MODELS_DEV_URL, timeout=10) as resp:
            data = json.load(resp)
    except (OSError, ValueError):
        log.error("models.dev registry unreachable", extra={"url": _MODELS_DEV_URL})
        raise
    return {
        pid: p["env"] for pid, p in data.items() if isinstance(p, dict) and p.get("env")
    }


def _provider_env_var(provider: str) -> str:
    """The env var a provider reads its API key from."""
    if provider in _PROVIDER_ENV:
        return _PROVIDER_ENV[provider]
    return _models_dev_env()[provider][0]


#: Files the harness writes into the workdir; named so they never collide with a
#: project's own sources.
SCRIPT_FILE = "agent.sh"
PROMPT_FILE = "agent_prompt.txt"


@dataclass(frozen=True)
class AgentAuth:
    """Resolved credentials a harness hands the prover to wire into the sandbox.

    Unlike a declarative spec, ``env`` here holds resolved name->**value** pairs --
    the harness has already read the host environment (and any explicit overrides)
    and validated that required credentials are present. The prover only forwards
    them; it never touches ``os.environ``.

    Parameters
    ----------
    env : dict[str, str], optional
        Environment variables (name -> value) to forward into the sandbox. Defaults
        to an empty mapping.
    mounts : list[tuple[pathlib.Path, str]], optional
        Host directories to expose under the sandbox's ``$HOME``, as
        ``(host_dir, dest_basename)`` pairs (e.g. ``(~/.codex, ".codex")``).
        Defaults to an empty list.
    """

    env: dict[str, str] = field(default_factory=dict)
    mounts: list[tuple[Path, str]] = field(default_factory=list)


@dataclass
class HarnessRunResult:
    """Token totals and cost parsed from an agent's streamed output.

    Parameters
    ----------
    input_tokens : int, default 0
        Total input (prompt) tokens the run consumed.
    cached_input_tokens : int, default 0
        The cache-hit subset of ``input_tokens``, when the agent reports one.
    output_tokens : int, default 0
        Total output (completion) tokens the run produced.
    stop_reason : str, optional
        Why the agent stopped, when the stream reports it; ``None`` otherwise.
    cost_usd : float, optional
        USD cost if the harness self-reports it (Claude Code, OpenCode); ``None``
        when it must be estimated from token counts (Codex, ax-prover).
    subtype : str, optional
        Final ``type:"result"`` subtype (Claude Code: ``success`` /
        ``error_max_turns`` / ``error_during_execution``); ``None`` if not reported.
    result_text : str, optional
        The agent's final result text (Claude Code's ``result`` field); ``None``
        otherwise.
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None
    cost_usd: float | None = None
    subtype: str | None = None
    result_text: str | None = None


class Harness(ABC):
    """Base class for an agent CLI harness.

    Parameters
    ----------
    model : str, default "claude-opus-4-8"
        Model id the agent runs. Subclasses override the default with the model
        their CLI drives.
    effort : str, default "high"
        Reasoning-effort level passed to harnesses that support it.
    """

    name: ClassVar[str]

    #: Workdir-relative directory this harness mounts skills into (e.g.
    #: ``.claude/skills``), or ``None`` if the harness doesn't consume skills
    #: (ax-prover ships its own). Read by :meth:`stage_skills` and the prover.
    skills_dest: ClassVar[str | None] = None

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-8",
        effort: str = "high",
    ) -> None:
        self.model = model
        self.effort = effort

    @property
    def command(self) -> str:
        """Bash command the backend runs to launch the agent.

        The backend has already ``cd``'d into the workdir and symlinked ``.lake``;
        we export ``$PROMPT`` from the written prompt file (the launch scripts
        reference it) and run the rendered script.

        Returns
        -------
        str
            The bash one-liner that exports ``$PROMPT`` and runs ``agent.sh``.
        """
        return f'export PROMPT="$(cat {PROMPT_FILE})" && bash {SCRIPT_FILE}'

    def agent_auth(self) -> AgentAuth:
        """Resolve this harness's credentials into a ready-to-forward auth bundle.

        Raises :exc:`MissingCredentials` if a key this harness requires is absent
        from both its explicit arguments and the host environment.

        Returns
        -------
        AgentAuth
            Resolved env (name -> value) and ``(host_dir, dest_basename)`` mounts
            the prover forwards into the sandbox.

        Examples
        --------
        An explicit ``oauth_token`` is resolved into the forwarded env:

        >>> from open_atp.harness import ClaudeCodeHarness
        >>> harness = ClaudeCodeHarness(oauth_token="sk-ant-oat-fake")
        >>> harness.agent_auth().env["CLAUDE_CODE_OAUTH_TOKEN"]
        'sk-ant-oat-fake'
        """
        env: dict[str, str] = {}
        env.update(self._static_env())
        env.update(self._required_env())
        auth = AgentAuth(env=env, mounts=self._home_dirs())
        log.debug(
            "resolved agent auth",
            extra={
                "harness": self.name,
                "env_keys": sorted(auth.env),
                "mounts": [dest for _, dest in auth.mounts],
            },
        )
        return auth

    @abstractmethod
    def auth_status(self) -> AuthStatus:
        """Report this harness's credential without resolving or staging it.

        Unlike :meth:`agent_auth`, this never raises on an absent credential and
        never touches the workdir -- an unauthenticated host gets a
        :attr:`~open_atp.auth.AuthState.MISSING` status back, not an exception.

        Returns
        -------
        ~open_atp.auth.AuthStatus
            Where the credential lives, whether it is there, and when it expires.
        """

    def _env_auth_status(
        self,
        env_name: str,
        explicit: str | None,
        kind: AuthKind = AuthKind.API_KEY,
    ) -> AuthStatus:
        """An :class:`~open_atp.auth.AuthStatus` for a credential read from the env.

        The non-raising counterpart to :meth:`_key_env`, resolving the same way: an
        explicit constructor override wins, else the host environment.

        Parameters
        ----------
        env_name : str
            The env var the credential is read from.
        explicit : str, optional
            A constructor override taking precedence over the host environment.
        kind : ~open_atp.auth.AuthKind, default AuthKind.API_KEY
            What sort of credential the env var carries.

        Returns
        -------
        ~open_atp.auth.AuthStatus
            A status with no expiry -- an env-borne credential carries none.
        """
        return AuthStatus(
            kind=kind,
            source=env_name,
            present=bool(explicit or os.environ.get(env_name)),
        )

    def _static_env(self) -> dict[str, str]:
        """Non-secret env vars to set for this harness (e.g. ``IS_SANDBOX``).

        Returns
        -------
        dict[str, str]
            Constant name -> value pairs; empty for the base class.
        """
        return {}

    def _required_env(self) -> dict[str, str]:
        """Resolve the harness's required credentials (name -> value).

        Override to read explicit constructor overrides or fall back to the host
        environment, raising if a required key is absent.

        Returns
        -------
        dict[str, str]
            Resolved credential name -> value pairs; empty for the base class.
        """
        return {}

    def _home_dirs(self) -> list[tuple[Path, str]]:
        """``(src, basename)`` dirs to mount under the sandbox ``$HOME``.

        Returns
        -------
        list[tuple[pathlib.Path, str]]
            ``(host_dir, dest_basename)`` mount pairs; empty for the base class.
        """
        return []

    def _key_env(self, env_name: str, explicit: str | None) -> dict[str, str]:
        """Resolve an API key, forwarded under ``env_name``.

        ``explicit`` (a constructor override) wins; otherwise read the host env under
        ``env_name``. Raise if neither is set. No format check -- OpenAI and DeepSeek
        keys are both ``sk-...`` and indistinguishable, so the key is assumed correct
        for the selected provider.

        Parameters
        ----------
        env_name : str
            The canonical env-var name the key is forwarded under.
        explicit : str, optional
            A constructor override that takes precedence over the host environment;
            ``None`` falls back to reading the host env.

        Returns
        -------
        dict[str, str]
            A single ``{env_name: key}`` pair to forward into the sandbox.

        Raises
        ------
        MissingCredentials
            If neither ``explicit`` nor the host env supplies the key.
        """
        key = explicit or os.environ.get(env_name)
        if not key:
            log.error(
                "missing provider credential",
                extra={"harness": self.name, "env": env_name},
            )
            raise MissingCredentials(f"{self.name} harness requires {env_name}")
        return {env_name: key}

    def stage_wd(self, wd: Path) -> None:
        """Populate ``wd`` with the harness's launch script.

        Everything the harness itself owns -- *not* the skills list (the prover stages
        it via :meth:`stage_skills`) and *not* the prompt (the prover and task own it,
        written via :meth:`write_prompt`). Subclasses that need more (Vibe's
        VIBE_HOME, ax-prover's per-target setup) override and call ``super().stage_wd``.

        Parameters
        ----------
        wd : pathlib.Path
            The agent working directory to populate; must already exist.

        Raises
        ------
        RuntimeError
            If ``wd`` does not exist.
        """
        if not wd.exists():
            log.error(
                "agent working directory missing",
                extra={"harness": self.name, "wd": str(wd)},
            )
            raise RuntimeError("The agent working directory must be created first.")
        (wd / SCRIPT_FILE).write_text(self._agent_command())

    def write_prompt(self, wd: Path, prompt: str) -> None:
        """Write the composed prompt where this harness's launch script reads it.

        The prompt's *content* is owned by the prover (its prover prompt) and the task
        (the optional user prompt); the harness owns only the file location and the
        ``cat $PROMPT`` launch contract, so it provides the write mechanism.

        Parameters
        ----------
        wd : pathlib.Path
            The agent working directory the launch script reads the prompt from.
        prompt : str
            The composed prompt text to write to :data:`PROMPT_FILE`.
        """
        (wd / PROMPT_FILE).write_text(prompt)

    def parse_result(self, lines: list[str], wd: Path) -> HarnessRunResult:
        """Parse the agent's run into a :class:`HarnessRunResult`.

        ``wd`` is passed explicitly (not stashed on the instance) so a single harness
        shared across concurrently-running tasks reads *this* task's usage: harnesses
        whose cost/tokens live in workdir files (Vibe's session log, ax-prover's ``-o``
        JSON) read them from ``wd`` here. The CLI harnesses parse from ``lines`` alone
        and ignore ``wd``.

        Parameters
        ----------
        lines : list[str]
            The agent's streamed stdout, one JSON object per line.
        wd : pathlib.Path
            The agent working directory this run used; where a harness reads any
            workdir-local usage files.

        Returns
        -------
        HarnessRunResult
            Token totals, cost, and stop metadata parsed from the run.
        """
        result = self._parse_lines(lines)
        self._log_usage(result)
        return result

    def _log_usage(self, result: HarnessRunResult) -> None:
        """Log a run's parsed token/cost totals.

        Kept separate from :meth:`parse_result` so usage lands on the ``open_atp``
        logger regardless of which harness produced it.
        """
        log.debug(
            "parsed agent usage",
            extra={
                "harness": self.name,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": result.cost_usd,
                "stop_reason": result.stop_reason,
            },
        )

    def collect_logs(self, wd: Path, logs_dir: Path) -> None:
        """Move this harness's rich log files out of ``wd`` into ``logs_dir``.

        The streamed event JSONL the prover captures from stdout *is* the agent's
        transcript for every CLI harness, so the default does nothing. Harnesses that
        *also* drop a richer record inside the workdir override this to relocate those
        files, so the downloaded workdir stays the proof project and the downloaded
        logs carry the full record. Runs after :meth:`parse_result`, which may read
        those files for cost, so moving them is safe.

        Parameters
        ----------
        wd : pathlib.Path
            The agent working directory rich log files are moved out of.
        logs_dir : pathlib.Path
            The run's log directory the files are relocated into.
        """

    def stage_skills(self, wd: Path, skill_dirs: list[Path]) -> None:
        """Copy resolved skill source dirs into this harness's skill location.

        Each ``<name>/SKILL.md`` tree lands at ``wd/<skills_dest>/<dir-name>/`` (an
        upstream ``tests/`` fixture dir is dropped). A no-op for a harness that does
        not consume skills (``skills_dest is None``, e.g. ax-prover). The prover owns
        the *list* (``AgentProver.skills``, resolved by ``resolve_skill``);
        the harness owns *where* it goes.

        Parameters
        ----------
        wd : pathlib.Path
            The agent working directory the skills are copied into (under
            :attr:`skills_dest`).
        skill_dirs : list[pathlib.Path]
            Resolved skill source dirs (each a ``<name>/SKILL.md`` tree) to copy.
        """
        if self.skills_dest is None or not skill_dirs:
            return
        target = wd / self.skills_dest
        target.mkdir(parents=True, exist_ok=True)
        for skill in skill_dirs:
            shutil.copytree(
                skill,
                target / skill.name,
                ignore=shutil.ignore_patterns("tests"),
                dirs_exist_ok=True,
            )
        log.debug(
            "staged skills",
            extra={"harness": self.name, "skills": [s.name for s in skill_dirs]},
        )

    def _render(self, template: str) -> str:
        """Substitute ``<<MODEL>>``/``<<EFFORT>>`` into a launch-script template.

        Parameters
        ----------
        template : str
            A launch-script template with ``<<MODEL>>``/``<<EFFORT>>`` placeholders.

        Returns
        -------
        str
            ``template`` with the placeholders replaced by :attr:`model`/:attr:`effort`.
        """
        return template.replace("<<MODEL>>", self.model).replace(
            "<<EFFORT>>", self.effort
        )

    @abstractmethod
    def _agent_command(self) -> str:
        """The rendered contents of the workdir's ``agent.sh``.

        Returns
        -------
        str
            The fully rendered launch script for this harness.
        """

    @abstractmethod
    def _parse_lines(self, lines: list[str]) -> HarnessRunResult:
        """Parse the agent's streamed JSON lines into a :class:`HarnessRunResult`.

        Parameters
        ----------
        lines : list[str]
            The agent's streamed stdout, one JSON object per line.

        Returns
        -------
        HarnessRunResult
            Token totals, cost, and stop metadata parsed from the stream.
        """
