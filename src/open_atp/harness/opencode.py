"""OpenCode CLI harness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from open_atp.harness._paths import _SCRIPTS
from open_atp.harness.base import (
    Harness,
    HarnessRunResult,
    _infer_provider,
)


class OpenCodeHarness(Harness):
    """OpenCode CLI, authenticated by a provider API key forwarded from the host.

    Parameters
    ----------
    model : str
        Model id the agent runs. Default ``"deepseek-v4-pro"``.
    effort : str
        Reasoning-effort level. Default ``"high"``.
    provider : str, optional
        API provider name. ``None`` infers it from the model prefix (``claude-*`` ->
        ``anthropic``, ``gpt-*`` -> ``openai``, ...).
    provider_api_key : str, optional
        The selected provider's API key, forwarded under its canonical env var
        (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` / ...). ``None`` (default) reads
        that env var from the host; resolution fails if neither is set. The key is
        assumed to match :attr:`provider` (OpenAI and DeepSeek keys are
        indistinguishable, so no format check is done).

    Examples
    --------
    The provider is inferred from the model prefix when not given explicitly:

    >>> from open_atp.harness import OpenCodeHarness
    >>> harness = OpenCodeHarness(model="gpt-5.5")
    >>> harness.name
    'opencode'
    >>> harness.provider
    'openai'

    With the provider key supplied explicitly, :meth:`agent_auth` forwards it under
    the provider's canonical env var without reading the host environment:

    >>> harness = OpenCodeHarness(model="claude-opus-4-8", provider_api_key="sk-fake")
    >>> harness.agent_auth().env
    {'ANTHROPIC_API_KEY': 'sk-fake'}
    """

    name = "opencode"

    skills_dest = ".agents/skills"

    def __init__(
        self,
        *,
        model: str = "deepseek-v4-pro",
        effort: str = "high",
        provider: str | None = None,
        provider_api_key: str | None = None,
    ) -> None:
        super().__init__(model=model, effort=effort)
        self._provider = provider
        self._provider_api_key = provider_api_key

    @property
    def provider(self) -> str:
        """API provider, taken from config or inferred from the model prefix."""
        return self._provider or _infer_provider(self.model)

    def stage_wd(self, wd: Path) -> None:
        super().stage_wd(wd)
        # opencode.json configures the model provider + MCP server.
        (wd / "opencode.json").write_text(json.dumps(self._opencode_config(), indent=2))

    def _opencode_config(self) -> dict[str, Any]:
        options: dict[str, Any]
        if self.provider == "anthropic":
            options = {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": self.effort},
            }
        else:
            options = {"reasoningEffort": self.effort}
        return {
            "$schema": "https://opencode.ai/config.json",
            "provider": {self.provider: {"models": {self.model: {"options": options}}}},
            "mcp": {
                "lean-lsp": {
                    "type": "local",
                    "command": ["lean-lsp-mcp"],
                    "enabled": True,
                    # The first lean_diagnostic_messages call starts `lake serve` and
                    # loads the file's full import closure (Mathlib) into the LSP, which
                    # routinely exceeds opencode's 60s default MCP request timeout on a
                    # cold, few-CPU sandbox -- surfacing as `MCP error -32001: Request
                    # timed out`. Raise it so the slow first diagnostic can return.
                    "timeout": 180_000,
                }
            },
        }

    def _required_env(self) -> dict[str, str]:
        return self._provider_key_env(self.provider, self._provider_api_key)

    def _agent_command(self) -> str:
        template = (_SCRIPTS / "opencode_agent.sh").read_text()
        return template.replace("<<PROVIDER>>", self.provider).replace(
            "<<MODEL>>", self.model
        )

    def _parse_lines(self, lines: list[str]) -> HarnessRunResult:
        """Parse ``opencode run --format json`` output."""

        def _as_int(x: Any) -> int:
            return x if isinstance(x, int) else 0

        result = HarnessRunResult()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "step_finish":
                continue
            part = event.get("part") or {}
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            result.input_tokens += (
                _as_int(tokens.get("input"))
                + _as_int(cache.get("write"))
                + _as_int(cache.get("read"))
            )
            result.output_tokens += _as_int(tokens.get("output"))
            c = part.get("cost")
            if isinstance(c, (int, float)):
                result.cost_usd = (result.cost_usd or 0.0) + float(c)
            r = part.get("reason")
            if isinstance(r, str):
                result.stop_reason = r
        return result
