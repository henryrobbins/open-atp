"""Grok CLI harness (xAI's ``grok`` coding agent, aka Grok Build)."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from open_atp.harness._paths import _SCRIPTS
from open_atp.harness.base import Harness, HarnessRunResult


class GrokHarness(Harness):
    """xAI's ``grok`` CLI (Grok Build), authenticated by a mounted ``auth.json``.

    Grok is single-provider (xAI only), so there is no provider to infer. Rather than
    a metered ``XAI_API_KEY``, this harness forwards the OAuth login written by
    ``grok`` on the host (``~/.grok/auth.json``), so runs draw on the logged-in xAI
    plan. Only ``auth.json`` is staged -- never the whole ``~/.grok`` (which also holds
    the installed CLI binary under ``bin/`` and personal state); the launch script
    points ``GROK_HOME`` at the mount so grok reads the credential there and
    self-populates the rest, leaving the image's ``~/.grok`` binary intact.
    ``grok --single`` runs one prompt headlessly; the lean-lsp MCP server is wired in
    via a project-scope ``.grok/config.toml`` that :meth:`stage_wd` writes.

    Parameters
    ----------
    model : str
        Model id the agent runs. Default ``"grok-4.5"`` (xAI's recommended model for
        code); ``"grok-build-0.1"`` selects the code-specialized model instead.
    effort : str
        Reasoning-effort level. Recorded in run metadata only -- ``grok --single``
        exposes no effort flag. Default ``"high"``.
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
        """Parse ``grok --single --output-format json`` output.

        ``--output-format json`` emits a single JSON object (which may span lines);
        pull token usage out of it defensively across the common field names. Cost is
        left ``None`` so the prover estimates it from the token totals via the pricing
        table -- the grok CLI does not self-report USD.
        """
        result = HarnessRunResult()
        obj = _decode_json(lines)
        if obj is None:
            return result
        usage = obj.get("usage")
        if isinstance(usage, dict):
            result.input_tokens = _first_int(
                usage, ("input_tokens", "prompt_tokens", "inputTokens")
            )
            result.output_tokens = _first_int(
                usage, ("output_tokens", "completion_tokens", "outputTokens")
            )
        sr = obj.get("stop_reason") or obj.get("finish_reason")
        if isinstance(sr, str):
            result.stop_reason = sr
        rt = obj.get("result") or obj.get("text")
        result.result_text = rt if isinstance(rt, str) else None
        return result


def _decode_json(lines: list[str]) -> dict[str, Any] | None:
    """Decode the grok JSON result, tolerating whether it is one line or many.

    Tries each line as a standalone object first (the last decodable one wins), then
    the whole buffer joined -- covering both a compact single line and a pretty-printed
    multi-line object.
    """
    obj: dict[str, Any] | None = None
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            obj = parsed
    if obj is not None:
        return obj
    try:
        parsed = json.loads("\n".join(lines))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _first_int(d: dict[str, Any], keys: tuple[str, ...]) -> int:
    """First key in ``keys`` whose value is an int, else 0."""
    for key in keys:
        v = d.get(key)
        if isinstance(v, int):
            return v
    return 0
