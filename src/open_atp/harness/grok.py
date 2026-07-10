"""Grok CLI harness (xAI's ``grok`` coding agent, aka Grok Build)."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path

from open_atp.harness._paths import _SCRIPTS
from open_atp.harness.base import Harness, HarnessRunResult

#: The ACP driver staged next to ``agent.sh`` and exec'd by the launch script.
_ACP_DRIVER = "grok_acp.py"


class GrokHarness(Harness):
    """xAI's ``grok`` CLI (Grok Build), authenticated by a mounted ``auth.json``.

    Grok is single-provider (xAI only), so there is no provider to infer. Rather than
    a metered ``XAI_API_KEY``, this harness forwards the OAuth login written by
    ``grok`` on the host (``~/.grok/auth.json``), so runs draw on the logged-in xAI
    plan. Only ``auth.json`` is staged -- never the whole ``~/.grok`` (which also holds
    the installed CLI binary under ``bin/`` and personal state); the launch script
    points ``GROK_HOME`` at the mount so grok reads the credential there and
    self-populates the rest, leaving the image's ``~/.grok`` binary intact.
    The run is driven over ACP (xAI's Agent Client Protocol: ``grok agent stdio``,
    JSON-RPC 2.0), not ``grok --single``: the single-shot mode emits only one terminal
    JSON object, so a run's tool calls and progress never reach stdout, whereas ACP
    streams them as ``session/update`` events. :meth:`stage_wd` writes a small Python
    driver (``grok_acp.py``) that the launch script exec's; it speaks ACP to a child
    ``grok agent stdio`` and re-emits the event stream as JSONL. The lean-lsp MCP
    server is wired in via a project-scope ``.grok/config.toml`` that :meth:`stage_wd`
    also writes.

    Parameters
    ----------
    model : str
        Model id the agent runs. Default ``"grok-4.5"`` (xAI's recommended model for
        code); ``"grok-build-0.1"`` selects the code-specialized model instead.
    effort : str
        Reasoning-effort level, passed to ``grok agent`` as ``--reasoning-effort``
        (ACP honors it; the old ``--single`` path did not). Default ``"high"``.
    auth_file : Path, optional
        The grok ``auth.json`` to mount. ``None`` (default) uses ``~/.grok/auth.json``
        (from ``grok`` login); resolution fails if the file is absent.

    Examples
    --------
    >>> from open_atp.harness import GrokHarness
    >>> harness = GrokHarness()
    >>> harness.name
    'grok'
    >>> harness.model
    'grok-4.5'
    """

    name = "grok"

    skills_dest = ".agents/skills"

    #: Holds the staged minimal GROK_HOME (just ``auth.json``) so it outlives
    #: :meth:`_home_dirs` until the backend mounts it; cleaned up on collection.
    _grok_home: tempfile.TemporaryDirectory[str] | None = None

    def __init__(
        self,
        *,
        model: str = "grok-4.5",
        effort: str = "high",
        auth_file: Path | None = None,
    ) -> None:
        super().__init__(model=model, effort=effort)
        self._auth_file = auth_file
        # Guards the lazy _grok_home init: a benchmark sweep shares one harness
        # instance across tasks run concurrently, so check-then-create must be atomic.
        self._grok_home_lock = threading.Lock()

    def _home_dirs(self) -> list[tuple[Path, str]]:
        # Mount ONLY the auth credential, and NOT at `.grok`: the image installs the
        # grok binary under ~/.grok/bin, so bind-mounting over ~/.grok would shadow it.
        # Stage a minimal dir holding just auth.json and mount it at `.grok-home`; the
        # launch script sets GROK_HOME to it so grok reads the credential there and
        # writes its own state (config, locks, sessions) alongside. Staged once and
        # cached so both agent_auth() reads return the same dir and it survives until
        # the backend mounts it.
        auth = self._auth_file or Path.home() / ".grok" / "auth.json"
        if not auth.is_file():
            raise RuntimeError(
                "grok harness requires ~/.grok/auth.json from `grok` login"
            )
        # Lock the check-then-create: without it two concurrent runs on a shared
        # harness both see None, the second's TemporaryDirectory overwrites the first,
        # and the orphaned finalizer deletes its dir out from under the staging run.
        with self._grok_home_lock:
            if self._grok_home is None:
                self._grok_home = tempfile.TemporaryDirectory(prefix="grok-home-")
                # copy2 preserves auth.json's 0600 mode.
                shutil.copy2(auth, Path(self._grok_home.name) / "auth.json")
        return [(Path(self._grok_home.name), ".grok-home")]

    def stage_wd(self, wd: Path) -> None:
        super().stage_wd(wd)
        # ACP driver the launch script exec's (see grok_agent.sh); needs python3 in
        # the image (present -- lean-lsp-mcp is a Python tool).
        shutil.copy2(_SCRIPTS / _ACP_DRIVER, wd / _ACP_DRIVER)
        # Project-scope grok config: wires lean-lsp-mcp (the same server the other
        # harnesses mount) into `grok --single`. tool_timeout_sec mirrors the
        # opencode/vibe fix -- the first lean_diagnostic_messages call starts
        # `lake serve` and loads the file's full Mathlib import closure, which blows
        # through the default tool timeout on a cold, few-CPU sandbox.
        grok_dir = wd / ".grok"
        grok_dir.mkdir(parents=True, exist_ok=True)
        (grok_dir / "config.toml").write_text(
            "[mcp_servers.lean-lsp]\n"
            'command = "lean-lsp-mcp"\n'
            "args = []\n"
            "enabled = true\n"
            "startup_timeout_sec = 30\n"
            "tool_timeout_sec = 180\n"
        )

    def _agent_command(self) -> str:
        return self._render((_SCRIPTS / "grok_agent.sh").read_text())

    def _parse_lines(self, lines: list[str]) -> HarnessRunResult:
        """Parse the JSONL stream ``grok_acp.py`` writes.

        The driver emits one ``{"sessionUpdate": "result", ...}`` line at the end,
        carrying ``stopReason`` and the token counts normalized out of the ACP
        ``session/prompt`` response ``_meta`` (``input_tokens`` / ``output_tokens``).
        Cost is left ``None`` so the prover estimates it from the token totals via the
        pricing table -- the grok CLI does not self-report USD.
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
            if not isinstance(obj, dict) or obj.get("sessionUpdate") != "result":
                continue
            result.input_tokens = obj.get("input_tokens") or 0
            result.output_tokens = obj.get("output_tokens") or 0
            sr = obj.get("stopReason")
            result.stop_reason = sr if isinstance(sr, str) else None
            rt = obj.get("text")
            result.result_text = rt if isinstance(rt, str) else None
        return result
