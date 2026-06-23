"""Codex CLI harness."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from open_afps.harness._paths import _SCRIPTS
from open_afps.harness.base import AuthSpec, Harness, HarnessRunResult


class CodexHarness(Harness):
    """Codex CLI, authenticated by a mounted ``auth.json`` credential."""

    name = "codex"

    #: Holds the staged minimal ``.codex`` so it outlives :meth:`auth_spec` until the
    #: backend pushes/bind-mounts it; cleaned up when the harness is collected.
    _codex_home: tempfile.TemporaryDirectory[str] | None = None

    def configure_wd(self, wd: Path, prompt: str) -> None:
        super().configure_wd(wd, prompt)
        # Codex registers the MCP server via -c overrides in the launch script;
        # only the skills need copying. https://developers.openai.com/codex/skills
        self._copy_skills(wd, ".agents/skills")

    def auth_spec(self) -> AuthSpec:
        # Mount ONLY the auth credential, never the whole ~/.codex: the host's
        # config.toml registers personal MCP servers (e.g. a localhost Zotero server)
        # that don't exist in the sandbox, and codex aborts when one is unreachable.
        # Stage a minimal .codex holding just auth.json -- the launch script supplies
        # the lean-lsp MCP via -c overrides, so no host config is needed. Staged once
        # and cached so both _auth() calls (mounts, then env) return the same dir and
        # it survives until the backend mounts it.
        auth = Path.home() / ".codex" / "auth.json"
        if not auth.is_file():
            raise RuntimeError(
                "codex harness requires ~/.codex/auth.json from `codex login`"
            )
        if self._codex_home is None:
            self._codex_home = tempfile.TemporaryDirectory(prefix="codex-home-")
            # copy2 preserves auth.json's 0600 mode, which codex requires.
            shutil.copy2(auth, Path(self._codex_home.name) / "auth.json")
        return AuthSpec(home_dirs=[(Path(self._codex_home.name), ".codex")])

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
