"""AgentProver: a coding agent + lean-lsp-mcp running in a sandbox.

An ``AgentProver`` composes:

* a :class:`~open_atp.harness.Harness` (claude_code / codex / opencode) -- the
  *agent* concern: launch script, auth forwarding, output parsing; and
* a :class:`~open_atp.backends.base.ComputeBackend` -- the *compute* concern:
  where the agent runs, with Lean+Mathlib and the lean-lsp MCP server.

``prove`` stages the project into the workdir, lets the agent fill the sorrys in
place, then diffs the ``.lean`` files against the staged originals to report what
changed. The shared :class:`~open_atp.verify.Verifier` (owned by the base
``run``) does the final compile/sorry/axiom check.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TextIO

from open_atp.backends.base import (
    CommandHandle,
    CommandResult,
    ComputeBackend,
    ComputeSession,
)
from open_atp.harness import (
    ClaudeCodeHarness,
    Harness,
    compute_cost_usd,
)
from open_atp.harness._catalog import resolve_skill
from open_atp.lean import LeanProject, ProofTask
from open_atp.provers.base import (
    AutomatedProver,
    ProofResult,
    compose_prompt,
)

log = logging.getLogger("open_atp")

PROVER_PROMPT = """\
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
   `items`. `success:false, items:[]` usually means the import oleans aren't
   materialized yet — run `lake env lean <file>` once to build them.
3. Write a proof for one `sorry` at a time. Mathlib is available; prefer library
   lemmas, `simp`, `omega`, `linarith`, `exact?`/`apply?` suggestions, and
   `aesop` over long bespoke arguments.
4. After each edit, re-check that file with
   `mcp__lean-lsp__lean_diagnostic_messages` and iterate until it is clean.
5. When a file looks done, verify it has no stubbed proofs with
   `mcp__lean-lsp__lean_verify` — the reported axioms must NOT contain `sorryAx`.
6. Repeat until no `.lean` file contains a `sorry` and every file compiles
   cleanly. Do the final compile check per file with `lake env lean <file>`:
   exit code 0 and no output means it compiles. This is the exact command the
   grader uses, so it is the source of truth.

Tips:
- Use the lean-lsp tools (`mcp__lean-lsp__*`) as your primary feedback loop; they
  are far faster than compiling per change.
- Do NOT trust `lake build` as a compile check: the `.lean` files here are not
  lake library targets, so `lake build` reports `Build completed successfully
  (0 jobs)` without ever compiling them. Always confirm with
  `lake env lean <file>`, which compiles the file by path.
- If a goal looks false or unprovable from the given hypotheses, re-read the
  statement: you likely misread a binder or a coercion. Do not "fix" it by
  changing the statement — finish the proof as stated.
