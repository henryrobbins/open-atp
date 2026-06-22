"""Agent harnesses: the *agent* concern of :class:`AgentProver`.

A :class:`Harness` knows, for one agent CLI (Claude Code / Codex / OpenCode):

* how to populate the working directory (launch script, MCP config, skills,
  prompt) -- :meth:`Harness.configure_wd`;
* the bash command that launches the agent -- :attr:`Harness.command`;
* which credentials to forward into the sandbox -- :meth:`Harness.auth_spec`; and
* how to read token/cost totals out of the agent's streamed JSON
  -- :meth:`Harness.parse`.

The *compute* concern (where that command runs, with Lean+Mathlib and a warm
cache) lives in the injected :class:`~open_afps.backends.base.ComputeBackend`.

Ported from milp_flare's ``harness/`` package; the MILP-specific skills are
replaced by a single generic ``filling-sorrys`` skill.
"""

from __future__ import annotations

import json
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

_ASSETS = Path(__file__).parent / "assets"
_SCRIPTS = _ASSETS / "scripts"
_SKILLS = _ASSETS / "skills"
_MCP_JSON = _ASSETS / "configs" / "mcp.json"

#: Files the harness writes into the workdir; named so they never collide with a
#: project's own sources.
SCRIPT_FILE = "agent.sh"
PROMPT_FILE = "agent_prompt.txt"


@dataclass(frozen=True)
class AuthSpec:
    """Compute-agnostic description of the credentials a harness needs.

    Attributes
    ----------
    env:
        Host environment-variable names to forward into the sandbox.
    home_dirs:
        Host directories to expose under the sandbox's ``$HOME``, as
        ``(host_dir, dest_basename)`` pairs (e.g. ``(~/.codex, ".codex")``).
    """

    env: list[str] = field(default_factory=list)
    home_dirs: list[tuple[Path, str]] = field(default_factory=list)


@dataclass
class HarnessRunResult:
    """Token totals and cost parsed from an agent's streamed output."""

    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None
    #: USD cost if the harness self-reports it (Claude Code, OpenCode); ``None``
    #: when it must be estimated from token counts (Codex).
    cost_usd: float | None = None


class Harness(ABC):
    """Base class for an agent CLI harness."""

    name: ClassVar[str]

    def __init__(self, model: str, effort: str = "medium") -> None:
        self.model = model
        self.effort = effort

    @property
    def command(self) -> str:
        """Bash command the backend runs to launch the agent.

        The backend has already ``cd``'d into the workdir and symlinked ``.lake``;
        we export ``$PROMPT`` from the written prompt file (the launch scripts
        reference it) and run the rendered script.
        """
        return f'export PROMPT="$(cat {PROMPT_FILE})" && bash {SCRIPT_FILE}'

    def static_env(self) -> dict[str, str]:
        """Non-secret env vars to set for this harness (e.g. ``IS_SANDBOX``)."""
        return {}

    def auth_spec(self) -> AuthSpec:
        """Credentials to forward into the sandbox for this harness."""
        return AuthSpec()

    def configure_wd(self, wd: Path, prompt: str) -> None:
        """Populate ``wd`` with the launch script, prompt, MCP config, and skills."""
        if not wd.exists():
            raise RuntimeError("The agent working directory must be created first.")
        (wd / SCRIPT_FILE).write_text(self._agent_command())
        (wd / PROMPT_FILE).write_text(prompt)

    def parse(self, lines: list[str]) -> HarnessRunResult:
        """Parse the agent's streamed JSON lines into a :class:`HarnessRunResult`."""
        return self._parse_lines(lines)

    @staticmethod
    def _copy_skills(wd: Path, dest: str) -> None:
        """Copy the generic skill bundle into ``wd/<dest>``."""
        target = wd / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(_SKILLS, target, dirs_exist_ok=True)

    def _render(self, template: str) -> str:
        """Substitute ``<<MODEL>>``/``<<EFFORT>>`` into a launch-script template."""
        return template.replace("<<MODEL>>", self.model).replace(
            "<<EFFORT>>", self.effort
        )

    @abstractmethod
    def _agent_command(self) -> str:
        """The rendered contents of the workdir's ``agent.sh``."""

    @abstractmethod
    def _parse_lines(self, lines: list[str]) -> HarnessRunResult: ...


class ClaudeCodeHarness(Harness):
    """Claude Code CLI, authenticated by a long-lived ``CLAUDE_CODE_OAUTH_TOKEN``."""

    name = "claude_code"

    def configure_wd(self, wd: Path, prompt: str) -> None:
        super().configure_wd(wd, prompt)
        # Project-scope MCP config (passed via --mcp-config) and skills.
        shutil.copy2(_MCP_JSON, wd / ".mcp.json")
        self._copy_skills(wd, ".claude/skills")

    def static_env(self) -> dict[str, str]:
        # Lets bypassPermissions run non-interactively in the container.
        return {"IS_SANDBOX": "1"}

    def auth_spec(self) -> AuthSpec:
        # A long-lived token (from `claude setup-token`) bills against a Claude
        # subscription rather than at the higher per-API-call rate.
        if "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ:
            raise RuntimeError(
                "claude_code harness requires CLAUDE_CODE_OAUTH_TOKEN"
                " from `claude setup-token`"
            )
        return AuthSpec(env=["CLAUDE_CODE_OAUTH_TOKEN"])

    def _agent_command(self) -> str:
        return self._render((_SCRIPTS / "claude_code_agent.sh").read_text())

    def _parse_lines(self, lines: list[str]) -> HarnessRunResult:
        """Parse ``claude -p --output-format stream-json`` output."""
        result = HarnessRunResult()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "result":
                result.stop_reason = obj.get("stop_reason")
                result.cost_usd = obj.get("total_cost_usd")
                usage = obj.get("usage", {})
                result.input_tokens = usage.get("input_tokens", result.input_tokens)
                result.output_tokens = usage.get("output_tokens", result.output_tokens)
        return result


