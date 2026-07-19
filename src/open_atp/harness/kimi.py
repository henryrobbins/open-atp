"""Kimi Code CLI harness (Moonshot ``kimi``, OAuth-authenticated)."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from open_atp.harness._paths import _SCRIPTS
from open_atp.harness.base import Harness, HarnessRunResult

log = logging.getLogger("open_atp")


class KimiHarness(Harness):
    """Moonshot Kimi Code CLI, authenticated by its file-stored OAuth credential.

    Kimi Code logs in via a device-code flow (``kimi login``) and stores the
    resulting OAuth tokens as files under its data directory (default
    ``~/.kimi-code``): ``config.toml`` (provider + model registry, wiring the
    ``managed:kimi-code`` provider to the file-stored token), ``credentials/`` (the
    access/refresh tokens), and ``device_id``. There is no API-key env var to
    forward, so the harness stages those files rather than resolving a credential.

    Like :class:`~open_atp.harness.VibeHarness`, the whole home is workdir-local:
    ``kimi_agent.sh`` exports ``KIMI_CODE_HOME=$PWD/.kimi-home`` so the credential,
    the user-scope ``mcp.json`` (lean-lsp) and skills dir, and the per-session wire
    log all live under the workdir. This isolates concurrent runs, syncs the
    telemetry back out on both backends, and sidesteps Kimi's project-scope config
    discovery (which anchors to the nearest ``.git`` and would miss a bare lake
    project) by landing everything at user scope under ``KIMI_CODE_HOME``.

    Cost comes from the session log, not stdout: ``--output-format stream-json``
    carries only messages and tool calls -- no token totals. Those live in Kimi's
    per-session ``wire.jsonl`` (``usage.record`` events); :meth:`parse_result` reads
    it from the synced-back home. Kimi bills a flat subscription rate and reports no
    USD, so ``cost_usd`` is left ``None``.

    Parameters
    ----------
    model : str
        Model alias the agent runs (a ``config.toml`` alias). Default
        ``"kimi-code/kimi-for-coding"`` (K2.7 Coding).
    effort : str
        Reasoning-effort level. Default ``"high"``. Reported only -- ``kimi -p`` has
        no effort flag and the default model pins its own thinking budget.
    home_dir : Path, optional
        The Kimi Code data directory to stage credentials from. ``None`` (default)
        uses ``$KIMI_CODE_HOME`` or ``~/.kimi-code`` (from ``kimi login``);
        resolution fails if its ``credentials/`` is absent.

    Examples
    --------
    >>> from open_atp.harness import KimiHarness
    >>> harness = KimiHarness()
    >>> harness.name
    'kimi'
    >>> harness.model
    'kimi-code/kimi-for-coding'
    """

    name = "kimi"

    #: Workdir-relative KIMI_CODE_HOME (matches the export in ``kimi_agent.sh``).
    KIMI_HOME_DIR = ".kimi-home"

    #: Skills mount under KIMI_CODE_HOME's *user* skills dir. Kimi auto-discovers
    #: ``$KIMI_CODE_HOME/skills`` at user scope (no ``.git`` anchor needed, unlike
    #: the project-scope ``.kimi-code/skills``), so it's the robust place to stage.
    skills_dest = f"{KIMI_HOME_DIR}/skills"

    def __init__(
        self,
        *,
        model: str = "kimi-code/kimi-for-coding",
        effort: str = "high",
        home_dir: Path | None = None,
    ) -> None:
        super().__init__(model=model, effort=effort)
        self._home_dir = home_dir

    def _source_home(self) -> Path:
        """The host Kimi Code data dir to stage credentials from."""
        if self._home_dir is not None:
            return self._home_dir
        env = os.environ.get("KIMI_CODE_HOME")
        return Path(env) if env else Path.home() / ".kimi-code"

    def stage_wd(self, wd: Path) -> None:
        super().stage_wd(wd)
        # Workdir-local KIMI_CODE_HOME: stage the OAuth credential, provider config,
        # and a user-scope lean-lsp mcp.json. Sessions (wire.jsonl) land here too and
        # sync back out for parse_result. The skills dir is populated by stage_skills.
        src = self._source_home()
        creds = src / "credentials"
        if not creds.is_dir():
            log.error(
                "missing kimi credentials",
                extra={"harness": self.name, "home": str(src)},
            )
            raise RuntimeError(
                f"kimi harness requires {src}/credentials from `kimi login`"
            )
        home = wd / self.KIMI_HOME_DIR
        home.mkdir(parents=True, exist_ok=True)
        # copytree with copy2 preserves the credentials' 0600 mode.
        shutil.copytree(creds, home / "credentials", copy_function=shutil.copy2)
        for name in ("config.toml", "device_id"):
            if (src / name).is_file():
                shutil.copy2(src / name, home / name)
        # User-scope MCP: lean-lsp with a raised startup/tool timeout so the cold
        # first lean_diagnostic (starts `lake serve`, loads Mathlib) can return --
        # the same 180s fix opencode/vibe needed on a few-CPU sandbox.
        (home / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "lean-lsp": {
                            "type": "stdio",
                            "command": "lean-lsp-mcp",
                            "args": [],
                            "startupTimeoutMs": 180_000,
                            "toolTimeoutMs": 180_000,
                        }
                    }
                },
                indent=2,
            )
        )

    def _agent_command(self) -> str:
        return self._render((_SCRIPTS / "kimi_agent.sh").read_text())

    def _sessions_dir(self, wd: Path) -> Path:
        """Where Kimi writes per-session logs under the workdir-local home."""
        return wd / self.KIMI_HOME_DIR / "sessions"

    def parse_result(self, lines: list[str], wd: Path) -> HarnessRunResult:
        # Final assistant text comes from the stream; token totals come from the
        # session wire log under ``wd`` (the stream omits them).
        result = self._parse_lines(lines)
        self._read_wire_usage(wd, result)
        self._log_usage(result)
        return result

    def collect_logs(self, wd: Path, logs_dir: Path) -> None:
        # The session record (wire.jsonl) and the OAuth credential both live under the
        # workdir-local home. Move the sessions tree out to ``logs/kimi-session`` (the
        # transcript belongs with the run record, not the proof project) and drop the
        # credential so a downloaded workdir doesn't carry the token. Runs after
        # parse_result, so reading usage first is safe.
        home = wd / self.KIMI_HOME_DIR
        sessions = home / "sessions"
        if sessions.is_dir():
            logs_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(sessions), str(logs_dir / "kimi-session"))
        shutil.rmtree(home / "credentials", ignore_errors=True)

    def _newest_wire(self, wd: Path) -> Path | None:
        """The most recently written ``agents/main/wire.jsonl`` under the home."""
        sessions = self._sessions_dir(wd)
        if not sessions.is_dir():
            return None
        wires = list(sessions.glob("*/*/agents/main/wire.jsonl"))
        if not wires:
            return None
        return max(wires, key=lambda p: p.stat().st_mtime)

    def _read_wire_usage(self, wd: Path, result: HarnessRunResult) -> None:
        """Sum token usage and read the stop reason from the session wire log."""
        wire = self._newest_wire(wd)
        if wire is None:
            log.warning(
                "no kimi session wire log found; tokens unavailable",
                extra={"harness": self.name, "wd": str(wd)},
            )
            return
        for line in wire.read_text().splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # usage.record aggregates a turn; sum across turns.
            if obj.get("type") == "usage.record":
                usage = obj.get("usage") or {}
                result.input_tokens += (
                    int(usage.get("inputOther", 0) or 0)
                    + int(usage.get("inputCacheRead", 0) or 0)
                    + int(usage.get("inputCacheCreation", 0) or 0)
                )
                result.output_tokens += int(usage.get("output", 0) or 0)
            event = obj.get("event")
            if isinstance(event, dict) and event.get("type") == "step.end":
                fr = event.get("finishReason")
                if isinstance(fr, str):
                    result.stop_reason = fr

    def _parse_lines(self, lines: list[str]) -> HarnessRunResult:
        """Pull the final assistant message out of Kimi's stream-json output."""
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
