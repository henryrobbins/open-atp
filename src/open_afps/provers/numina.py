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

Re-implementation note (vs upstream ``scripts/runner.py``): each round here is a
fresh ``claude -p`` invocation over the *same* (bind-mounted, persistent) workdir,
not a ``claude -c continue`` against a live session. The DockerBackend launches a
new ``--rm`` container per round, so the Claude session does not survive between
rounds -- which means every round is effectively a "session reset", and the agent
resumes from the partial proof state left on disk. ``max_consecutive_limits`` is
still tracked (and surfaced in metadata) for parity/observability.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from open_afps.backends.base import ComputeSession
from open_afps.core.prover import logs_dir_for
from open_afps.core.result import GenerationOutput
from open_afps.core.task import LeanProject, ProofTask
from open_afps.harness import (
    HARNESSES,
    Harness,
    HarnessRunResult,
    compute_cost_usd,
    resolve_bundle,
)
from open_afps.provers.agent_prover import AgentProver, AgentProverConfig
from open_afps.provers.numina_tracker import StatementTracker

# Directories never worth copying into the agent workdir (mirrors AgentProver).
_IGNORE = shutil.ignore_patterns(".lake", ".git", "*.tar.gz")

# Numina's coordinator prompt (main_entry.md) does not, by itself, emit the
# END_REASON marker the round loop keys off; append an explicit protocol so the
# loop can tell "done" from "out of budget". (We still fall back to Claude's result
# subtype when the marker is missing -- see ``_end_reason``.)
_END_REASON_PROTOCOL = """

---
SESSION CONTROL PROTOCOL (open-afps round loop)

When you finish this turn, the VERY LAST line of your final message MUST be
exactly one of the following, on its own line, with no surrounding markdown,
backticks, or trailing text:

  END_REASON:COMPLETE   -- every target theorem compiles and is sorry-free
  END_REASON:LIMIT      -- you made progress but ran out of turns/budget

If you emit END_REASON:LIMIT you will be re-invoked to continue from the proof
state currently on disk, so leave the project in the best state you can.
"""

_FALLBACK_PROMPT = (
    "Complete every `sorry` in this Lean project so it compiles and is sorry-free "
    "without introducing new axioms; do not weaken or delete the stated theorems."
)

# Match END_REASON:<reason> on a line of its own (case-insensitive), per upstream.
_REASON_RE = re.compile(
    r"(?m)^\s*END_REASON:(LIMIT|COMPLETE|SELECTED_TARGET_COMPLETE)\s*$", re.I
)

_SORRY_RE = re.compile(r"\bsorry\b")

# Helper-LLM usage ledger: ``discussion_partner.py`` appends one JSON record per
# Gemini/GPT call here (workdir-relative, under ``.claude/`` next to ``cli.log``).
# ``prove()`` reads it after the run, prices the tokens, and bills the result into
# the run's ``cost_usd`` so discussion-partner spend is not lost. The path is the
# host-side mirror of the default in ``discussion_partner._record_usage``.
_HELPER_USAGE_FILE = Path(".claude") / "helper_usage.jsonl"