- Non-trivial proofs routinely take many rounds of compile-error fixing. Keep
  iterating against the diagnostics rather than guessing."""
# END PROVER_PROMPT (docs literalinclude end marker -- keep adjacent)

# Directories never worth copying into the agent workdir.
_IGNORE = shutil.ignore_patterns(".lake", ".git", "*.tar.gz")


class AgentProver(AutomatedProver):
    """Generate proofs by driving an agent CLI harness in a compute backend.

    Composes an :doc:`agent harness </api/harness>` (the *agent* concern) with a
    :class:`~open_atp.backends.base.ComputeBackend` (the *compute* concern): the
    harness edits the staged ``.lean`` files in place, then the shared
    :class:`~open_atp.verify.Verifier` does the final compile/sorry/axiom check.
    ``codex``, ``opencode``, ``axproverbase``, and ``vibe`` are this prover on a
    different harness.

    Parameters
    ----------
    backend : ComputeBackend
        The sandbox the agent runs in. Generation reuses it via a live session and
        verification runs in that same hot sandbox.
    harness : Harness, optional
        The harness to drive and its knobs:
        :class:`~open_atp.harness.ClaudeCodeHarness` (default),
        :class:`~open_atp.harness.CodexHarness`,
        :class:`~open_atp.harness.OpenCodeHarness`,
        :class:`~open_atp.harness.VibeHarness`, or
        :class:`~open_atp.harness.AxProverBaseHarness`. Carries ``model``/``effort``
        plus any harness-specific knobs. Plugins are Claude-only and live on
        :class:`~open_atp.harness.ClaudeCodeHarness`.
    skills : list[str], optional
        Skills to mount into the agent workdir, each a name (resolved from the
        vendored ``leanprover/skills`` catalog) or a full path to a ``SKILL.md``
        tree. Default ``["lean-proof"]``; an empty list mounts none. Staged into
        every skill-supporting harness's location; ignored by ax-prover.
    timeout_s : int
        Wall-clock budget for the generation run, in seconds. Default ``1800``.

    Attributes
    ----------
    prover_prompt : str
        The prover's own prompt handed to the agent, before any user prompt.

    Examples
    --------

    Construct the prover directly, wiring up a harness and a backend:

    >>> from open_atp.backends.docker import DockerBackend
    >>> from open_atp.harness import CodexHarness
    >>> from open_atp.provers.agent_prover import AgentProver
    >>> backend = DockerBackend()
    >>> prover = AgentProver(harness=CodexHarness(effort="high"), backend=backend)
    >>> prover.harness.model
    'gpt-5.5'

    Or build the same prover from the standard catalog by name, taking its
    baked-in defaults (see :func:`~open_atp.config.standard_prover`):

    >>> from open_atp import standard_prover
    >>> prover = standard_prover("codex", backend=DockerBackend())
    >>> prover.name, prover.harness.name
    ('agent', 'codex')

    Complete a task's ``sorry``\\s with
    :meth:`~open_atp.provers.base.AutomatedProver.prove`, here on a bundled example
    (this runs the agent in Docker and bills it):

    >>> import tempfile
    >>> from open_atp.examples import EXAMPLE, example_task
    >>> task = example_task(EXAMPLE.MUL_REORDER)
    >>> result = prover.prove(task, tempfile.mkdtemp())  # doctest: +SKIP
    >>> result.success  # doctest: +SKIP
    True
    """

    name = "agent"

    def __init__(
        self,
        *,
        backend: ComputeBackend,
        harness: Harness | None = None,
        skills: list[str] | None = None,
        timeout_s: int = 1800,
    ) -> None:
        super().__init__(backend=backend, timeout_s=timeout_s)
        self.harness = harness or ClaudeCodeHarness()
        self.skills = skills if skills is not None else ["lean-proof"]

    @property
    def prover_prompt(self) -> str:
        """The prover's own prompt handed to the agent, before any user prompt."""
        return PROVER_PROMPT

    def _generate(
        self, task: ProofTask, wd: Path, logs_dir: Path, result: ProofResult
    ) -> None:
        # 1. Stage the project so the workdir is a complete project to edit in place.
        shutil.copytree(task.project.root, wd, dirs_exist_ok=True, ignore=_IGNORE)

        # 2. Snapshot the original .lean contents for the post-run diff.
        original = {
            p.relative_to(wd).as_posix(): p.read_text()
            for p in wd.rglob("*.lean")
            if ".lake" not in p.parts
        }

        # 3. Stage the workdir: the harness launch script, then the config's skills
        #    (this prover owns the list; the harness owns where they land), then prompt
        #    (this prover's own prompt + the task's optional user prompt).
        harness = self.harness
        harness.stage_wd(wd)
        harness.stage_skills(wd, [resolve_skill(s) for s in self.skills])
        harness.write_prompt(wd, compose_prompt(self.prover_prompt, task.user_prompt))
        stdout_path = logs_dir / "stdout.txt"

        # 4. Run the agent and the final check in one persistent sandbox: generation
        #    and verification share the backend, so we keep the container hot between
        #    them and never pay a second spin-up. Mounts (credential dirs) are pinned
        #    when the session is created; per-command env is forwarded by _run_agent.
        #    The session must cover both phases -- the generation budget plus the
        #    verifier's own -- since both run before the sandbox is torn down; the
        #    backend adds its sync headroom on top.
        _, mounts = self._auth(harness)
        log.info(
            "agent generation started",
            extra={
                "harness": harness.name,
                "model": harness.model,
                "effort": harness.effort,
            },
        )
        with self.verifier.backend.session(
            wd, mounts=mounts, timeout_s=self.timeout_s + self.verifier.timeout_s
        ) as session:
            lines, stderr = self._run_agent(
                wd, harness, stdout_path, session, timeout_s=self.timeout_s
            )
            self._download_wd(wd, session)
            # Parse (which reads harness usage files still in wd) before
            # _download_logs relocates them out.
            self._fill_result(result, harness, wd, original, lines)
            self._download_logs(harness, wd, logs_dir, stderr)
            result.verification = self.verifier.verify(LeanProject(wd), session=session)

    def _download_wd(self, wd: Path, session: ComputeSession) -> None:
        """Bring the agent's edits onto the host at ``wd``.

        A tar pull for a live Modal session; a no-op for bind-mounted Docker (where the
        agent wrote ``wd`` directly).
        """
        log.debug("downloading agent edits", extra={"wd": str(wd)})
        session.sync_out()

    def _download_logs(
        self, harness: Harness, wd: Path, logs_dir: Path, stderr: str = ""
    ) -> None:
        """Populate ``logs_dir`` with the run record beside the live ``stdout.txt``.

        The streamed stdout is already teed into ``logs_dir/stdout.txt`` as it arrives;
        here we relocate any harness-specific rich logs out of the workdir (no-op for
        the CLI harnesses) and write the captured ``stderr``.
        """
        harness.collect_logs(wd, logs_dir)
        if stderr:
            (logs_dir / "stderr.txt").write_text(stderr)

    def _fill_result(
        self,
        result: ProofResult,
        harness: Harness,
        wd: Path,
        original: dict[str, str],
        lines: list[str],
    ) -> None:
        """Token totals -> cost, then diff the workdir against the staged originals."""
        parsed = harness.parse_result(lines, wd)
        cost = parsed.cost_usd
        if cost is None:
            cost = compute_cost_usd(
                self.harness.model, parsed.input_tokens, parsed.output_tokens
            )

        completed: dict[str, str] = {}
        for path in sorted(wd.rglob("*.lean")):
            if ".lake" in path.parts:
                continue
            rel = path.relative_to(wd).as_posix()
            content = path.read_text()
            if original.get(rel) != content:
                completed[rel] = content

        result.completed_files = completed
        result.cost_usd = cost
        result.metadata = {
            "harness": harness.name,
            "model": self.harness.model,
            "effort": self.harness.effort,
            "input_tokens": parsed.input_tokens,
            "output_tokens": parsed.output_tokens,
            "stop_reason": parsed.stop_reason,
        }

    def _auth(self, harness: Harness) -> tuple[dict[str, str], list[tuple[str, str]]]:
        """Map the harness's resolved :class:`AgentAuth` onto backend env + mounts."""
        auth = harness.agent_auth()
        home = self.verifier.backend.container_home
        mounts = [(str(src), f"{home}/{dest}") for src, dest in auth.mounts]
        return auth.env, mounts

    def _run_agent(
        self,
        workdir: Path,
        harness: Harness,
        stdout_path: Path,
        session: ComputeSession,
        *,
        timeout_s: int | None = None,
    ) -> tuple[list[str], str]:
        """Resolve auth, launch the agent in the live ``session``, and tee its stdout.

        Each streamed event line is written to ``stdout_path`` (opened in append mode,
        so a multi-round caller accumulates one transcript) as it arrives -- the live
        run log -- and collected to return for token/cost parsing. Returns
        ``(stdout_lines, stderr)``: the lines plus the run's captured stderr (e.g.
        Modal's ``modal_stderr.txt``), which the prover writes to ``logs/stderr.txt``.

        The agent execs in the persistent ``session`` -- the same hot sandbox that
        stays up for the verifier afterwards. Mounts (credential dirs) were pinned when
        the session was created; only per-command env is forwarded here.

        ``timeout_s`` is the agent's wall-clock budget: on Modal it is enforced by the
        Sandbox exec (killed with Sandbox slack left to pull the workdir), and a
        multi-round caller passes the *remaining* budget so successive rounds share one
        Sandbox lifetime. Docker ignores it (bind-mounted, no self-terminate).

        Isolated (it owns credential resolution + the backend call) so tests can
        stand in a fake run -- write a solved file, return a captured stream --
        without Docker or credentials.
        """
        env, _ = self._auth(harness)
        lines: list[str] = []

        def drain(handle: CommandHandle, sink: TextIO) -> None:
            for line in handle.stream():
                sink.write(line + "\n")
                sink.flush()
                lines.append(line)

        with stdout_path.open("a", encoding="utf-8") as sink:
            handle = session.exec(harness.command, env=env, timeout_s=timeout_s)
            drain(handle, sink)
            result = handle.wait()
            self._log_agent_result(harness, result, lines)
        return lines, result.stderr

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