class CodexHarness(Harness):
    """Codex CLI, authenticated by a bind-mounted ``~/.codex`` credential dir."""

    name = "codex"

    def configure_wd(self, wd: Path, prompt: str) -> None:
        super().configure_wd(wd, prompt)
        # Codex registers the MCP server via -c overrides in the launch script;
        # only the skills need copying. https://developers.openai.com/codex/skills
        self._copy_skills(wd, ".agents/skills")

    def auth_spec(self) -> AuthSpec:
        # Mounted rw because codex refreshes its access token mid-session.
        codex_dir = Path.home() / ".codex"
        if not codex_dir.exists():
            raise RuntimeError("codex harness requires ~/.codex from `codex login`")
        return AuthSpec(home_dirs=[(codex_dir, ".codex")])

    def _agent_command(self) -> str:
        return self._render((_SCRIPTS / "codex_agent.sh").read_text())

    def _parse_lines(self, lines: list[str]) -> HarnessRunResult:
        """Parse ``codex exec --json`` output."""
        result = HarnessRunResult()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "turn.completed":
                continue
            usage = event.get("usage") or {}
            it = (
                usage.get("input_tokens")
                or usage.get("inputTokens")
                or usage.get("prompt_tokens")
                or 0
            )
            ot = (
                usage.get("output_tokens")
                or usage.get("outputTokens")
                or usage.get("completion_tokens")
                or 0
            )
            if isinstance(it, int):
                result.input_tokens += it
            if isinstance(ot, int):
                result.output_tokens += ot
            sr = event.get("stop_reason") or event.get("finish_reason")
            if isinstance(sr, str):
                result.stop_reason = sr
        # Codex does not surface USD; left as None so the prover fills from tokens.
        return result


def _infer_provider(model: str) -> str:
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("deepseek"):
        return "deepseek"
    if model.startswith("gemini"):
        return "google"
    return "openai"


class OpenCodeHarness(Harness):
    """OpenCode CLI, authenticated by a provider API key forwarded from the host."""

    name = "opencode"

    def __init__(
        self, model: str, effort: str = "medium", provider: str | None = None
    ) -> None:
        super().__init__(model, effort)
        self.provider = provider or _infer_provider(model)

    def configure_wd(self, wd: Path, prompt: str) -> None:
        super().configure_wd(wd, prompt)
        # opencode.json configures the model provider + MCP server.
        (wd / "opencode.json").write_text(json.dumps(self._opencode_config(), indent=2))
        self._copy_skills(wd, ".agents/skills")

    def _opencode_config(self) -> dict[str, Any]:
        options: dict[str, Any]
        if self.provider == "anthropic":
            options = {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": self.effort},
            }
        else:
            options = {"reasoningEffort": self.effort}
        return {
            "$schema": "https://opencode.ai/config.json",
            "provider": {self.provider: {"models": {self.model: {"options": options}}}},
            "mcp": {
                "lean-lsp": {
                    "type": "local",
                    "command": ["uvx", "lean-lsp-mcp"],
                    "enabled": True,
                }
            },
        }

    def auth_spec(self) -> AuthSpec:
        env = [
            key
            for key in (
                "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY",
                "GOOGLE_API_KEY",
                "DEEPSEEK_API_KEY",
            )
            if key in os.environ
        ]
        return AuthSpec(env=env)

    def _agent_command(self) -> str:
        template = (_SCRIPTS / "opencode_agent.sh").read_text()
        return template.replace("<<PROVIDER>>", self.provider).replace(
            "<<MODEL>>", self.model
        )

    def _parse_lines(self, lines: list[str]) -> HarnessRunResult:
        """Parse ``opencode run --format json`` output."""

        def _as_int(x: Any) -> int:
            return x if isinstance(x, int) else 0

        result = HarnessRunResult()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "step_finish":
                continue
            part = event.get("part") or {}
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            result.input_tokens += (
                _as_int(tokens.get("input"))
                + _as_int(cache.get("write"))
                + _as_int(cache.get("read"))
            )
            result.output_tokens += _as_int(tokens.get("output"))
            c = part.get("cost")
            if isinstance(c, (int, float)):
                result.cost_usd = (result.cost_usd or 0.0) + float(c)
            r = part.get("reason")
            if isinstance(r, str):
                result.stop_reason = r
        return result


#: Harness registry selected by ``AgentProverConfig.harness``.
HARNESSES: dict[str, type[Harness]] = {
    ClaudeCodeHarness.name: ClaudeCodeHarness,
    CodexHarness.name: CodexHarness,
    OpenCodeHarness.name: OpenCodeHarness,
}
