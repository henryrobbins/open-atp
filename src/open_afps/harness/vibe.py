"""Mistral Vibe CLI harness (drives the builtin ``lean`` agent / Leanstral)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from open_afps.harness._paths import _SCRIPTS, _VIBE_ASSETS
from open_afps.harness.base import AuthSpec, Harness, HarnessRunResult
from open_afps.harness.bundles import AssetBundle


class VibeHarness(Harness):
    """Mistral Vibe CLI driving its builtin ``lean`` agent (Leanstral) in a sandbox.

    Vibe's ``lean`` agent *is* Leanstral: ``vibe -p ... --agent lean`` pins the model
    to ``leanstral`` via the builtin agent profile (there is no ``--model`` flag). The
    bare ``lean`` profile is Labs-gated, so the ``lean-standin`` stand-in (vendored
    under ``assets/vibe/``) runs the same Lean scaffold on a non-Labs model until Labs
    access is enabled. The selected profile is named by :attr:`agent`. Since vibe has
    no ``--model`` flag, the stand-in profile templates ``<<MODEL>>`` and the harness
    substitutes :attr:`model` into it at :meth:`configure_wd` time -- so the model is a
    knob (default Magistral) just like the other harnesses' ``--model``.

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
        #
        # ``tool_timeout_sec`` mirrors the 180s opencode fix: the first
        # ``lean_diagnostic_messages`` call starts ``lake serve`` and loads the file's
        # full Mathlib import closure into the LSP, which blows through vibe's 60s
        # default tool timeout on a cold, few-CPU Modal sandbox (the
        # ``test_lean_lsp_mcp[vibe-modal]`` probe timed out at exactly 60s). Vibe's
        # field is in *seconds* (opencode's is ms).
        (vibe_home / "config.toml").write_text(
            'installed_agents = ["lean"]\n'
            "bypass_tool_permissions = true\n\n"
            "[session_logging]\nenabled = true\n\n"
            "[[mcp_servers]]\n"
            'transport = "stdio"\n'
            'name = "lean-lsp"\n'
            'command = "lean-lsp-mcp"\n'
            "tool_timeout_sec = 180\n"
        )
        # The vendored stand-in profile lives as ``<agent>.toml`` (``lean-standin``).
        # Vibe has no ``--model`` flag, so the model is templated into the profile:
        # ``_render`` substitutes ``<<MODEL>>`` with ``self.model`` (default Magistral)
        # as it writes the copy into the workdir. The builtin ``lean`` agent (real
        # Leanstral) ships with vibe and pins its own model, so it needs no profile.
        if self.agent != "lean":
            profile = _VIBE_ASSETS / f"{self.agent}.toml"
            (agents_dir / profile.name).write_text(self._render(profile.read_text()))
        # Mount the selected bundle's skills (by default the vendored ``lean-proof``
        # skill) under VIBE_HOME/skills -- vibe's *user* skills dir, which loads
        # regardless of project-folder trust. The other harnesses copy into
        # ``.claude/skills`` / ``.agents/skills`` (project dirs); those are gated
        # behind ``--trust`` in ``vibe -p``, so the VIBE_HOME-relative location is
        # the parity-preserving spot.
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

    def collect_logs(self, wd: Path, logs_dir: Path) -> None:
        # Vibe's per-session record (message log + meta.json with cost) lives under the
        # workdir-local VIBE_HOME (``.vibe/logs``). Move the whole logs tree out to
        # ``logs/vibe-session`` -- preserving its internal structure but dropping the
        # ``.vibe/logs`` prefix -- so it leaves the proof project. The rest of VIBE_HOME
        # (config.toml, agents/) is scaffolding and stays in the workdir.
        src = wd / self.VIBE_HOME_DIR / "logs"
        if src.is_dir():
            logs_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(logs_dir / "vibe-session"))

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
