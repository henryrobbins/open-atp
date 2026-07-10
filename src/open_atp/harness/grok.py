"""Grok CLI harness (xAI's ``grok`` coding agent, aka Grok Build)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from open_atp.harness._paths import _SCRIPTS
from open_atp.harness.base import Harness, HarnessRunResult


class GrokHarness(Harness):
    """xAI's ``grok`` CLI (Grok Build), authenticated by an ``XAI_API_KEY``.

    Grok is single-provider (xAI only), so unlike :class:`OpenCodeHarness` there is
    no provider to infer: the key is always forwarded as ``XAI_API_KEY`` and the
    model is any xAI model id. ``grok --single`` runs one prompt headlessly; the
    lean-lsp MCP server is wired in via a project-scope ``.grok/config.toml`` that
    :meth:`stage_wd` writes.

    Parameters
    ----------
    model : str
        Model id the agent runs. Default ``"grok-4.5"`` (xAI's recommended model for
        code); ``"grok-build-0.1"`` selects the code-specialized model instead.
    effort : str
        Reasoning-effort level. Recorded in run metadata only -- ``grok --single``
        exposes no effort flag. Default ``"high"``.
    xai_api_key : str, optional
        The xAI API key forwarded as ``XAI_API_KEY``. ``None`` (default) reads it from
        the host ``XAI_API_KEY`` env var; resolution fails if neither is set.

    Examples
    --------
    >>> from open_atp.harness import GrokHarness
    >>> harness = GrokHarness()
    >>> harness.name
    'grok'
    >>> harness.model
    'grok-4.5'

    With the key supplied explicitly, :meth:`agent_auth` forwards it as
    ``XAI_API_KEY`` without reading the host environment:

    >>> harness = GrokHarness(xai_api_key="xai-fake")
    >>> harness.agent_auth().env
    {'XAI_API_KEY': 'xai-fake'}
    """

    name = "grok"

    skills_dest = ".agents/skills"

    def __init__(
        self,
        *,
        model: str = "grok-4.5",
        effort: str = "high",
        xai_api_key: str | None = None,
    ) -> None:
        super().__init__(model=model, effort=effort)
        self._xai_api_key = xai_api_key

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

    def _required_env(self) -> dict[str, str]:
        key = self._xai_api_key or os.environ.get("XAI_API_KEY")
        if not key:
            raise RuntimeError("grok harness requires XAI_API_KEY (an xAI API key)")
        return {"XAI_API_KEY": key}

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
