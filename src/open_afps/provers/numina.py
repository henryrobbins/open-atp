"""NuminaProver: a configured variant of :class:`AgentProver`.

Numina is, structurally, "Claude Code + a specific skills/prompts/search toolkit,
run in a multi-round loop in a sandbox." So rather than re-implement it, we extend
``AgentProver`` pinned to the ``claude_code`` harness with Numina's vendored assets
(``vendor/numina/skills`` + ``vendor/numina/prompts``), and add the two genuinely
different behaviours:

* a **round-continuation loop** -- re-invoke the agent while it reports it hit a
  limit rather than completing (Numina's ``END_REASON == LIMIT``); and
* the **statement tracker** -- guard against the agent deleting or weakening the
  theorems it was asked to prove (ported from ``scripts/statement_tracker.py``).

Numina's helper skills call Leandex / Gemini / GPT, so its config carries those API
keys to forward into the sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from open_afps.core.task import ProofTask
from open_afps.provers.agent import AgentProver, AgentProverConfig


@dataclass
class NuminaProverConfig(AgentProverConfig):
    harness: str = "claude_code"  # Numina is claude-CLI driven; not configurable.
    assets: str = "numina"
    max_rounds: int = 20
    # Helper-skill credentials forwarded into the sandbox.
    helper_env_keys: tuple[str, ...] = (
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "LEAN_LEANDEX_API_KEY",
    )
    guard_statements: bool = True
    extra_env: dict[str, str] = field(default_factory=dict)


class NuminaProver(AgentProver):
    name = "numina"

    config: NuminaProverConfig

    def prove(self, task: ProofTask, workdir: Path) -> dict[str, str]:
        # TODO(phase 4):
        #   1. Stage project + vendored Numina assets into workdir.
        #   2. Snapshot statements (statement_tracker) if guard_statements.
        #   3. Loop up to max_rounds: run the claude_code agent; stop when it reports
        #      COMPLETE, continue while it reports LIMIT.
        #   4. Re-check the statement tracker; reject runs that weakened theorems.
        #   5. Return changed files.
        raise NotImplementedError("NuminaProver.prove not yet implemented")
