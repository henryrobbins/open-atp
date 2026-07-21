"""Kimi Code CLI harness (Moonshot ``kimi``, OAuth-authenticated)."""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from open_atp.auth import AuthKind, AuthStatus
from open_atp.harness._paths import _SCRIPTS
from open_atp.harness.base import Harness, HarnessRunResult, MissingCredentials

log = logging.getLogger("open_atp")


class KimiHarness(Harness):
    """Moonshot Kimi Code CLI, authenticated by its file-stored OAuth credential.

    Parameters
    ----------
    model : str, default "kimi-code/k3"
        Model alias the agent runs (a ``config.toml`` alias). The default is K3.
    effort : str, default "high"
        Reasoning-effort level, one of ``"low"``, ``"high"``, or ``"max"``.
    home_dir : pathlib.Path, optional
        The Kimi Code data directory to stage credentials from. ``None`` (default)
        uses ``$KIMI_CODE_HOME`` or ``~/.kimi-code`` (from ``kimi login``);
        resolution fails if absent.

    Examples
    --------

    Constructing the harness resolves its defaults:

    >>> from open_atp.harness import KimiHarness
    >>> harness = KimiHarness()
    >>> harness.name
    'kimi'
    >>> harness.model
    'kimi-code/k3'
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
        model: str = "kimi-code/k3",
        effort: str = "high",
        home_dir: Path | None = None,
    ) -> None:
        super().__init__(model=model, effort=effort)
        self._home_dir = home_dir

    def auth_status(self) -> AuthStatus:
        creds = self._source_home() / "credentials"
        # `kimi login` writes one file per account and the CLI picks by configured
        # provider, so report on the whole dir the way stage_wd mounts it: any
        # credential counts, with the default provider's the one read for an expiry.
        status = AuthStatus(
            kind=AuthKind.OAUTH,
            source=str(creds),
            present=False,
            remedy="`kimi login`",
        )
        if not any(creds.glob("*.json")):
            return status
        try:
            data = json.loads((creds / "kimi-code.json").read_text())
        except (OSError, json.JSONDecodeError):
            # Logged in under some other provider; present, but no expiry to read.
            return replace(status, present=True)
        # `expires_at` is in epoch seconds; the access token is short-lived (~15min)
        # and the CLI trades the refresh token for a new one on the host.
        expires = data.get("expires_at")
        return replace(
            status,
            present=True,
            expires_at=(
                datetime.fromtimestamp(expires, UTC)
                if isinstance(expires, int | float)
                else None
            ),
            refreshable=bool(data.get("refresh_token")),
        )

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
            raise MissingCredentials(
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
                cache_read = int(usage.get("inputCacheRead", 0) or 0)
                # Kimi prices cache *reads* at a discount but has no separate
                # cache-write rate, so creation stays in the full-rate remainder.
                result.input_tokens += (
                    int(usage.get("inputOther", 0) or 0)
                    + cache_read
                    + int(usage.get("inputCacheCreation", 0) or 0)
                )
                result.cached_input_tokens += cache_read
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