@dataclass
class NuminaProverConfig(AgentProverConfig):
    """Configuration for :class:`NuminaProver`.

    Extends :class:`~open_afps.provers.agent_prover.AgentProverConfig` with the
    Numina coordinator's round-loop and helper-skill knobs.

    Attributes
    ----------
    harness : str
        Fixed to ``claude_code``: Numina is claude-CLI driven and not configurable.
    assets : str
        Asset bundle to mount. Default ``numina`` (coordinator prompt + vendored
        skills + subagent prompts).
    max_rounds : int
        Maximum number of coordinator rounds before the run stops. Default ``20``.
    max_consecutive_limits : int
        Reset (start a fresh session) after this many consecutive LIMIT rounds.
        Default ``2``.
    helper_env_keys : tuple[str, ...]
        Helper-skill credentials forwarded into the sandbox when present in the host
        env; skills degrade/skip when their key is absent. ``ANTHROPIC_API_KEY``
        backs the informal-prover skill's Claude calls.
    guard_statements : bool
        Whether to snapshot the target theorems and reject runs that weaken or
        delete them. Default ``True``.
    on_statement_change : {"error", "warn"}
        Behavior on a weakened/deleted target theorem: ``error`` stops the run and
        restores the originals; ``warn`` restores and continues. Default ``error``
        (rejects; safe).
    extra_env : dict[str, str]
        Additional environment variables forwarded into the agent sandbox. Default
        empty.
    """

    harness: str = "claude_code"
    assets: str = "numina"
    max_rounds: int = 20
    max_consecutive_limits: int = 2
    helper_env_keys: tuple[str, ...] = (
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "LEAN_LEANDEX_API_KEY",
        "ANTHROPIC_API_KEY",
    )
    guard_statements: bool = True
    on_statement_change: Literal["error", "warn"] = "error"
    extra_env: dict[str, str] = field(default_factory=dict)


