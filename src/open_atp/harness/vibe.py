"""Mistral Vibe CLI harness (drives the builtin ``lean`` agent / Leanstral)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from open_atp.harness._paths import _SCRIPTS, _VIBE_ASSETS
from open_atp.harness.base import Harness, HarnessRunResult


class VibeHarness(Harness):
    """Mistral Vibe CLI driving its builtin ``lean`` agent (Leanstral) in a sandbox.

    Vibe's ``lean`` agent *is* Leanstral: ``vibe -p ... --agent lean`` pins the model
    via the builtin agent profile (there is no ``--model`` flag), but that pin is a
    deprecated Leanstral and cannot be changed. So the ``lean-labs`` profile (vendored
    under ``assets/vibe/``) mirrors the same Lean scaffold while templating in a chosen
    model: it carries ``<<MODEL>>`` and the harness substitutes :attr:`model` at
    :meth:`stage_wd` time -- so the model is a knob (default the ``labs-leanstral-1-5``
    lab model) just like the other harnesses' ``--model``. The selected profile is
    named by :attr:`agent`. Reaching a Labs model requires Lab Model access enabled by
    a Mistral org admin.

    Two things differ from the other harnesses:

    * **VIBE_HOME is workdir-local.** ``vibe_agent.sh`` exports
      ``VIBE_HOME=$PWD/.vibe`` so vibe's config (which un-gates the builtin ``lean``
      agent), the vendored stand-in agent, and the per-session log all live under the
      workdir and sync back out with it.
    * **Cost comes from the session log, not stdout.** ``--output streaming`` carries
      only conversation messages -- no token/cost totals. Those live in vibe's
      per-session ``meta.json``; :meth:`parse_result` reads it from the synced-back
      log dir.

    Parameters
    ----------
    model : str
        Model templated into the ``lean-labs`` profile (vibe has no ``--model`` flag).
        Ignored by the builtin ``lean`` agent, which pins its own (deprecated)
        Leanstral. Default ``"labs-leanstral-1-5"``.
    effort : str
        Reasoning-effort level. Default ``"high"``.
    agent : str, optional
        Which vibe agent profile to drive: ``"lean"`` (builtin, deprecated Leanstral)
        or the model-templated ``"lean-labs"``. Default ``"lean-labs"``.
    max_turns : int, optional
        ``vibe -p`` turn guard; ``None`` (default) leaves it unset.
    max_price : float, optional
        ``vibe -p`` price guard; ``None`` (default) leaves it unset.
    mistral_api_key : str, optional
        Mistral La Plateforme key forwarded as ``MISTRAL_API_KEY``. ``None`` (default)
        reads it from the host env var; resolution fails if neither is set.

    Examples
    --------
    >>> from open_atp.harness import VibeHarness
    >>> harness = VibeHarness()
    >>> harness.name
    'vibe'
    >>> harness.agent
    'lean-labs'

    With the key supplied explicitly, :meth:`agent_auth` forwards it as
    ``MISTRAL_API_KEY`` without reading the host environment:

    >>> harness = VibeHarness(mistral_api_key="msk-fake")
    >>> harness.agent_auth().env
    {'MISTRAL_API_KEY': 'msk-fake'}
    """

    name = "vibe"

    #: Workdir-relative VIBE_HOME (matches the export in ``vibe_agent.sh``).
    VIBE_HOME_DIR = ".vibe"

    #: Skills mount under VIBE_HOME's *user* skills dir, which loads regardless of
    #: project-folder trust. The other harnesses use project dirs (``.claude/skills`` /
    #: ``.agents/skills``), which ``vibe -p`` gates behind ``--trust``, so the
    #: VIBE_HOME-relative spot is the parity-preserving one.
    skills_dest = f"{VIBE_HOME_DIR}/skills"

    def __init__(
        self,
        *,
        model: str = "labs-leanstral-1-5",
        effort: str = "high",
        agent: str = "lean-labs",
        max_turns: int | None = None,
        max_price: float | None = None,
        mistral_api_key: str | None = None,
    ) -> None:
        super().__init__(model=model, effort=effort)
        self.agent = agent
        self.max_turns = max_turns
        self.max_price = max_price
        self._mistral_api_key = mistral_api_key
        #: Set in :meth:`stage_wd`; where :meth:`parse_result` looks for session logs.
        self._session_log_dir: Path | None = None

    def _required_env(self) -> dict[str, str]:
        # The lean agent's provider reads MISTRAL_API_KEY from the process env
        # (api_key_env_var in lean-standin.toml); forward it into the sandbox.
        key = self._mistral_api_key or os.environ.get("MISTRAL_API_KEY")
        if not key:
            raise RuntimeError(
                "vibe harness requires MISTRAL_API_KEY (a Mistral La Plateforme key)"
            )
        return {"MISTRAL_API_KEY": key}

    def stage_wd(self, wd: Path) -> None:
        super().stage_wd(wd)
        # Workdir-local VIBE_HOME: a minimal config that un-gates the builtin `lean`
        # agent, plus the vendored model-templated `lean-labs` profile. Session logs
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
        # The vendored profile lives as ``<agent>.toml`` (``lean-labs``). Vibe has no
        # ``--model`` flag, so the model is templated into the profile: ``_render``
        # substitutes ``<<MODEL>>`` with ``self.model`` (default the lab Leanstral) as
        # it writes the copy into the workdir. The builtin ``lean`` agent ships with
        # vibe and pins its own (deprecated) model, so it needs no profile.
        if self.agent != "lean":
            profile = _VIBE_ASSETS / f"{self.agent}.toml"
            (agents_dir / profile.name).write_text(self._render(profile.read_text()))
        self._session_log_dir = vibe_home / "logs" / "session"

    def _agent_command(self) -> str:
        template = (_SCRIPTS / "vibe_agent.sh").read_text()
        extra = ""
        if self.max_turns is not None:
            extra += f" \\\n    --max-turns {int(self.max_turns)}"
        if self.max_price is not None:
            extra += f" \\\n    --max-price {self.max_price}"
        return template.replace("<<AGENT>>", self.agent).replace("<<EXTRA>>", extra)

    def parse_result(self, lines: list[str]) -> HarnessRunResult:
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
