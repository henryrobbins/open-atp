"""AgentProver: a coding agent + lean-lsp-mcp running in a sandbox.

An ``AgentProver`` composes:

* a :class:`~open_afps.harness.Harness` (claude_code / codex / opencode) -- the
  *agent* concern: launch script, auth forwarding, output parsing; and
* a :class:`~open_afps.backends.base.ComputeBackend` -- the *compute* concern:
  where the agent runs, with Lean+Mathlib and the lean-lsp MCP server.

``prove`` stages the project into the workdir, lets the agent fill the sorrys in
place, then diffs the ``.lean`` files against the staged originals to report what
changed. The shared :class:`~open_afps.core.verifier.Verifier` (owned by the base
``run``) does the final compile/sorry/axiom check.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from open_afps.backends.base import CommandResult, ComputeBackend, ComputeSession
from open_afps.core.prover import AutomatedProver, AutomatedProverConfig
from open_afps.core.result import GenerationOutput
from open_afps.core.task import LeanProject, ProofTask
from open_afps.harness import HARNESSES, Harness, bundle_for_config, compute_cost_usd

log = logging.getLogger(__name__)

_DEFAULT_PROMPT = """\
The working directory is a complete Lean 4 lake project. One or more `.lean`
files contain `sorry` (or `admit`) placeholders standing in for proofs that have
not been written yet. Replace every such placeholder with a real proof so the
project compiles cleanly and depends on no axioms beyond Lean's standard set.

Hard rules:
- Do not weaken, rename, restate, or delete any theorem, lemma, `def`,
  `structure`, or signature. Only fill in proof bodies (the part after `:=` /
  `by` that is currently `sorry`). Changing a statement to make it easier to
  prove is failure, not success.
- No new axioms and no `sorry`/`admit`/`native_decide`-on-false escapes. The
  finished proof must type-check honestly. The only acceptable axioms are Lean's
  standard `propext`, `Classical.choice`, and `Quot.sound`.
- Stay inside this working directory; do not read or write files outside it.
- Do not edit `lakefile.toml`/`lakefile.lean`, `lean-toolchain`, or
  `lake-manifest.json` — they pin the toolchain and dependencies and must match
  the verification environment.

Workflow:
1. Find the work: search for `sorry` across the `.lean` source files (e.g.
   `rg -n '\\bsorry\\b'`). Read each file containing one to understand the
   statement, the hypotheses, and the relevant imports.
2. Confirm the lean-lsp MCP server is live before relying on it: call
   `mcp__lean-lsp__lean_diagnostic_messages` on a file you have not yet edited.
   `success:true, items:[]` means it compiles cleanly; real errors come back as
   `items`. `success:false, items:[]` usually means imports aren't built yet —
   run `lake build` for the relevant modules first.
3. Write a proof for one `sorry` at a time. Mathlib is available; prefer library
   lemmas, `simp`, `omega`, `linarith`, `exact?`/`apply?` suggestions, and
   `aesop` over long bespoke arguments.
4. After each edit, re-check that file with
   `mcp__lean-lsp__lean_diagnostic_messages` and iterate until it is clean.
5. When a file looks done, verify it has no stubbed proofs with
   `mcp__lean-lsp__lean_verify` — the reported axioms must NOT contain `sorryAx`.
6. Repeat until no `.lean` file contains a `sorry` and the whole project builds
   (`lake build`).

Tips:
- Use the lean-lsp tools (`mcp__lean-lsp__*`) as your primary feedback loop; they
  are far faster than a full `lake build` per change. Use `lake build` to
  materialize oleans for imports and as the final whole-project check.
- If a goal looks false or unprovable from the given hypotheses, re-read the
  statement: you likely misread a binder or a coercion. Do not "fix" it by
  changing the statement — finish the proof as stated.
