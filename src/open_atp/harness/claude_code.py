"""Claude Code CLI harness."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from open_atp.harness._catalog import resolve_plugin
from open_atp.harness._paths import _MCP_JSON, _SCRIPTS
from open_atp.harness.base import Harness, HarnessRunResult


class ClaudeCodeHarness(Harness):
    """Claude Code CLI, authenticated by a long-lived ``CLAUDE_CODE_OAUTH_TOKEN``.

    Claude Code is the only harness that loads plugins, so they live here rather than
    on the prover's shared skills list.

    Parameters
    ----------
    model : str
        Model id the agent runs. Default ``"claude-opus-4-8"``.
    effort : str
        Reasoning-effort level. Default ``"high"``.
    plugins : list[str], optional
        Claude Code plugins to load, each a name (resolved from the vendored
        ``lean4-skills`` catalog) or a full path to a ``.claude-plugin/plugin.json``
        tree. Default ``["lean4"]``; an empty list loads none.
    oauth_token : str, optional
        The ``CLAUDE_CODE_OAUTH_TOKEN`` (from ``claude setup-token``) to forward into
        the sandbox. ``None`` (default) reads it from the host
        ``CLAUDE_CODE_OAUTH_TOKEN`` env var; resolution fails if neither is set.
    env : dict[str, str], optional
        Literal env vars forwarded verbatim into the sandbox; win over resolved
        credentials on a key clash. Default none.
    optional_env : tuple[str, ...], optional
        Best-effort credential names forwarded from the host when present. Default none.
    """

    name = "claude_code"

    skills_dest = ".claude/skills"

    #: Where plugin dirs are staged in the workdir (the launch script's
    #: ``--plugin-dir`` flags reference this, so the two must agree).
    PLUGINS_DIR = ".plugins"

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-8",
        effort: str = "high",
        plugins: list[str] | None = None,
        oauth_token: str | None = None,
        env: dict[str, str] | None = None,
        optional_env: tuple[str, ...] = (),
    ) -> None:
        super().__init__(model=model, effort=effort, env=env, optional_env=optional_env)
        # plugins documented as a class Parameter/Attribute above.
        self.plugins = plugins if plugins is not None else ["lean4"]
        self._oauth_token = oauth_token

    def stage(self, wd: Path) -> None:
        super().stage(wd)
        # Project-scope MCP config (passed via --mcp-config) and plugins.
        shutil.copy2(_MCP_JSON, wd / ".mcp.json")
        self._copy_plugins(wd)

    def _resolved_plugins(self) -> list[Path]:
        """``self.plugins`` (names or paths) resolved to plugin source dirs."""
        return [resolve_plugin(p) for p in self.plugins]

    def _copy_plugins(self, wd: Path) -> None:
        """Stage each configured plugin under ``wd/.plugins/<name>``.

        Claude is the only harness that consumes plugins (so they live on this harness,
        not the shared skills list); the launch script
        loads them with ``--plugin-dir`` (see :meth:`_plugin_flags`). Plugins are copied
        *into* the workdir (not referenced from the host vendor tree) so they sync
        into the sandbox with everything else.
        """
        for plugin in self._resolved_plugins():
            shutil.copytree(
                plugin, wd / self.PLUGINS_DIR / plugin.name, dirs_exist_ok=True
            )

    def _plugin_flags(self) -> str:
        """``--plugin-dir`` flags (one per plugin) appended to the launch command.

        Empty when no plugins; otherwise a leading line-continuation so it grafts
        onto the end of the ``claude -p ...`` invocation.
        """
        return "".join(
            f" \\\n    --plugin-dir {self.PLUGINS_DIR}/{p.name}"
            for p in self._resolved_plugins()
        )

    def _static_env(self) -> dict[str, str]:
        # Lets bypassPermissions run non-interactively in the container.
        env = {"IS_SANDBOX": "1"}
        # Plugin-provided subagents (e.g. lean4's sorry-filler-deep) are only
        # dispatchable in a headless `-p` run with subagent forking enabled.
        if self.plugins:
            env["CLAUDE_CODE_FORK_SUBAGENT"] = "1"
        return env

    def _required_env(self) -> dict[str, str]:
        # A long-lived token (from `claude setup-token`) bills against a Claude
        # subscription rather than at the higher per-API-call rate.
        token = self._oauth_token or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if not token:
            raise RuntimeError(
                "claude_code harness requires CLAUDE_CODE_OAUTH_TOKEN"
                " from `claude setup-token`"
            )
        return {"CLAUDE_CODE_OAUTH_TOKEN": token}

    def _agent_command(self) -> str:
        template = self._render((_SCRIPTS / "claude_code_agent.sh").read_text())
        return template.replace("<<PLUGIN_FLAGS>>", self._plugin_flags())

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
