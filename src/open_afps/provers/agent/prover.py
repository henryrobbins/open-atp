"""AgentProver: a coding agent + lean-lsp-mcp running in a sandbox.

An ``AgentProver`` composes:

* a :class:`~open_afps.provers.agent.harness.Harness` (claude_code / codex /
  opencode) -- the *agent* concern: launch script, auth forwarding, output
  parsing; and
* a :class:`~open_afps.backends.base.ComputeBackend` -- the *compute* concern:
  where the agent runs, with Lean+Mathlib and the lean-lsp MCP server.

``prove`` stages the project into the workdir, lets the agent fill the sorrys in
place, then diffs the ``.lean`` files against the staged originals to report what
changed. The shared :class:`~open_afps.core.verifier.Verifier` (owned by the base
``run``) does the final compile/sorry/axiom check.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from open_afps.backends.base import ComputeBackend
from open_afps.core.prover import AutomatedProver, AutomatedProverConfig
from open_afps.core.result import GenerationOutput
from open_afps.core.task import ProofTask
from open_afps.provers.agent.cost import compute_cost_usd
from open_afps.provers.agent.harness import HARNESSES, Harness, resolve_bundle

_DEFAULT_PROMPT = (
    "Complete every `sorry` in this Lean project. Make the project compile and be "
    "sorry-free without introducing new axioms; do not weaken or delete the stated "
    "theorems. Use the lean-lsp-mcp tools (mcp__lean-lsp__*) to check your work as "
    "you go."
)

# Directories never worth copying into the agent workdir.
_IGNORE = shutil.ignore_patterns(".lake", ".git", "*.tar.gz")


@dataclass
class AgentProverConfig(AutomatedProverConfig):
    harness: str = "claude_code"  # one of: claude_code | opencode | codex | vibe
    model: str = "claude-opus-4-8"
    effort: str = "high"
    # Vendored skill/prompt/MCP asset bundle to mount into the workdir.
    assets: str = "default"
    extra_env: dict[str, str] = field(default_factory=dict)
    # Vibe-only knobs (ignored by the other harnesses): which vibe agent profile to
    # drive (``lean`` is Leanstral; ``lean-devstral`` the non-Labs stand-in) and the
    # programmatic-run guards passed straight to ``vibe -p``.
    agent: str = "lean"
    max_turns: int | None = None
    max_price: float | None = None
    # AxProver-only knob (ignored by the other harnesses): cap on ax-prover's own
    # proposer->builder->reviewer loop. ``None`` keeps ax-prover's default (50).
    max_iterations: int | None = None


class AgentProver(AutomatedProver):
    name = "agent"

    config: AgentProverConfig

    def __init__(
        self,
        config: AgentProverConfig,
        verification_backend: ComputeBackend,
        agent_backend: ComputeBackend | None = None,
    ) -> None:
        super().__init__(config, verification_backend)
        # Generation may run in a different backend than verification (e.g. Modal for
        # the agent, local Docker for the cheap final check) -- but defaults to shared.
        self.agent_backend = agent_backend or verification_backend

    def prove(self, task: ProofTask, workdir: Path) -> GenerationOutput:
        # 1. Stage the project so the workdir is a complete project to edit in place.
        shutil.copytree(task.project.root, workdir, dirs_exist_ok=True, ignore=_IGNORE)

        # 2. Snapshot the original .lean contents for the post-run diff.
        original = {
            p.relative_to(workdir).as_posix(): p.read_text()
            for p in workdir.rglob("*.lean")
            if ".lake" not in p.parts
        }

        # 3. Configure the workdir for the chosen harness + asset bundle.
        bundle = resolve_bundle(self.config.assets)
        harness = HARNESSES[self.config.harness].from_config(self.config, assets=bundle)
        # Prompt precedence: explicit task instructions > bundle default > generic.
        prompt = task.instructions or bundle.default_prompt() or _DEFAULT_PROMPT
        harness.configure_wd(workdir, prompt)

        # 4. Run the agent and collect its streamed output.
        lines = self._run_agent(workdir, harness)

        # 5. Token totals -> cost (self-reported, else estimated from the table).
        parsed = harness.parse(lines)
        cost = parsed.cost_usd
        if cost is None:
            cost = compute_cost_usd(
                self.config.model, parsed.input_tokens, parsed.output_tokens
            )

        # 6. Diff the workdir's .lean files against the staged originals.
        completed: dict[str, str] = {}
        for path in sorted(workdir.rglob("*.lean")):
            if ".lake" in path.parts:
                continue
            rel = path.relative_to(workdir).as_posix()
            content = path.read_text()
            if original.get(rel) != content:
                completed[rel] = content

        return GenerationOutput(
            completed_files=completed,
            cost_usd=cost,
            logs="\n".join(lines),
            metadata={
                "harness": harness.name,
                "model": self.config.model,
                "effort": self.config.effort,
                "input_tokens": parsed.input_tokens,
                "output_tokens": parsed.output_tokens,
                "stop_reason": parsed.stop_reason,
            },
        )

    def _auth(self, harness: Harness) -> tuple[dict[str, str], list[tuple[str, str]]]:
        """Resolve the harness's :class:`AuthSpec` into backend env + mounts."""
        spec = harness.auth_spec()
        env: dict[str, str] = {}
        for key in spec.env:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        env.update(harness.static_env())
        env.update(self.config.env)
        env.update(self.config.extra_env)
        home = self.agent_backend.container_home
        mounts = [(str(src), f"{home}/{dest}") for src, dest in spec.home_dirs]
        return env, mounts

    def _run_agent(self, workdir: Path, harness: Harness) -> list[str]:
        """Resolve auth, launch the agent in the backend, and drain its stdout.

        Isolated (it owns credential resolution + the backend call) so tests can
        stand in a fake run -- write a solved file, return a captured stream --
        without Docker or credentials.
        """
        env, mounts = self._auth(harness)
        lines: list[str] = []
        with self.agent_backend.start(
            workdir,
            harness.command,
            env=env,
            mounts=mounts,
            timeout_s=self.config.timeout_s,
        ) as handle:
            lines.extend(handle.stream())
            handle.wait()
        return lines
