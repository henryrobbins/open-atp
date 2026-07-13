"""Numina's pinned Claude Code harness (private: only :class:`NuminaProver` uses it).

Numina is claude_code-driven and ships its own scaffold (no plugins). Its helper
skills call Leandex / Gemini / GPT / Claude, so this harness forwards those host
credentials into the sandbox best-effort -- present keys are forwarded; absent ones
are skipped (the skills degrade/skip), never a hard failure. Any literal ``env``
extras win over everything else on a key clash.

This lives here, rather than on the base :class:`Harness`, because Numina is the
only caller that needs best-effort host passthrough -- so the generic harnesses
don't carry it.
"""

from __future__ import annotations

import logging
import os

from open_atp.harness.base import AgentAuth
from open_atp.harness.claude_code import ClaudeCodeHarness

log = logging.getLogger("open_atp")

#: Helper-skill credentials forwarded into the sandbox when present in the host env;
#: skills degrade/skip when their key is absent. ``ANTHROPIC_API_KEY`` backs the
#: informal-prover skill's Claude calls.
_DEFAULT_HELPER_ENV_KEYS = (
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "LEAN_LEANDEX_API_KEY",
    "ANTHROPIC_API_KEY",
)


class NuminaHarness(ClaudeCodeHarness):
    """Claude Code pinned for Numina: no plugins, plus best-effort helper keys.

    Parameters
    ----------
    model : str
        Model id the agent runs. Default ``"claude-opus-4-8"``.
    effort : str
        Reasoning-effort level. Default ``"high"``.
    oauth_token : str, optional
        The ``CLAUDE_CODE_OAUTH_TOKEN`` to forward; ``None`` (default) reads the host
        env var.
    helper_env_keys : tuple[str, ...], optional
        Host credential names forwarded into the sandbox when present, skipped when
        absent (never a hard failure). Default :data:`_DEFAULT_HELPER_ENV_KEYS`.
    env : dict[str, str], optional
        Literal env vars (name -> value) forwarded verbatim; win over resolved
        credentials (including helper keys) on a key clash. Default none.
    """

    name = "numina"

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-8",
        effort: str = "high",
        oauth_token: str | None = None,
        helper_env_keys: tuple[str, ...] = _DEFAULT_HELPER_ENV_KEYS,
        env: dict[str, str] | None = None,
    ) -> None:
        # Numina ships its own scaffold, so it loads no plugins.
        super().__init__(
            model=model, effort=effort, plugins=[], oauth_token=oauth_token
        )
        self._helper_env_keys = tuple(helper_env_keys)
        self._extra_env = dict(env or {})

    def agent_auth(self) -> AgentAuth:
        """Claude Code's auth plus best-effort helper keys; literal ``env`` wins."""
        auth = super().agent_auth()
        forwarded = []
        for key in self._helper_env_keys:
            value = os.environ.get(key)
            if value is not None:
                auth.env.setdefault(key, value)
                forwarded.append(key)
        missing = [k for k in self._helper_env_keys if k not in forwarded]
        if missing:
            log.debug(
                "numina helper credentials absent; dependent skills will degrade",
                extra={"forwarded": forwarded, "missing": missing},
            )
        auth.env.update(self._extra_env)
        return auth
