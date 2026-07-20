"""Agent harnesses: the *agent* concern composed by ``AgentProver``.

Each harness adapts one agent CLI (Claude Code / Codex / OpenCode / Vibe /
ax-prover) to the sandbox: launch script, credential forwarding, and token/cost
parsing. The *compute* concern (where the command runs, with Lean+Mathlib) lives
in the injected :class:`~open_atp.backends.base.ComputeBackend`.
"""

from open_atp.harness.axproverbase import AxProverBaseHarness
from open_atp.harness.base import (
    PROMPT_FILE,
    SCRIPT_FILE,
    AgentAuth,
    Harness,
    HarnessRunResult,
    MissingCredentials,
)
from open_atp.harness.claude_code import ClaudeCodeHarness
from open_atp.harness.codex import CodexHarness
from open_atp.harness.cost import COST_PER_MTOK, compute_cost_usd
from open_atp.harness.opencode import OpenCodeHarness
from open_atp.harness.vibe import VibeHarness

#: Harness registry by name (``Harness.name`` -> harness class). Package-internal: the
#: factory in :mod:`open_atp.config` dispatches a harness spec's ``type`` through this.
_HARNESSES: dict[str, type[Harness]] = {
    h.name: h
    for h in (
        ClaudeCodeHarness,
        CodexHarness,
        OpenCodeHarness,
        VibeHarness,
        AxProverBaseHarness,
    )
}

__all__ = [
    "Harness",
    "HarnessRunResult",
    "AgentAuth",
    "MissingCredentials",
    "SCRIPT_FILE",
    "PROMPT_FILE",
    "ClaudeCodeHarness",
    "CodexHarness",
    "OpenCodeHarness",
    "VibeHarness",
    "AxProverBaseHarness",
    "compute_cost_usd",
    "COST_PER_MTOK",
]
