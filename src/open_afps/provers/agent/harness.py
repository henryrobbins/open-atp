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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

_ASSETS = Path(__file__).parent / "assets"
_SCRIPTS = _ASSETS / "scripts"
_SKILLS = _ASSETS / "skills"
_MCP_JSON = _ASSETS / "configs" / "mcp.json"
#: Vibe-specific assets: the vendored stand-in agent profile (lean scaffold on a
#: non-Labs model) copied into the sandbox's per-workdir VIBE_HOME/agents.
_VIBE_ASSETS = _ASSETS / "vibe"

#: Files the harness writes into the workdir; named so they never collide with a
#: project's own sources.
SCRIPT_FILE = "agent.sh"
PROMPT_FILE = "agent_prompt.txt"


def _vendor_numina_dir() -> Path:
    """Locate the vendored Numina bundle in both wheel and source layouts."""
    candidates = [
        # Built wheel: force-included at open_afps/vendor/numina (see pyproject).
        Path(__file__).parents[2] / "vendor" / "numina",
        # Source checkout / editable install: vendor/ at the repo root.
        Path(__file__).parents[4] / "vendor" / "numina",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


@dataclass(frozen=True)
class AssetBundle:
    """A selectable set of agent assets mounted into the workdir.

    Attributes
    ----------
    name:
        Bundle identifier matching ``AgentProverConfig.assets``.
    skills_dir:
        Directory whose contents become the agent's skills (copied into the
        harness's skills location, e.g. ``.claude/skills``).
    prompt_file:
        Optional default system prompt for the bundle, used when the task carries
        no explicit ``instructions``.
    extra_dirs:
        Additional ``(src_dir, dest_relative_to_workdir)`` trees to copy in (e.g.
        Numina's coordinator/subagent prompts under ``.claude/prompts``).
    """

    name: str
    skills_dir: Path
    prompt_file: Path | None = None
    extra_dirs: tuple[tuple[Path, str], ...] = ()

    def default_prompt(self) -> str | None:
        if self.prompt_file is not None and self.prompt_file.is_file():
            return self.prompt_file.read_text()
        return None


#: The built-in bundle: the generic ``filling-sorrys`` skill, no default prompt.
DEFAULT_BUNDLE = AssetBundle(name="default", skills_dir=_SKILLS)


def _numina_bundle() -> AssetBundle:
    root = _vendor_numina_dir()
    return AssetBundle(
        name="numina",
        skills_dir=root / "skills",
        prompt_file=root / "prompts" / "main_entry.md",
        # The coordinator prompt tells the agent to read its subagent prompts from
        # .claude/prompts/subagent_prompts/, so stage the whole prompt tree there.
        extra_dirs=((root / "prompts", ".claude/prompts"),),
    )


#: Asset-bundle registry selected by ``AgentProverConfig.assets``.
BUNDLES: dict[str, Callable[[], AssetBundle]] = {
    "default": lambda: DEFAULT_BUNDLE,
    "numina": _numina_bundle,
}


def resolve_bundle(name: str) -> AssetBundle:
    """Resolve an ``assets`` name to its :class:`AssetBundle`."""
    try:
        return BUNDLES[name]()
    except KeyError:
        raise ValueError(
            f"unknown asset bundle {name!r}; known: {sorted(BUNDLES)}"
        ) from None


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
    #: Final ``type:"result"`` subtype (Claude Code: ``success`` /
    #: ``error_max_turns`` / ``error_during_execution``). Used by NuminaProver's
    #: round loop to decide continue-vs-stop when no END_REASON marker is present.
    subtype: str | None = None
    #: The agent's final result text (Claude Code's ``result`` field), where the
    #: Numina coordinator prints its ``END_REASON:<reason>`` marker.
    result_text: str | None = None


class Harness(ABC):
    """Base class for an agent CLI harness."""

    name: ClassVar[str]

    def __init__(
        self, model: str, effort: str = "medium", assets: AssetBundle | None = None
    ) -> None:
        self.model = model
        self.effort = effort
        self.assets = assets or DEFAULT_BUNDLE

    @classmethod
    def from_config(cls, config: Any, *, assets: AssetBundle | None = None) -> Harness:
        """Build a harness from an :class:`AgentProverConfig`.

        The default reads only the shared ``model``/``effort`` knobs; harnesses with
        extra config (e.g. :class:`VibeHarness`'s agent/turn/price) override this.
        """
        return cls(config.model, config.effort, assets=assets)

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
        self._copy_extra_dirs(wd)

    def _copy_extra_dirs(self, wd: Path) -> None:
        """Copy the bundle's extra asset trees (e.g. Numina's prompts) into ``wd``."""
        for src, dest in self.assets.extra_dirs:
            target = wd / dest
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, target, dirs_exist_ok=True)

    def parse(self, lines: list[str]) -> HarnessRunResult:
        """Parse the agent's streamed JSON lines into a :class:`HarnessRunResult`."""
        return self._parse_lines(lines)

    def _copy_skills(self, wd: Path, dest: str) -> None:
        """Copy the selected bundle's skills into ``wd/<dest>``."""
        target = wd / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.assets.skills_dir, target, dirs_exist_ok=True)

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
                result.subtype = obj.get("subtype")
                rt = obj.get("result")
                result.result_text = rt if isinstance(rt, str) else None
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
        self,
        model: str,
        effort: str = "medium",
        provider: str | None = None,
        assets: AssetBundle | None = None,
    ) -> None:
        super().__init__(model, effort, assets)
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
                    "command": ["lean-lsp-mcp"],
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


