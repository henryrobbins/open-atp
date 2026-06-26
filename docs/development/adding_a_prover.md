# Adding a prover

A prover is a *candidate generator* — it turns a {class}`~open_atp.lean.ProofTask` into
completed Lean files, then the shared {class}`~open_atp.verify.Verifier` does the final
compile / sorry / axiom check. This guide assumes the development setup in {doc}`index`. There are two ways to add one,
depending on what you are wrapping:

- **A new coding-agent CLI** (the common case) — Claude Code, Codex, OpenCode, Vibe and
  ax-prover are all the *same*
  {class}`~open_atp.provers.agent_prover.AgentProver` composed with a different
  {class}`~open_atp.harness.base.Harness`. To add another CLI you write a **harness**,
  not a prover. Start at [Route A](#route-a-a-new-agent-harness).
- **A genuinely different prover** — one that doesn't drive an agent CLI over a staged
  workdir (e.g. a hosted API like Aristotle). Subclass
  {class}`~open_atp.provers.base.AutomatedProver` directly. See
  [Route B](#route-b-a-new-prover-type).

## Route A: a new agent harness

A {class}`~open_atp.harness.base.Harness` adapts one agent CLI to the sandbox: its
launch script, credential forwarding, and token/cost parsing. The *where it runs*
concern (Lean+Mathlib, lean-lsp-mcp) is the injected
{class}`~open_atp.backends.base.ComputeBackend`, so a harness never touches compute.

### 1. Write the harness class

Add `src/open_atp/harness/<cli>.py` subclassing `Harness`. Use the existing harnesses as
templates — `vibe.py` is the smallest, `claude_code.py` the most complete. You must set:

- `name` (the `ClassVar` registry key — what `--provers agent:<cli>` and config `type`
  resolve to) and, if the CLI consumes skills, `skills_dest` (the workdir-relative
  directory skills mount into, e.g. `.claude/skills`).
- `_agent_command()` — returns the bash launch script. Most harnesses store the script
  under `assets/scripts/<cli>_agent.sh` and read it via `_paths._SCRIPTS`; the base
  `command` property already exports `$PROMPT` and runs it.
- `_parse_lines()` — parse the agent's streamed stdout into a
  {class}`~open_atp.harness.base.HarnessRunResult` (token totals, cost, stop reason).

Override the credential hooks as needed: `_static_env()` (non-secret env like
`IS_SANDBOX`), `_required_env()` (resolve API keys, raising if absent), and
`_home_dirs()` (host dirs to mount under the sandbox `$HOME`). If the CLI writes a rich
log *inside* the workdir, override `collect_logs()` to relocate it so the downloaded
workdir stays a clean proof project.

### 2. Register it

Add the class to `_HARNESSES` in `src/open_atp/harness/__init__.py` (the map is keyed on
`Harness.name`) and export it from `__all__`. {mod}`open_atp.config` dispatches a harness
spec's `type` through this registry.

### 3. Add a standard-catalog entry

Add an `agent:<cli>` key to `STANDARD_PROVERS` in `src/open_atp/config.py` so the prover
is buildable by name (and shows up in `open-atp solve --provers ...` /
`standard_provers()`):

```python
STANDARD_PROVERS = {
    ...
    "agent:<cli>": {"type": "agent", "harness": {"type": "<cli>"}},
}
```

### 4. Wire up credentials

If the CLI needs a key, document it in `.env.example` and the *Environment / secrets*
section of {github}`AGENTS.md </blob/main/AGENTS.md>`. Absent keys should make the
prover degrade or skip, never hard-crash an unrelated run.

### 5. Document it

- Add an autoclass for the harness to `docs/api/harness.md` (follow the existing
  entries: `:show-inheritance:` and `:exclude-members: stage`).
- Add a prover page `docs/provers/<cli>.md` and list it in the hidden toctree in
  `docs/provers/index.md`.
- Add a row to `docs/provers.yaml` (the single source of truth for the comparison
  table — never edit the tables by hand), then run `make gen-provers` and commit the
  regenerated README block. `make check-provers` (run by `make check`) fails if the
  README table drifts.

### 6. Test it

Add unit tests under `tests/harness/` (parsing, credential resolution, staging) mirroring
`tests/harness/test_vibe.py`. The live, billable end-to-end path is covered by the
`agent_api` marker — opt-in only (`make test-agent`), never in the default suite.

## Route B: a new prover type

For a prover that isn't an agent-over-workdir, subclass
{class}`~open_atp.provers.base.AutomatedProver` directly (see
`src/open_atp/provers/aristotle.py`). Implement the generation step; the base class owns
the shared verify so you still get the same final check for free. Then:

1. Register the class in `_PROVERS` in `src/open_atp/provers/__init__.py` (keyed on the
   `type` name) and export it from `__all__`.
2. Add a `STANDARD_PROVERS` entry (a bare `{"type": "<name>"}` if it needs no harness).
3. Document and test it as in Route A steps 4–6 (a `docs/provers/<name>.md` page and a
   `docs/provers.yaml` row).

## Verify the result

```bash
make check        # lint + typecheck + test (includes check-provers)
make docs         # builds with -W, so a broken xref or stale table fails
```

Then run the prover end-to-end against a bundled example:

```bash
open-atp solve <project> --provers agent:<cli>
```
