"""AristotleProver: a wrapper around Harmonic's Aristotle API.

No agentic sandbox is needed for generation -- we shell out to ``aristotle submit
--project-dir <dir> --wait --destination <tar.gz>``, unpack the returned archive
over the workdir, and then hand off to the shared verifier for the final check.

This is the cheapest end-to-end slice and is the recommended second build step
(after the backend + verifier spine), because it exercises the full platform path
with the least moving parts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from open_afps.core.prover import AutomatedProver, AutomatedProverConfig
from open_afps.core.task import ProofTask


@dataclass
class AristotleProverConfig(AutomatedProverConfig):
    api_key_env: str = "ARISTOTLE_API_KEY"
    mode: str = "instruct"  # 'ask' | 'instruct' for follow-ups
    wait: bool = True


class AristotleProver(AutomatedProver):
    name = "aristotle"

    config: AristotleProverConfig

    def prove(self, task: ProofTask, workdir: Path) -> dict[str, str]:
        # TODO(phase 2):
        #   1. Stage task.project into workdir.
        #   2. Run `aristotle submit "<instructions>" --project-dir {workdir} --wait
        #      --destination result.tar.gz` (via aristotlelib or subprocess).
        #   3. Unpack result.tar.gz over workdir (completed .lean + summary).
        #   4. Return the changed files. Cost is currently undocumented -> leave None.
        raise NotImplementedError("AristotleProver.prove not yet implemented")