class VibeHarness(Harness):
    """Mistral Vibe CLI driving its builtin ``lean`` agent (Leanstral) in a sandbox.

    Vibe's ``lean`` agent *is* Leanstral: ``vibe -p ... --agent lean`` pins the model
    to ``leanstral`` via the builtin agent profile (there is no ``--model`` flag). The
    bare ``lean`` profile is Labs-gated, so the ``lean-devstral`` stand-in (vendored
    under ``assets/vibe/``) runs the same Lean scaffold on a non-Labs model until Labs
    access is enabled. The selected profile is named by :attr:`agent`.

    Two things differ from the other harnesses:

    * **VIBE_HOME is workdir-local.** ``vibe_agent.sh`` exports
      ``VIBE_HOME=$PWD/.vibe`` so vibe's config (which un-gates the builtin ``lean``
      agent), the vendored stand-in agent, and the per-session log all live under the
      workdir and sync back out with it.
    * **Cost comes from the session log, not stdout.** ``--output streaming`` carries
      only conversation messages -- no token/cost totals. Those live in vibe's
      per-session ``meta.json``; :meth:`parse` reads it from the synced-back log dir.
    """

    name = "vibe"

    #: Workdir-relative VIBE_HOME (matches the export in ``vibe_agent.sh``).
    VIBE_HOME_DIR = ".vibe"

    def __init__(
        self,
        model: str,
        effort: str = "medium",
        *,
        agent: str = "lean",
        max_turns: int | None = None,
        max_price: float | None = None,
        assets: AssetBundle | None = None,
    ) -> None:
        super().__init__(model, effort, assets)
        self.agent = agent
        self.max_turns = max_turns
        self.max_price = max_price
        #: Set in :meth:`configure_wd`; where :meth:`parse` looks for session logs.
        self._session_log_dir: Path | None = None

    @classmethod
    def from_config(cls, config: Any, *, assets: AssetBundle | None = None) -> Harness:
        return cls(
            config.model,
            config.effort,
            agent=getattr(config, "agent", "lean"),
            max_turns=getattr(config, "max_turns", None),
            max_price=getattr(config, "max_price", None),
            assets=assets,
        )

    def auth_spec(self) -> AuthSpec:
        # The lean agent's provider reads MISTRAL_API_KEY from the process env
        # (api_key_env_var); forward it from the host into the sandbox.
        if "MISTRAL_API_KEY" not in os.environ:
            raise RuntimeError(
                "vibe harness requires MISTRAL_API_KEY (a Mistral La Plateforme key)"
            )
        return AuthSpec(env=["MISTRAL_API_KEY"])

    def configure_wd(self, wd: Path, prompt: str) -> None:
        super().configure_wd(wd, prompt)
        # Workdir-local VIBE_HOME: a minimal config that un-gates the builtin `lean`
        # agent, plus the vendored stand-in agent profile. Session logs (with cost)
        # default to VIBE_HOME/logs/session, so they land here and sync back out.
        vibe_home = wd / self.VIBE_HOME_DIR
        agents_dir = vibe_home / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        # ``bypass_tool_permissions`` is the *only* thing that ungates mutating tools
        # (``edit``, ``write_file``) in ``vibe -p`` programmatic mode: there is no
        # approval callback, so any tool that resolves to ``ASK`` is answered "Tool
        # execution not permitted" and silently skipped (agent_loop.py short-circuits
        # to EXECUTE when this is set). The ``--auto-approve``/``--yolo`` CLI flag can't
        # be used instead -- it forces ``--agent auto-approve``, discarding the ``lean``
        # scaffold. The builtin ``lean`` profile sets no permission bypass, so without
        # this even the real Leanstral cannot write its proof. Set it on the base config
        # so it applies to ``--agent lean`` and the vendored stand-ins alike.
        #
        # ``mcp_servers`` wires in lean-lsp-mcp (the same server the other harnesses
        # mount via .mcp.json) so the agent actually gets the compile/diagnostic
        # feedback loop the prompt assumes. Vibe publishes its tools as
        # ``lean-lsp_<tool>``; discovery failure is non-fatal (logged, agent runs
        # without them) so this is safe even if the server is missing.
        (vibe_home / "config.toml").write_text(
            'installed_agents = ["lean"]\n'
            "bypass_tool_permissions = true\n\n"
            "[session_logging]\nenabled = true\n\n"
            "[[mcp_servers]]\n"
            'transport = "stdio"\n'
            'name = "lean-lsp"\n'
            'command = "lean-lsp-mcp"\n'
        )
        # Vendored stand-in profiles live as ``<agent>.toml`` (e.g. ``lean-devstral``,
        # ``lean-magistral``). The builtin ``lean`` agent (real Leanstral) ships with
        # vibe, so it needs no profile copied.
        if self.agent != "lean":
            profile = _VIBE_ASSETS / f"{self.agent}.toml"
            shutil.copy2(profile, agents_dir / profile.name)
        # Mount the selected bundle's skills (the generic ``filling-sorrys`` skill)
        # under VIBE_HOME/skills -- vibe's *user* skills dir, which loads regardless
        # of project-folder trust. The other harnesses copy into ``.claude/skills`` /
        # ``.agents/skills`` (project dirs); those are gated behind ``--trust`` in
        # ``vibe -p``, so the VIBE_HOME-relative location is the parity-preserving spot.
        self._copy_skills(wd, f"{self.VIBE_HOME_DIR}/skills")
        self._session_log_dir = vibe_home / "logs" / "session"

    def _agent_command(self) -> str:
        template = (_SCRIPTS / "vibe_agent.sh").read_text()
        extra = ""
        if self.max_turns is not None:
            extra += f" \\\n    --max-turns {int(self.max_turns)}"
        if self.max_price is not None:
            extra += f" \\\n    --max-price {self.max_price}"
        return template.replace("<<AGENT>>", self.agent).replace("<<EXTRA>>", extra)

    def parse(self, lines: list[str]) -> HarnessRunResult:
        # Final assistant text + stop signal come from the NDJSON stream; token/cost
        # totals come from the session log, which the stream does not carry.
        result = self._parse_lines(lines)
        stats = self._read_session_stats()
        if stats is not None:
            cost = stats.get("session_cost")
            if isinstance(cost, (int, float)):
                result.cost_usd = float(cost)
            result.input_tokens = int(stats.get("session_prompt_tokens", 0) or 0)
            result.output_tokens = int(stats.get("session_completion_tokens", 0) or 0)
        return result

    def _read_session_stats(self) -> dict[str, Any] | None:
        """Load ``stats`` from the most recent session ``meta.json``, if present."""
        if self._session_log_dir is None or not self._session_log_dir.is_dir():
            return None
        metas = sorted(self._session_log_dir.glob("*/meta.json"))
        if not metas:
            return None
        try:
            data = json.loads(metas[-1].read_text())
        except (json.JSONDecodeError, OSError):
            return None
        stats = data.get("stats")
        return stats if isinstance(stats, dict) else None

    def _parse_lines(self, lines: list[str]) -> HarnessRunResult:
        """Pull the final assistant message out of vibe's NDJSON message stream."""
        result = HarnessRunResult()
        for line in lines:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("role") == "assistant":
                content = obj.get("content")
                if isinstance(content, str) and content:
                    result.result_text = content
        return result


