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
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Literal

from open_atp.backends.base import ComputeBackend, ComputeSession
from open_atp.harness import (
    Harness,
    HarnessRunResult,
    compute_cost_usd,
)
from open_atp.harness._catalog import resolve_skill
from open_atp.harness._numina import _DEFAULT_HELPER_ENV_KEYS, NuminaHarness
from open_atp.harness._paths import _vendor_numina_dir
from open_atp.lean import LeanProject, ProofTask
from open_atp.provers.agent_prover import AgentProver
from open_atp.provers.base import GenerationTimeout, ProofResult, compose_prompt
from open_atp.provers.numina_tracker import StatementTracker

log = logging.getLogger("open_atp")

# Directories never worth copying into the agent workdir (mirrors AgentProver).
_IGNORE = shutil.ignore_patterns(".lake", ".git", "*.tar.gz")

# Numina's coordinator prompt (main_entry.md) does not, by itself, emit the
# END_REASON marker the round loop keys off; append an explicit protocol so the
# loop can tell "done" from "out of budget". (We still fall back to Claude's result
# subtype when the marker is missing -- see ``_end_reason``.)
_END_REASON_PROTOCOL = """

---
SESSION CONTROL PROTOCOL (open-atp round loop)

When you finish this turn, the VERY LAST line of your final message MUST be
exactly one of the following, on its own line, with no surrounding markdown,
backticks, or trailing text:

  END_REASON:COMPLETE   -- every target theorem compiles and is sorry-free
  END_REASON:LIMIT      -- you made progress but ran out of turns/budget

If you emit END_REASON:LIMIT you will be re-invoked to continue from the proof
state currently on disk, so leave the project in the best state you can.
"""


def _coordinator_prompt() -> str:
    """Numina's prover prompt: the coordinator (main_entry.md) + the round protocol.

    The coordinator scaffold is a vendored asset (``vendor/numina/prompts``); the
    session-control protocol is appended so the round loop can tell "done" from "out
    of budget". This is the *prover* prompt -- the task's optional ``user_prompt`` is
    layered on top by :func:`~open_atp.provers.base.compose_prompt`.
    """
    main_entry = _vendor_numina_dir() / "prompts" / "main_entry.md"
    return main_entry.read_text() + _END_REASON_PROTOCOL


# Match END_REASON:<reason> on a line of its own (case-insensitive), per upstream.
_REASON_RE = re.compile(
    r"(?m)^\s*END_REASON:(LIMIT|COMPLETE|SELECTED_TARGET_COMPLETE)\s*$", re.I
)

_SORRY_RE = re.compile(r"\bsorry\b")

# Don't start another coordinator round with less than this much of the generation
# budget left: a round needs meaningful wall-clock to make progress, and the per-round
# cap (remaining budget) must stay comfortably positive so coreutils ``timeout`` has
# something to enforce (see ``_run_rounds``).
_MIN_ROUND_S = 60

# Helper-LLM usage ledger: ``discussion_partner.py`` appends one JSON record per
# Gemini/GPT call here (workdir-relative, under ``.claude/`` next to ``cli.log``).
# ``prove()`` reads it after the run, prices the tokens, and bills the result into
# the run's ``cost_usd`` so discussion-partner spend is not lost. The path is the
# host-side mirror of the default in ``discussion_partner._record_usage``.
_HELPER_USAGE_FILE = Path(".claude") / "helper_usage.jsonl"