- Non-trivial proofs routinely take many rounds of compile-error fixing. Keep
  iterating against the diagnostics rather than guessing."""

# Directories never worth copying into the agent workdir.
_IGNORE = shutil.ignore_patterns(".lake", ".git", "*.tar.gz")


@dataclass
class AgentProverConfig(AutomatedProverConfig):
    harness: str = "claude_code"  # one of: claude_code | opencode | codex | vibe
    model: str = "claude-opus-4-8"
    effort: str = "high"
    # Vendored skill/prompt/MCP asset bundle to mount into the workdir.
    assets: str = "default"
    # Per-run overrides of the bundle's assets, each a list of names (resolved from
    # a vendored catalog) or full paths. ``None`` keeps the bundle's own assets; an
    # empty list mounts none. ``skills`` apply to every harness; ``plugins`` are
    # Claude-only (ignored by the others).
    skills: list[str] | None = None
    plugins: list[str] | None = None
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
        bundle = bundle_for_config(self.config)
        harness = HARNESSES[self.config.harness].from_config(self.config, assets=bundle)
        # Prompt precedence: explicit task instructions > bundle default > generic.
        prompt = task.instructions or bundle.default_prompt() or _DEFAULT_PROMPT
        harness.configure_wd(workdir, prompt)

        # 4. Run the agent. When generation and verification share a backend, keep the
        #    sandbox alive and run the final check in it -- no second spin-up.
        if self.agent_backend is self.verifier.backend:
            # Mounts (credential dirs) must be pinned when the session sandbox is
            # created; per-command env is forwarded by _run_agent's exec.
            _, mounts = self._auth(harness)
            with self.agent_backend.session(
                workdir, mounts=mounts, timeout_s=self.config.timeout_s
            ) as session:
                lines = self._run_agent(workdir, harness, session=session)
                # Bring the agent's edits back to the host so the diff sees them
                # (no-op for bind-mounted Docker; a tar pull for Modal).
                session.sync_out()
                output = self._build_output(workdir, harness, lines, original)
                output.verification = self.verifier.verify(
                    LeanProject(workdir), session=session
                )
                return output

        lines = self._run_agent(workdir, harness)
        return self._build_output(workdir, harness, lines, original)

    def _build_output(
        self,
        workdir: Path,
        harness: Harness,
        lines: list[str],
        original: dict[str, str],
    ) -> GenerationOutput:
        """Token totals -> cost, then diff the workdir against the staged originals."""
        parsed = harness.parse(lines)
        cost = parsed.cost_usd
        if cost is None:
            cost = compute_cost_usd(
                self.config.model, parsed.input_tokens, parsed.output_tokens
            )

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

    def _run_agent(
        self, workdir: Path, harness: Harness, session: ComputeSession | None = None
    ) -> list[str]:
        """Resolve auth, launch the agent in the backend, and drain its stdout.

        Isolated (it owns credential resolution + the backend call) so tests can
        stand in a fake run -- write a solved file, return a captured stream --
        without Docker or credentials.

        ``session`` given (the backend-reuse path): exec the agent in that live
        sandbox, which stays up for the verifier afterwards. ``None``: the one-shot
        path, a fresh sandbox via ``start`` that tears down on exit.
        """
        env, mounts = self._auth(harness)
        lines: list[str] = []
        if session is not None:
            # Mounts were pinned at session creation; only per-command env here.
            handle = session.exec(harness.command, env=env)
            lines.extend(handle.stream())
            self._log_agent_result(harness, handle.wait(), lines)
            return lines
        with self.agent_backend.start(
            workdir,
            harness.command,
            env=env,
            mounts=mounts,
            timeout_s=self.config.timeout_s,
        ) as handle:
            lines.extend(handle.stream())
            self._log_agent_result(harness, handle.wait(), lines)
        return lines

    def _log_agent_result(
        self, harness: Harness, result: CommandResult, lines: list[str]
    ) -> None:
        """Surface a silent/failed agent run -- its stderr is otherwise discarded.

        The agent's stderr (captured by the backend) is the only clue when an agent
        emits no parseable stdout (e.g. a launch/auth failure that leaves 0 tokens and
        no edits). Without this a silent run is invisible short of re-running the whole
        sandbox job, so log a warning whenever the agent exits non-zero or produces no
        output at all.
        """
        if result.exit_code != 0 or not lines:
            stderr = result.stderr.strip()
            log.warning(
                "agent harness %r exited %s with %d stdout line(s)%s",
                harness.name,
                result.exit_code,
                len(lines),
                f"; stderr:\n{stderr}" if stderr else " and no stderr",
            )