class AxProverHarness(Harness):
    """ax-prover-base (LangGraph Lean agent), driven by ``ax-prover prove`` in-sandbox.

    ax-prover is a self-contained proving agent (its own
    proposer->builder->reviewer->memory loop) that edits the target ``.lean`` file
    in place. It slots in as a harness rather than a standalone prover because
    :meth:`AgentProver.prove` already supplies everything around the edges (staging,
    snapshot/diff, sandbox run, key forwarding) and the shared ``Verifier`` -- not
    ax-prover's own reviewer -- remains the source of truth for compile/sorry/axiom.

    Two things differ from the CLI harnesses (it mirrors :class:`VibeHarness` here):

    * **Config lives in a workdir YAML, not flags.** :meth:`configure_wd` writes
      ``axprover.yaml`` selecting the model/effort/iterations; it layers on top of
      ax-prover's bundled ``default.yaml`` (auto-prepended by the CLI), so it only
      needs to override the deltas.
    * **Cost is not on stdout.** ax-prover streams human-readable logs, and its ``-o``
      JSON carries only ``{success, error, summary}``. Token totals come from the
      per-target ``ax_usage.*.json`` files written by the launch script (see
      ``axprover_agent.sh``); :meth:`parse` sums them and leaves ``cost_usd`` ``None``
      so the prover converts tokens->USD via the fallback table, exactly like
      :class:`CodexHarness`. Emitting those usage files requires a small upstream
      ax-prover patch (see ``AX_PROVER_HARNESS_PLAN.md`` step 3); until it lands the
      files are absent and the run reports zero tokens / no cost.
    """

    name = "axprover"

    #: open-afps provider name -> ax-prover's LangChain ``provider:model`` prefix.
    _AX_PROVIDER_PREFIX: ClassVar[dict[str, str]] = {
        "anthropic": "anthropic",
        "openai": "openai",
        "google": "google_genai",
        "deepseek": "deepseek",
    }

    def __init__(
        self,
        model: str,
        effort: str = "medium",
        *,
        max_iterations: int | None = None,
        assets: AssetBundle | None = None,
    ) -> None:
        super().__init__(model, effort, assets)
        self.max_iterations = max_iterations
        #: Set in :meth:`configure_wd`; where :meth:`parse` looks for usage files.
        self._wd: Path | None = None

    @classmethod
    def from_config(cls, config: Any, *, assets: AssetBundle | None = None) -> Harness:
        return cls(
            config.model,
            config.effort,
            max_iterations=getattr(config, "max_iterations", None),
            assets=assets,
        )

    def auth_spec(self) -> AuthSpec:
        # Raw provider keys, exactly like OpenCodeHarness; ax-prover reads them from
        # the process env (ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY).
        env = [
            key
            for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY")
            if key in os.environ
        ]
        if not env:
            raise RuntimeError(
                "axprover harness requires one of ANTHROPIC_API_KEY / "
                "OPENAI_API_KEY / GOOGLE_API_KEY"
            )
        return AuthSpec(env=env)

    def configure_wd(self, wd: Path, prompt: str) -> None:
        # The free-text prompt is ignored: ax-prover has its own prompts. We still let
        # the base write agent.sh + agent_prompt.txt for a uniform contract.
        super().configure_wd(wd, prompt)
        (wd / "axprover.yaml").write_text(self._render_config())
        self._wd = wd

    def _ax_model(self) -> str:
        """``self.model`` as ax-prover's ``provider:model`` string."""
        provider = _infer_provider(self.model)
        prefix = self._AX_PROVIDER_PREFIX.get(provider, "openai")
        return f"{prefix}:{self.model}"

    def _provider_config(self) -> dict[str, Any]:
        """Provider-specific LLM kwargs mapping ``effort`` to each API's knob."""
        provider = _infer_provider(self.model)
        if provider == "anthropic":
            return {
                "temperature": 1.0,  # required when thinking is enabled
                "max_tokens": None,
                "effort": self.effort,
                "thinking": {"type": "adaptive"},
            }
        if provider == "google":
            return {
                "temperature": 1.0,
                "max_tokens": None,
                "include_thoughts": True,
                "thinking_level": self.effort,
            }
        return {  # openai / deepseek (OpenAI-compatible)
            "temperature": None,
            "max_tokens": None,
            "reasoning": {"effort": self.effort},
        }

    def _render_config(self) -> str:
        """The ``axprover.yaml`` overrides layered over ax-prover's ``default.yaml``.

        JSON is valid YAML, so we emit JSON to avoid a YAML dependency. Only the
        deltas are set: ``prover_llm`` (which the bundled config's ``memory_config``
        and ``summarize_output`` interpolate from) and, when capped, ``max_iterations``.
        The bundled ``proposer_tools`` (lean + web search) are left untouched -- a
        missing TAVILY_API_KEY or blocked egress degrades a tool to a no-op rather
        than failing the run.
        """
        prover: dict[str, Any] = {
            "prover_llm": {
                "model": self._ax_model(),
                "provider_config": self._provider_config(),
            }
        }
        if self.max_iterations is not None:
            prover["max_iterations"] = int(self.max_iterations)
        return json.dumps({"prover": prover}, indent=2)

    def _agent_command(self) -> str:
        # No <<MODEL>>/<<EFFORT>> substitution: those live in axprover.yaml.
        return (_SCRIPTS / "axprover_agent.sh").read_text()

    def parse(self, lines: list[str]) -> HarnessRunResult:
        # Tokens come from the per-target usage files (the stream has none); cost is
        # left None so the prover derives USD from the token table (like Codex).
        result = self._parse_lines(lines)
        if self._wd is not None and self._wd.is_dir():
            for path in sorted(self._wd.glob("ax_usage.*.json")):
                try:
                    data = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                result.input_tokens += int(
                    data.get("input_tokens", data.get("prompt_tokens", 0)) or 0
                )
                result.output_tokens += int(
                    data.get("output_tokens", data.get("completion_tokens", 0)) or 0
                )
        return result

    def _parse_lines(self, lines: list[str]) -> HarnessRunResult:
        # ax-prover's stdout is human-readable logs, not a JSON event stream; keep the
        # last non-empty line as result text for debugging and read tokens elsewhere.
        result = HarnessRunResult()
        for line in lines:
            stripped = line.strip()
            if stripped:
                result.result_text = stripped
        return result


#: Harness registry selected by ``AgentProverConfig.harness``.
HARNESSES: dict[str, type[Harness]] = {
    ClaudeCodeHarness.name: ClaudeCodeHarness,
    CodexHarness.name: CodexHarness,
    OpenCodeHarness.name: OpenCodeHarness,
    VibeHarness.name: VibeHarness,
    AxProverHarness.name: AxProverHarness,
}