class NuminaProver(AgentProver):
    """Run the Numina coordinator/subagent scaffold as an :class:`AgentProver`.

    A specialization of :class:`AgentProver` wired to Numina's vendored scaffold
    (coordinator prompt + skills + subagent prompts, staged by
    :meth:`_stage_numina_assets`); generation and the shared
    :class:`~open_atp.verify.Verifier` work exactly as in the base agent prover.

    The harness is fixed to an internal ``NuminaHarness`` (Claude Code with no plugins,
    since Numina ships its own scaffold) and is *not* configurable --
    Numina is claude-CLI driven, and :meth:`_stage_numina_assets` mounts the vendored
    scaffold straight into the known ``.claude/`` locations.

    Parameters
    ----------
    backend : ComputeBackend
        The sandbox the agent runs in. Generation reuses it via a live session and
        verification runs in that same hot sandbox.
    skills : list[str], optional
        Extra named/path skills to mount alongside Numina's vendored scaffold.
        Defaults to none -- Numina's coordinator skill is staged from
        ``vendor/numina/skills``, not this list.
    max_rounds : int, default 20
        Maximum number of coordinator rounds before the run stops.
    max_consecutive_limits : int, default 2
        Reset (start a fresh session) after this many consecutive LIMIT rounds.
    oauth_token : str, optional
        The ``CLAUDE_CODE_OAUTH_TOKEN`` to forward into the sandbox; ``None``
        (default) reads it from the host env var.
    helper_env_keys : tuple[str, ...]
        Helper-skill credentials forwarded into the sandbox when present in the host
        env; skills degrade/skip when their key is absent. Defaults to
        :data:`_DEFAULT_HELPER_ENV_KEYS`.
    guard_statements : bool, default True
        Whether to snapshot the target theorems and reject runs that weaken or
        delete them.
    on_statement_change : {"error", "warn"}, default "error"
        Behavior on a weakened/deleted target theorem: ``error`` stops the run and
        restores the originals; ``warn`` restores and continues. The default rejects,
        which is the safe choice.
    timeout_s : int, default 1800
        Wall-clock budget for the generation run, in seconds.
    env : dict[str, str], optional
        Extra literal environment variables forwarded into the agent sandbox (Numina
        pins its harness, so its env knobs live here). Defaults to no extra variables.

    Examples
    --------

    Construct the prover directly:

    >>> from open_atp.backends.docker import DockerBackend
    >>> from open_atp.provers.numina import NuminaProver
    >>> backend = DockerBackend()
    >>> prover = NuminaProver(backend=backend)
    >>> prover.max_rounds
    20

    Or build the same prover from the standard catalog by name, taking its
    baked-in defaults (see :func:`~open_atp.config.standard_prover`):

    >>> from open_atp import standard_prover
    >>> prover = standard_prover("numina", backend=DockerBackend())
    >>> prover.name
    'numina'

    Complete a task's ``sorry``\\s with
    :meth:`~open_atp.provers.base.AutomatedProver.prove`, here on a bundled example
    (this runs the Numina scaffold in Docker and bills it):

    >>> import tempfile
    >>> from open_atp.examples import EXAMPLE, example_task
    >>> task = example_task(EXAMPLE.INTER_UNION_DISTRIB)
    >>> result = prover.prove(task, tempfile.mkdtemp())  # doctest: +SKIP
    >>> result.success  # doctest: +SKIP
    True
    """

    def __init__(
        self,
        *,
        backend: ComputeBackend,
        skills: list[str] | None = None,
        max_rounds: int = 20,
        max_consecutive_limits: int = 2,
        oauth_token: str | None = None,
        helper_env_keys: tuple[str, ...] = _DEFAULT_HELPER_ENV_KEYS,
        guard_statements: bool = True,
        on_statement_change: Literal["error", "warn"] = "error",
        timeout_s: int = 1800,
        env: dict[str, str] | None = None,
    ) -> None:
        # The harness is pinned: Numina is claude_code-driven and not configurable.
        # NuminaHarness ships no plugins (Numina ships its own scaffold) and forwards
        # the helper-skill keys best-effort plus any literal env extras. Default skills
        # are empty -- the coordinator skill is staged from the vendored scaffold.
        super().__init__(
            backend=backend,
            harness=NuminaHarness(
                oauth_token=oauth_token, helper_env_keys=helper_env_keys, env=env
            ),
            skills=skills if skills is not None else [],
            timeout_s=timeout_s,
        )
        self.max_rounds = max_rounds
        self.max_consecutive_limits = max_consecutive_limits
        self.helper_env_keys = tuple(helper_env_keys)
        self.guard_statements = guard_statements
        self.on_statement_change = on_statement_change

    @property
    def prover_prompt(self) -> str:
        """The prover's own prompt, handed to the agent before any user prompt.

        Numina's coordinator scaffold plus the round protocol.
        """
        return _coordinator_prompt()

    def _generate(
        self, task: ProofTask, wd: Path, logs_dir: Path, result: ProofResult
    ) -> None:
        # 1. Stage the project so the workdir is a complete project to edit in place.
        shutil.copytree(task.project.root, wd, dirs_exist_ok=True, ignore=_IGNORE)

        # 2. Snapshot original .lean contents for the post-run diff.
        original = {
            p.relative_to(wd).as_posix(): p.read_text()
            for p in wd.rglob("*.lean")
            if ".lake" not in p.parts
        }

        # 3. Stage the workdir: the harness launch script, Numina's vendored scaffold
        #    (coordinator skill + subagent prompts), any config skills, then the
        #    coordinator prompt (+ the task's optional user prompt).
        harness = self.harness
        harness.stage_wd(wd)
        self._stage_numina_assets(wd)
        harness.stage_skills(wd, [resolve_skill(s) for s in self.skills])
        harness.write_prompt(wd, compose_prompt(self.prover_prompt, task.user_prompt))
        stdout_path = logs_dir / "stdout.txt"

        # 4. Statement-change guard: snapshot the target theorems before the run.
        tracked = self._tracked_files(task, wd)
        tracker: StatementTracker | None = None
        if self.guard_statements and tracked:
            tracker = StatementTracker(tracked)

        # 5. Round-continuation loop, with the final check, in one persistent sandbox:
        #    generation and verification share the backend, so we pay neither a
        #    per-round nor a separate verify spin-up. The session must cover both the
        #    whole round loop (self.timeout_s) and the verifier's own budget, since
        #    both run before teardown; the backend adds its sync headroom on top.
        _, mounts = self._auth(harness)
        with self.verifier.backend.session(
            wd, mounts=mounts, timeout_s=self.timeout_s + self.verifier.timeout_s
        ) as session:
            loop = self._run_rounds(wd, harness, stdout_path, tracker, session=session)
            # Final pull so the host workdir (helper-usage ledger, completed files)
            # is current before pricing + diffing.
            self._download_wd(wd, session=session)
            self._finalize(result, wd, logs_dir, harness, loop, original)
            self._check_auth(harness, result, loop["lines"], loop["stderr"])
            result.verification = self.verifier.verify(LeanProject(wd), session=session)

        # The round loop stopped because it ran out of wall-clock, not because it
        # finished -- a timeout, not a plain miss, once the salvaged proof has been
        # verified and the partial record is on ``result``.
        if loop["end_reason"] == "TIMEOUT" and not result.success:
            raise GenerationTimeout(
                f"numina round loop used its full {self.timeout_s}s budget without a "
                "verifying proof"
            )

    def _stage_numina_assets(self, wd: Path) -> None:
        """Copy Numina's vendored scaffold into the workdir's ``.claude/`` locations.

        Numina is one root-mounted coordinator skill (top-level ``SKILL.md`` + ``cli/``
        helpers) plus a subagent-prompt tree. Because the harness is pinned to Claude,
        the destinations are known: the skill's *contents* land at ``.claude/skills``
        (so the top-level ``SKILL.md`` is at ``.claude/skills/SKILL.md``), and the whole
        prompt tree at ``.claude/prompts`` (where the coordinator prompt tells the agent
        to read its subagent prompts from ``.claude/prompts/subagent_prompts/``).
        """
        root = _vendor_numina_dir()
        claude = wd / ".claude"
        shutil.copytree(root / "skills", claude / "skills", dirs_exist_ok=True)
        shutil.copytree(root / "prompts", claude / "prompts", dirs_exist_ok=True)

    def _finalize(
        self,
        result: ProofResult,
        wd: Path,
        logs_dir: Path,
        harness: Harness,
        loop: dict[str, Any],
        original: dict[str, str],
    ) -> None:
        """Price helper-LLM usage, diff the workdir, and fill the result."""
        # Price the helper-LLM (discussion_partner) usage the agent racked up inside
        # the sandbox and fold it into the run's total cost.
        helper = self._helper_cost(wd)

        # Diff the workdir's .lean files against the staged originals.
        completed: dict[str, str] = {}
        for path in sorted(wd.rglob("*.lean")):
            if ".lake" in path.parts:
                continue
            rel = path.relative_to(wd).as_posix()
            content = path.read_text()
            if original.get(rel) != content:
                completed[rel] = content

        # Relocate harness logs (no-op for claude_code) and write captured stderr; the
        # streamed stdout was already teed live into ``logs_dir/stdout.txt``.
        self._download_logs(harness, wd, logs_dir, loop["stderr"])

        result.completed_files = completed
        result.cost_usd = loop["total_cost_usd"] + helper["cost_usd"]
        result.metadata = {
            "harness": harness.name,
            "model": self.harness.model,
            "effort": self.harness.effort,
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
            "guard_statements": self.guard_statements,
            "statement_changed": loop["statement_changed"],
            "statement_changes": loop["statement_changes"],
            "round_history": loop["round_history"],
        }

    # -- round loop -----------------------------------------------------------

    def _run_rounds(
        self,
        workdir: Path,
        harness: Harness,
        stdout_path: Path,
        tracker: StatementTracker | None,
        session: ComputeSession,
    ) -> dict[str, Any]:
        """Drive the agent for up to ``max_rounds``, continuing while it reports LIMIT.

        Each round's stdout is teed live into ``stdout_path`` (appended, so the file is
        the full multi-round transcript). Returns an accumulator dict (token totals,
        cost, per-round history, final end reason, statement-guard outcome).

        Every round execs in the one persistent ``session``. We ``sync_out`` after each
        round so the host-side statement tracker reads the round's result, and
        ``sync_in`` after a restore so the sandbox picks it up for the next round (both
        no-ops on bind-mounted Docker).
        """
        stderrs: list[str] = []
        all_lines: list[str] = []
        round_history: list[dict[str, Any]] = []
        total_cost = 0.0
        input_tokens = 0
        output_tokens = 0
        consecutive_limits = 0
        session_resets = 0
        end_reason: str | None = None
        statement_changed = False
        statement_changes: list[str] = []

        # The rounds share one Sandbox lifetime (Sandbox timeout = self.timeout_s +
        # headroom), so budget the loop as a whole: each round is capped at the
        # *remaining* wall-clock, and we stop once too little is left to make progress.
        deadline = time.monotonic() + self.timeout_s

        for round_num in range(1, self.max_rounds + 1):
            remaining = deadline - time.monotonic()
            if remaining < _MIN_ROUND_S:
                end_reason = "TIMEOUT"
                log.warning(
                    "numina round loop out of budget",
                    extra={"round": round_num, "remaining_s": round(remaining, 1)},
                )
                break

            # A fresh session is started whenever we have hit too many consecutive
            # LIMITs (and, in this backend, every round -- see module docstring).
            if consecutive_limits >= self.max_consecutive_limits:
                consecutive_limits = 0
                session_resets += 1
                log.debug("session reset", extra={"round": round_num})

            # The per-round coreutils-timeout flag is ignored: the loop infers timeout
            # from its own wall-clock budget below (backend-agnostic; Docker doesn't
            # cap a round, so the flag never trips there).
            round_lines, round_stderr, _ = self._run_agent(
                workdir, harness, stdout_path, session=session, timeout_s=int(remaining)
            )
            all_lines.extend(round_lines)
            if round_stderr:
                stderrs.append(round_stderr)
            # Pull the round's edits to the host so the statement tracker (and the
            # next round's snapshot) see them (no-op on bind-mounted Docker).
            session.sync_out()
            parsed = harness.parse_result(round_lines, workdir)

            input_tokens += parsed.input_tokens
            output_tokens += parsed.output_tokens
            round_cost = parsed.cost_usd
            if round_cost is None:
                round_cost = (
                    compute_cost_usd(
                        self.harness.model,
                        parsed.input_tokens,
                        parsed.output_tokens,
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
                    session.sync_in()
                    if self.on_statement_change == "error":
                        end_reason = "STATEMENT_CHANGED"
                        record["end_reason"] = end_reason
                        round_history.append(record)
                        break

            round_history.append(record)
            log.debug(
                "round complete",
                extra={
                    "round": round_num,
                    "max_rounds": self.max_rounds,
                    "end_reason": end_reason,
                    "cost_usd": round_cost,
                },
            )

            if end_reason == "COMPLETE":
                break
            if end_reason == "LIMIT":
                consecutive_limits += 1
            else:
                # None / SELECTED_TARGET_COMPLETE: keep going from a clean streak.
                consecutive_limits = 0

        return {
            "stderr": "\n".join(stderrs),
            "lines": all_lines,
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