class NuminaProver(AgentProver):
    name = "numina"

    config: NuminaProverConfig

    def prove(self, task: ProofTask, workdir: Path) -> GenerationOutput:
        # 1. Stage the project so the workdir is a complete project to edit in place.
        shutil.copytree(task.project.root, workdir, dirs_exist_ok=True, ignore=_IGNORE)

        # 2. Snapshot original .lean contents for the post-run diff.
        original = {
            p.relative_to(workdir).as_posix(): p.read_text()
            for p in workdir.rglob("*.lean")
            if ".lake" not in p.parts
        }

        # 3. Configure the workdir with the Numina asset bundle (coordinator prompt
        #    + vendored skills + subagent prompts).
        bundle = resolve_bundle(self.config.assets)
        harness = HARNESSES[self.config.harness](
            self.config.model, self.config.effort, assets=bundle
        )
        base_prompt = task.instructions or bundle.default_prompt() or _FALLBACK_PROMPT
        harness.configure_wd(workdir, base_prompt + _END_REASON_PROTOCOL)

        # 4. Statement-change guard: snapshot the target theorems before the run.
        tracked = self._tracked_files(task, workdir)
        tracker: StatementTracker | None = None
        if self.config.guard_statements and tracked:
            tracker = StatementTracker(tracked)

        # 5. Round-continuation loop. When generation and verification share a
        #    backend, run the whole loop -- and the final check -- in one persistent
        #    sandbox, so we pay neither per-round nor a separate verify spin-up.
        if self.agent_backend is self.verifier.backend:
            _, mounts = self._auth(harness)
            with self.agent_backend.session(
                workdir, mounts=mounts, timeout_s=self.config.timeout_s
            ) as session:
                loop = self._run_rounds(workdir, harness, tracker, session=session)
                # Final pull so the host workdir (helper-usage ledger, completed
                # files) is current before pricing + diffing.
                session.sync_out()
                output = self._finalize(workdir, harness, loop, original)
                output.verification = self.verifier.verify(
                    LeanProject(workdir), session=session
                )
                return output

        loop = self._run_rounds(workdir, harness, tracker)
        return self._finalize(workdir, harness, loop, original)

    def _finalize(
        self,
        workdir: Path,
        harness: Harness,
        loop: dict[str, Any],
        original: dict[str, str],
    ) -> GenerationOutput:
        """Price helper-LLM usage, diff the workdir, and assemble the output."""
        # Price the helper-LLM (discussion_partner) usage the agent racked up inside
        # the sandbox and fold it into the run's total cost.
        helper = self._helper_cost(workdir)

        # Diff the workdir's .lean files against the staged originals.
        completed: dict[str, str] = {}
        for path in sorted(workdir.rglob("*.lean")):
            if ".lake" in path.parts:
                continue
            rel = path.relative_to(workdir).as_posix()
            content = path.read_text()
            if original.get(rel) != content:
                completed[rel] = content

        # Relocate any harness-specific rich logs out of the workdir (no-op for the
        # claude_code harness Numina pins, whose record is the stdout stream).
        harness.collect_logs(workdir, logs_dir_for(workdir))

        return GenerationOutput(
            completed_files=completed,
            cost_usd=loop["total_cost_usd"] + helper["cost_usd"],
            logs="\n".join(loop["lines"]),
            stderr=loop["stderr"],
            metadata={
                "harness": harness.name,
                "model": self.config.model,
                "effort": self.config.effort,
                "assets": self.config.assets,
                "input_tokens": loop["input_tokens"],
                "output_tokens": loop["output_tokens"],
                # cost_usd above bundles agent + helper; keep the split visible.
                "agent_cost_usd": loop["total_cost_usd"],
                "helper_cost_usd": helper["cost_usd"],
                "helper_tokens": {
                    "input": helper["input_tokens"],
                    "output": helper["output_tokens"],
                },
                "helper_breakdown": helper["breakdown"],
                "helper_unpriced_models": helper["unpriced_models"],
                "rounds": loop["rounds"],
                "end_reason": loop["end_reason"],
                "session_resets": loop["session_resets"],
                "guard_statements": self.config.guard_statements,
                "statement_changed": loop["statement_changed"],
                "statement_changes": loop["statement_changes"],
                "round_history": loop["round_history"],
            },
        )

    # -- round loop -----------------------------------------------------------

    def _run_rounds(
        self,
        workdir: Path,
        harness: Harness,
        tracker: StatementTracker | None,
        session: ComputeSession | None = None,
    ) -> dict[str, Any]:
        """Drive the agent for up to ``max_rounds``, continuing while it reports LIMIT.

        Returns an accumulator dict (lines, token totals, cost, per-round history,
        final end reason, statement-guard outcome).

        ``session`` given: every round execs in the one persistent sandbox. We
        ``sync_out`` after each round so the host-side statement tracker reads the
        round's result, and ``sync_in`` after a restore so the sandbox picks it up for
        the next round (both no-ops on bind-mounted Docker).
        """
        lines: list[str] = []
        stderrs: list[str] = []
        round_history: list[dict[str, Any]] = []
        total_cost = 0.0
        input_tokens = 0
        output_tokens = 0
        consecutive_limits = 0
        session_resets = 0
        end_reason: str | None = None
        statement_changed = False
        statement_changes: list[str] = []

        for round_num in range(1, self.config.max_rounds + 1):
            # A fresh session is started whenever we have hit too many consecutive
            # LIMITs (and, in this backend, every round -- see module docstring).
            if consecutive_limits >= self.config.max_consecutive_limits:
                consecutive_limits = 0
                session_resets += 1

            round_lines, round_stderr = self._run_agent(
                workdir, harness, session=session
            )
            lines.extend(round_lines)
            if round_stderr:
                stderrs.append(round_stderr)
            # Pull the round's edits to the host so the statement tracker (and the
            # next round's snapshot) see them (no-op on bind-mounted Docker).
            if session is not None:
                session.sync_out()
            parsed = harness.parse(round_lines)

            input_tokens += parsed.input_tokens
            output_tokens += parsed.output_tokens
            round_cost = parsed.cost_usd
            if round_cost is None:
                round_cost = (
                    compute_cost_usd(
                        self.config.model, parsed.input_tokens, parsed.output_tokens
                    )
                    or 0.0
                )
            total_cost += round_cost
            end_reason = self._end_reason(parsed)

            record: dict[str, Any] = {
                "round": round_num,
                "end_reason": end_reason,
                "subtype": parsed.subtype,
                "input_tokens": parsed.input_tokens,
                "output_tokens": parsed.output_tokens,
                "cost_usd": round_cost,
            }

            # Statement-change guard.
            if tracker is not None:
                ok, changes = tracker.check_initial_statements()
                if not ok:
                    statement_changed = True
                    these = [str(c) for c in changes]
                    statement_changes.extend(these)
                    record["statement_changes"] = these
                    tracker.restore_initial_statements(changes)
                    # Push the restored statements back so the sandbox reflects them
                    # for the next round (no-op on bind-mounted Docker).
                    if session is not None:
                        session.sync_in()
                    if self.config.on_statement_change == "error":
                        end_reason = "STATEMENT_CHANGED"
                        record["end_reason"] = end_reason
                        round_history.append(record)
                        break

            round_history.append(record)

            if end_reason == "COMPLETE":
                break
            if end_reason == "LIMIT":
                consecutive_limits += 1
            else:
                # None / SELECTED_TARGET_COMPLETE: keep going from a clean streak.
                consecutive_limits = 0

        return {
            "lines": lines,
            "stderr": "\n".join(stderrs),
            "round_history": round_history,
            "rounds": len(round_history),
            "total_cost_usd": total_cost,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "session_resets": session_resets,
            "end_reason": end_reason,
            "statement_changed": statement_changed,
            "statement_changes": statement_changes,
        }

    @staticmethod
    def _end_reason(parsed: HarnessRunResult) -> str | None:
        """Resolve a round's end reason from the agent's final result.

        Prefers the explicit ``END_REASON:<reason>`` marker; falls back to Claude's
        result subtype (``success`` -> COMPLETE, ``error_max_turns`` -> LIMIT).
        """
        if parsed.result_text:
            m = _REASON_RE.search(parsed.result_text)
            if m:
                return m.group(1).upper()
        if parsed.subtype == "success":
            return "COMPLETE"
        if parsed.subtype == "error_max_turns":
            return "LIMIT"
        return None

    # -- helpers --------------------------------------------------------------

    def _helper_cost(self, workdir: Path) -> dict[str, Any]:
        """Price the discussion-partner usage ledger into USD.

        Reads the JSONL ledger ``discussion_partner.py`` appended inside the
        sandbox (one record per Gemini/GPT call, accumulated across rounds in the
        persistent workdir) and converts the tokens via :func:`compute_cost_usd`.
        Records whose model is absent from the price table contribute ``0.0`` but
        their tokens are still summed and the model name flagged in
        ``unpriced_models`` -- so an unpriced helper is visible, not silently free.
        Returns total cost, token totals, a per-``backend:model`` breakdown, and
        the unpriced-model list.
        """
        path = workdir / _HELPER_USAGE_FILE
        total = 0.0
        input_tokens = 0
        output_tokens = 0
        unpriced: list[str] = []
        breakdown: dict[str, dict[str, Any]] = {}
        if path.is_file():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                model = str(rec.get("model", ""))
                it = int(rec.get("input_tokens", 0) or 0)
                ot = int(rec.get("output_tokens", 0) or 0)
                input_tokens += it
                output_tokens += ot
                cost = compute_cost_usd(model, it, ot)
                if cost is None:
                    if model not in unpriced:
                        unpriced.append(model)
                    cost = 0.0
                total += cost
                key = f"{rec.get('backend', '?')}:{model}"
                agg = breakdown.setdefault(
                    key,
                    {
                        "calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_usd": 0.0,
                    },
                )
                agg["calls"] += 1
                agg["input_tokens"] += it
                agg["output_tokens"] += ot
                agg["cost_usd"] += cost
        return {
            "cost_usd": total,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "unpriced_models": unpriced,
            "breakdown": breakdown,
        }

    def _tracked_files(self, task: ProofTask, workdir: Path) -> list[Path]:
        """The workdir ``.lean`` files whose statements the guard should protect."""
        if task.targets:
            return [f for f in (workdir / t for t in task.targets) if f.is_file()]
        return [
            p
            for p in sorted(workdir.rglob("*.lean"))
            if ".lake" not in p.parts and _SORRY_RE.search(p.read_text())
        ]

    def _auth(self, harness: Harness) -> tuple[dict[str, str], list[tuple[str, str]]]:
        """Extend the base auth with Numina's helper-skill credentials."""
        env, mounts = super()._auth(harness)
        for key in self.config.helper_env_keys:
            value = os.environ.get(key)
            if value is not None:
                env.setdefault(key, value)
        return env, mounts
