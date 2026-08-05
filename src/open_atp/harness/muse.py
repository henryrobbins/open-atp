"""Muse Code CLI harness (Meta ``muse``, Meta Model API key)."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from open_atp.auth import AuthKind, AuthStatus
from open_atp.harness._paths import _SCRIPTS
from open_atp.harness.base import Harness, HarnessRunResult

log = logging.getLogger("open_atp")

#: Host env var the Meta Model API key is read from, shared with the other Meta
#: provers. ``muse`` itself reads ``META_API_KEY``, which the sandbox env sets.
_HOST_KEY_ENV = "META_MODEL_API_KEY"


class MuseHarness(Harness):
    """Meta's Muse Code CLI, authenticated by a Meta Model API key.

    Parameters
    ----------
    model : str, default "muse-spark-1.2-contributor"
        Model id the agent runs. The default is the **contributor tier**: it is
        roughly an order of magnitude cheaper than the standard
        ``muse-spark-1.2``, in exchange for Meta training future models on the
        run's prompts and completions. Pass ``"muse-spark-1.2"`` for the standard
        tier, which carries no such grant.
    effort : str, default "high"
        Reasoning-effort level, one of ``"none"``, ``"minimal"``, ``"low"``,
        ``"medium"``, ``"high"``, ``"xhigh"``, or ``"ultra"``.
    api_key : str, optional
        Meta Model API key. ``None`` (default) reads ``META_MODEL_API_KEY`` from
        the host environment; resolution fails if that is unset too.

    Examples
    --------

    Constructing the harness resolves its defaults:

    >>> from open_atp.harness import MuseHarness
    >>> harness = MuseHarness()
    >>> harness.name
    'muse'
    >>> harness.model
    'muse-spark-1.2-contributor'
    """

    name = "muse"

    #: Workdir-relative XDG roots (matching the exports in ``muse_agent.sh``).
    #: Config carries settings.json; data carries the per-session event logs.
    CONFIG_DIR = ".muse-config"
    DATA_DIR = ".muse-data"

    #: Project-scope skills, which muse discovers under the workspace root once
    #: the workspace is trusted (``--yolo`` does that).
    skills_dest = ".agents/skills"

    def __init__(
        self,
        *,
        model: str = "muse-spark-1.2-contributor",
        effort: str = "high",
        api_key: str | None = None,
    ) -> None:
        super().__init__(model=model, effort=effort)
        self._api_key = api_key

    def auth_status(self) -> AuthStatus:
        return AuthStatus.from_env(
            _HOST_KEY_ENV,
            self._api_key,
            kind=AuthKind.API_KEY,
            remedy="create a key at https://dev.meta.ai/",
        )

    def _required_env(self) -> dict[str, str]:
        # Read under the project-wide name shared with the other Meta provers,
        # forward under the name muse itself reads.
        key = self._key_env(_HOST_KEY_ENV, self._api_key)[_HOST_KEY_ENV]
        return {"META_API_KEY": key}

    def stage_wd(self, wd: Path) -> None:
        super().stage_wd(wd)
        # Workdir-local XDG_CONFIG_HOME: muse requires settings.json to declare
        # schema_version 1 or it refuses to start. mcp_servers is where an agentic
        # harness gets lean-lsp; muse spawns it as a plain child process (MCP
        # servers sit outside its own sandbox, which --yolo disables anyway).
        config = wd / self.CONFIG_DIR / "muse"
        config.mkdir(parents=True, exist_ok=True)
        (config / "settings.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "mcp_servers": {
                        "lean-lsp": {
                            "enabled": True,
                            "transport": "stdio",
                            "command": "lean-lsp-mcp",
                            "args": [],
                        }
                    },
                },
                indent=2,
            )
        )

    def _agent_command(self) -> str:
        return self._render((_SCRIPTS / "muse_agent.sh").read_text())

    def _sessions_dir(self, wd: Path) -> Path:
        """Where muse writes its per-session event logs under the workdir."""
        return wd / self.DATA_DIR / "muse" / "sessions"

    def parse_result(self, lines: list[str], wd: Path) -> HarnessRunResult:
        # The stdout stream carries the final text and the terminal state but no
        # usage at all; token totals come from the session logs under ``wd``.
        result = self._parse_lines(lines)
        self._read_session_usage(wd, result)
        self._log_usage(result)
        return result

    def collect_logs(self, wd: Path, logs_dir: Path) -> None:
        # The session event logs are the agent's rich transcript; move them out to
        # ``logs/muse-session`` so the downloaded workdir stays the proof project.
        # Runs after parse_result, which reads them for usage, so this is safe.
        sessions = self._sessions_dir(wd)
        if sessions.is_dir():
            logs_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(sessions), str(logs_dir / "muse-session"))

    def _read_session_usage(self, wd: Path, result: HarnessRunResult) -> None:
        """Sum token usage across the run's main and subagent session logs.

        Muse delegates freely to subagents, each of which writes its own
        ``session.jsonl`` under the main session's ``subagent/`` tree; their tokens
        bill to the same account, so a total that skipped them would understate the
        run substantially.
        """
        sessions = self._sessions_dir(wd)
        logs = sorted(sessions.glob("**/session.jsonl")) if sessions.is_dir() else []
        if not logs:
            log.warning(
                "no muse session log found; tokens unavailable",
                extra={"harness": self.name, "wd": str(wd)},
            )
            return
        for path in logs:
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = (obj.get("payload") or {}).get("event")
                if (
                    not isinstance(event, dict)
                    or event.get("kind") != "goal_usage_attribution"
                ):
                    continue
                quantity = (event.get("record") or {}).get("quantity") or {}
                # `reported: false` records are placeholders for a step whose usage
                # the provider never returned; their zeros are harmless but skipping
                # them keeps the intent clear.
                if not quantity.get("reported"):
                    continue
                # `cached_tokens` is the cache-hit subset of `input_tokens`, and
                # `reasoning_tokens` the reasoning subset of `output_tokens` -- both
                # already counted in their parent, so neither is added separately.
                result.input_tokens += int(quantity.get("input_tokens", 0) or 0)
                result.cached_input_tokens += int(quantity.get("cached_tokens", 0) or 0)
                result.output_tokens += int(quantity.get("output_tokens", 0) or 0)

    def _parse_lines(self, lines: list[str]) -> HarnessRunResult:
        """Read the final text and terminal state out of muse's JSONL stream.

        Every record is an envelope whose ``payload_type`` names the event; the run's
        single ``run.terminal.*`` record carries how it ended (``completed`` /
        ``failed`` / ``cancelled``) and the agent's closing message.
        """
        result = HarnessRunResult()
        for line in lines:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not str(obj.get("payload_type", "")).startswith("run.terminal."):
                continue
            payload = obj.get("payload") or {}
            result.stop_reason = payload.get("terminal")
            result.result_text = payload.get("text") or None
            reason = payload.get("reason")
            if reason:
                # Only set on a non-completed terminal; it is the one place muse
                # explains itself (bad credential, MCP server that failed to start).
                log.warning(
                    "muse run did not complete",
                    extra={
                        "harness": self.name,
                        "terminal": payload.get("terminal"),
                        "reason": reason,
                    },
                )
        return result
