"""Codex CLI harness."""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
from pathlib import Path

from open_atp.harness._paths import _SCRIPTS
from open_atp.harness.base import Harness, HarnessRunResult, MissingCredentials

log = logging.getLogger("open_atp")


class CodexHarness(Harness):
    """Codex CLI, authenticated by a mounted ``auth.json`` credential.

    Codex authenticates via ChatGPT/OpenAI, so it must run an OpenAI model; ``model``
    defaults to ``gpt-5.5`` rather than the Anthropic base default.

    Parameters
    ----------
    model : str, default "gpt-5.5"
        Model id the agent runs; must be an OpenAI model.
    effort : str, default "high"
        Reasoning-effort level.
    auth_file : Path, optional
        The Codex ``auth.json`` to mount. ``None`` (default) uses ``~/.codex/auth.json``
        (from ``codex login``); resolution fails if the file is absent.

    Examples
    --------
    >>> from open_atp.harness import CodexHarness
    >>> harness = CodexHarness()
    >>> harness.name
    'codex'
    >>> harness.model
    'gpt-5.5'
    """

    name = "codex"

    skills_dest = ".agents/skills"

    #: Holds the staged minimal ``.codex`` so it outlives :meth:`_home_dirs` until the
    #: backend pushes/bind-mounts it; cleaned up when the harness is collected.
    _codex_home: tempfile.TemporaryDirectory[str] | None = None

    def __init__(
        self,
        *,
        model: str = "gpt-5.5",
        effort: str = "high",
        auth_file: Path | None = None,
    ) -> None:
        super().__init__(model=model, effort=effort)
        self._auth_file = auth_file
        # Guards the lazy _codex_home init: a benchmark sweep shares one harness
        # instance across tasks run concurrently, so the check-then-create must be
        # atomic (see _home_dirs).
        self._codex_home_lock = threading.Lock()

    def _home_dirs(self) -> list[tuple[Path, str]]:
        # Mount ONLY the auth credential, never the whole ~/.codex: the host's
        # config.toml registers personal MCP servers (e.g. a localhost Zotero server)
        # that don't exist in the sandbox, and codex aborts when one is unreachable.
        # Stage a minimal .codex holding just auth.json -- the launch script supplies
        # the lean-lsp MCP via -c overrides, so no host config is needed. Cached so
        # the staged dir survives until the backend mounts it.
        auth = self._auth_file or Path.home() / ".codex" / "auth.json"
        if not auth.is_file():
            log.error(
                "missing codex auth file",
                extra={"harness": self.name, "auth_file": str(auth)},
            )
            raise MissingCredentials(
                "codex harness requires ~/.codex/auth.json from `codex login`"
            )
        # Lock the check-then-create: without it, two concurrent runs on a shared
        # harness both see None, the second's TemporaryDirectory overwrites the first,
        # and the orphaned one's finalizer deletes its dir out from under the run still
        # staging it -- surfacing as a missing auth.json.
        with self._codex_home_lock:
            if self._codex_home is None:
                self._codex_home = tempfile.TemporaryDirectory(prefix="codex-home-")
                # copy2 preserves auth.json's 0600 mode, which codex requires.
                shutil.copy2(auth, Path(self._codex_home.name) / "auth.json")
        return [(Path(self._codex_home.name), ".codex")]

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
            # ``cached_input_tokens`` is the cache-hit subset of ``input_tokens``,
            # not an addend; it bills at the model's discounted cached rate.
            ct = usage.get("cached_input_tokens") or usage.get("cachedInputTokens") or 0
            if isinstance(it, int):
                result.input_tokens += it
            if isinstance(ct, int):
                result.cached_input_tokens += ct
            if isinstance(ot, int):
                result.output_tokens += ot
            sr = event.get("stop_reason") or event.get("finish_reason")
            if isinstance(sr, str):
                result.stop_reason = sr
        # Codex does not surface USD; left as None so the prover fills from tokens.
        return result
