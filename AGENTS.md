# AGENTS.md

Developer guide for **open-atp** (Open Automated Theorem Proving). Read this
before making changes. The user-facing overview lives in [README.md](README.md);
this file is the engineering reference.

## What this project does

Upload one or more Lean files containing `sorry`, run them through proof-synthesis
backends, and get back **verified** completed proofs with metadata (verification
status, cost, duration). Every prover — including the hosted Aristotle — funnels its
output through one **shared verifier** that compiles the candidate in a Lean+Mathlib
sandbox and checks it compiles, is sorry-free, and is axiom-clean.

### Two primitives + thin generators

1. **`ComputeBackend`** (`backends/`) — run a command over a working directory inside a
   Lean+Mathlib sandbox. Two impls: `DockerBackend`, `ModalBackend`.
2. **`Verifier`** (`verify.py`) — compile a candidate project in a backend and
   report `verified` / `sorry_free` / `axioms`.

```
ComputeBackend (docker | modal)         ← the sandbox primitive
        │
        ├── Verifier  ──────────────────← shared final check (ALL provers)
        │
AutomatedProver (provers/base.py, base)
 ├── AgentProver      coding-agent harness (claude/codex/opencode/axproverbase/vibe) + lean-lsp-mcp
 ├── NuminaProver     configured AgentProver: claude + vendored Numina assets + round loop
 └── AristotleProver  remote `aristotle submit --project-dir --wait` (no local generation sandbox)
```

### Input contract

Submit a **full lake project** (carries `lean-toolchain` + `lake-manifest.json`). The
verifier **rejects** projects whose toolchain doesn't match the sandbox image's pin
(`ToolchainMismatch`) instead of failing deep in a build. The CLI can also take bare
`.lean` files and stage them into the pinned skeleton. One Mathlib image to start
(pinned Lean/Mathlib **v4.28.0**); `image` is a config field so more can be added.

## Project structure (high-level)

```
src/open_atp/
  config.py         standard_prover + STANDARD_PROVERS registry (build provers by name)
  __main__.py       `open-atp prove | benchmark | download | auth-status | build-docker-image | build-modal-image` CLI
  auth.py           AuthStatus: a prover's credential, its expiry, and its state
  images/           image name + toolchain pins (DEFAULT_IMAGE, DEFAULT_TOOLCHAIN)
  lean.py           LeanProject, ProofTask, create_project (the Lean input contract)
  verify.py         VerificationReport, Verifier (the shared final check)
  benchmark.py      run_benchmark: run named provers x named tasks, tabulate results
  backends/         base.py  docker.py  modal.py            (ComputeBackend impls)
  provers/          base.py  agent_prover.py  numina.py  numina_tracker.py  aristotle.py
  harness/          coding-agent CLIs staged into the sandbox:
                      base.py  claude_code.py  codex.py  opencode.py
                      axproverbase.py  vibe.py  cost.py  _catalog.py  _numina.py  _paths.py
                    assets/  scripts/*.sh  configs/mcp.json  vibe/lean-standin.toml

images/             Dockerfile (Mathlib base image) + lean/ skeleton (toolchain, lakefile)
vendor/             vendored third-party assets, tracked to upstream SHAs (see VENDOR.md in each)
  numina/             Numina skills + prompts (round-loop prover)
  leanprover-skills/  host-agnostic Lean skills
  lean4-skills/       Claude `lean4` plugin
tests/              pytest suite (+ tests/.runs/ integration artifacts, gitignored)
docs/               Sphinx docs (user_guide/, provers/, agent_harness/, api/)
refs/               read-only symlinks to reference projects (NEVER modify or commit)
```

The README's `Layout` section predates the `harness/` split — trust the tree above.

### Vendored code

`vendor/*` is upstream third-party code pinned to a SHA (each has a `VENDOR.md`).
Ruff is configured with `extend-exclude = ["vendor"]` — **do not reformat or lint
vendored code**, and keep its upstream style. It ships in the wheel via
`force-include` and is resolved at runtime by `harness/_paths.py` (wheel:
`open_atp/vendor/<name>`; checkout: repo-root `vendor/<name>`).

## Provers

**`STANDARD_PROVERS` in `config.py` is the source of truth for the prover roster.**
Its keys are exactly the names accepted by the `prove` positional `prover` and the
`benchmark --provers` flag, and each value spells out that prover's wiring — which
harness backs it, and any model / provider / auth overrides. Read it there rather
than maintaining a second list here; `standard_provers()` returns the names at
runtime. Per-prover presentation metadata (company, paper, source, skills, MCP)
lives in `docs/provers.yaml` — see [Docs: prover comparison table](#docs-prover-comparison-table).

Most entries are the shared `AgentProver` on a different harness; the rest are
standalone provers. Agentic harnesses share **lean-lsp-mcp** as their LSP server.
The shared `Verifier` does the final compile/sorry/axiom check regardless of which
tool generated the proof.

## Tooling

- **Python ≥ 3.12**, packaged with **hatchling**, deps managed by **uv** (`uv.lock`).
- **ruff** — lint (`E,F,I,UP`) + format, line length 88, excludes `vendor`.
- **mypy** — `strict`, `files = ["src/open_atp"]`.
- **pytest** — `pytest-cov`, `pytest-xdist` (default `-n 5`).
- **lefthook** — pre-commit runs ruff check, ruff format --check, and mypy on staged
  `*.py` (with `--force-exclude` so vendored code is skipped). Install with
  `uv run lefthook install`.
- **Sphinx** (furo + myst) for docs; Read the Docs config in `.readthedocs.yaml`.
- CLI entry point: `open-atp` → `open_atp.__main__:main`.

## Makefile commands

```
make install         uv sync
make test            pytest, skipping docker/modal/live-API tests (default markers)
make test-docker     -m docker     (requires the built image)
make test-modal      -m modal      (requires a Modal token)
make test-aristotle  -m aristotle_api   (live, needs ARISTOTLE_API_KEY)
make test-agent      -m agent_api       (live + billable, needs creds)
make cov             pytest with coverage → htmlcov/, coverage.xml
make cov-open        build + open the HTML coverage report
make cov-clean       remove coverage artifacts
make lint            ruff check src tests
make format          ruff format + ruff check --fix on src tests
make typecheck       mypy
make check           lint + typecheck + test
make build-image     docker build -t open-atp:latest images/
make docs            sphinx-build -W -b html docs docs/_build/html
make docs-serve      live-reload docs
make docs-clean      remove built docs
make clean           remove build + cache artifacts
```

Run `make check` before pushing.

## Testing

Default `addopts`: `-m 'not aristotle_api and not agent_api and not numina_api' -n 5`.
The live/billable credentialed suites are **opt-out by default** and run only when you
select their marker. Markers (`pyproject.toml`):

- `docker` — needs the `open-atp` Docker image (opt-out: `-m 'not docker'`)
- `modal` — launches a Modal sandbox (opt-out: `-m 'not modal'`)
- `aristotle_api` — live Aristotle API (opt-in: `-m aristotle_api`)
- `agent_api` — live agent CLI, billable + creds (opt-in: `-m agent_api`)
- `numina_api` — live NuminaProver, billable + creds (opt-in: `-m numina_api`)

> Project convention: when running tests (even by explicit path), exclude the
> docker / modal / `*_api` markers by default — they are slow, billable, or need
> external compute. `make test` already does this. Use `-n 0` to run serially when
> debugging.

Integration artifacts (agent logs, workdirs) land in `tests/.runs/` (gitignored).

## Compute setup: Docker vs. Modal

Both backends run the shared `Verifier` **and** the agentic provers end-to-end against
the Mathlib image. Pick a backend with `--compute {docker,modal}`.

- **Docker** (`DockerBackend`) — bind-mounts the workdir; uses `images/Dockerfile`,
  runs as the `agent` user. Local; build the image first:
  ```bash
  docker build -t open-atp:latest images/      # or: make build-image / open-atp build-docker-image
  uv run pytest -m docker
  ```
- **Modal** (`ModalBackend`) — pushes/pulls the workdir around an isolated Sandbox
  filesystem; runs as **root**, so its image is built programmatically with the same
  toolchain installed globally. Publish the image, then run the parity suite:
  ```bash
  uv run open-atp build-modal-image --name open-atp --app open-atp
  uv run pytest -m modal          # needs MODAL_TOKEN_ID / MODAL_TOKEN_SECRET
  ```
  `ModalBackend`'s `image` (sans `:tag`) must match the `--name` you publish under.

Run a single prover on Modal instead of Docker:
```bash
uv run open-atp prove path/to/project runs/example claude --compute modal
```

## CLI quick reference

```
open-atp prove <path> <output> <prover> [options]   # lake project dir, or a bare .lean file
  -c/--compute {docker,modal}     default docker
  --json                          emit the ProofResult as JSON

open-atp benchmark <dataset> <output> [options]     # directory of proof tasks
  --config FILE                   YAML provers/tasks/compute/workers; CLI flags override
  -p/--provers   comma-separated names (default: every config/standard prover)
  -t/--tasks     comma-separated task names (default: all)
  -c/--compute {docker,modal}     default docker
  -w/--workers N                  default 1
  --json                          emit the BenchmarkResult as JSON

open-atp download <dataset> <output>                # lands at <output>/<dataset>

open-atp auth-status [--json]                       # per-prover credential + expiry

open-atp build-docker-image       [-t/--tag TAG] [-C/--no-cache]
open-atp build-modal-image        [-n/--name N] [-a/--app A] [-f/--force]
```

Programmatic verify:
```python
from open_atp.lean import LeanProject
from open_atp.verify import docker_verifier
report = docker_verifier().verify(LeanProject("path/to/lake/project"))
print(report.verified, report.sorry_free, report.axioms)
```

## Environment / secrets

Copy `.env.example` → `.env` (gitignored, never committed). All keys are needed only
for the corresponding **live** test or harness; absent keys make the dependent
skill/test degrade or skip. `open-atp auth-status` tabulates what this host actually
has, per prover — including how long the file-backed OAuth logins (codex, grok, kimi)
stay valid. Those refresh only when their CLI runs **on the host**: a sandbox gets a
copy, so a token it refreshes there dies with the container.

- `ARISTOTLE_API_KEY` — `pytest -m aristotle_api`
- `CLAUDE_CODE_OAUTH_TOKEN` — `agent_api` test with default claude_code harness
  (`claude setup-token`)
- `GEMINI_API_KEY` / `OPENAI_API_KEY` / `LEAN_LEANDEX_API_KEY` — Numina helper skills
- `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` — `axproverbase` (raw provider key matching
  the configured `model`); `TAVILY_API_KEY` optional (ax-prover web search)
- `DEEPSEEK_API_KEY` — the `deepseek` prover (opencode harness, `auth="api_key"`)
- `opencode auth login` -> xAI (writes an `xai` entry to opencode's `auth.json`) —
  the `grok` prover (opencode harness, `auth="login"`); that entry is mounted into the
  sandbox (no API key), so runs bill against your xAI plan
- `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` — Modal backend

## Docs: API reference convention

Write **stock numpydoc**. The conventions below are the numpy/scipy/sklearn ones; the
only project-specific parts are the two `Image` gotchas and the `code-block` rule for
non-runnable examples.

The API pages (`docs/api/*.md`) are Sphinx `autoclass` directives; **numpydoc** renders
the class docstring's `Parameters`/`Attributes` sections, and a single
`autodoc-skip-member` hook in `docs/conf.py` (`_skip_data_attributes`) drops class
members that are plain data. So the split is:

- **Methods and `@property` are members**, enumerated by `autoclass` and documented in
  **their own docstring**, in full. Document each **once, on the class that defines it.**
- **Members render in source order** (`autodoc_member_order = "bysource"`), so **define
  properties right after `__init__`** and order the methods deliberately — the page reads
  in the order you write. Don't use `groupwise`: autodoc scores methods (50) ahead of
  properties (60), which is backwards.
- **`@property` is documented like a method** — never listed in `Attributes`. numpydoc
  overwrites an `Attributes` entry naming a property with the first sentence of that
  property's docstring, so the prose you write there is silently discarded and the
  summary gets cut at the first `. ` (an `e.g.`/`i.e.`/`etc.` truncates it mid-thought).
  Writing it on the property renders the whole docstring, and the type comes from the
  return annotation.
- **Constructor params and plain instance state** live **only in the class docstring**,
  under `Parameters`/`Attributes`. The hook hides them as members so they render once,
  from the prose. Never re-list them with `:members:`.
- **List each name once — `Parameters` *or* `Attributes`, never both.** A constructor arg
  stored verbatim as an attribute is documented only under `Parameters`; readers know
  `self.<arg>` exists without it being repeated. `Attributes` is reserved for plain
  non-property state that is **not** in the signature.
- **Dataclasses use `Parameters`**, like any other class — its fields are its
  constructor signature. Do not put them under `Attributes`.
- **Inheritance**: numpydoc does *not* walk the MRO, so each leaf class must
  **re-document every constructor param it accepts, including inherited ones** (e.g.
  `backend`/`timeout_s` from `AutomatedProver`) — otherwise they don't render. Pages do
  **not** use `:inherited-members:`: an inherited method or property (e.g. `prove`)
  appears only on its base class, not on each child.

### Parameter types and defaults

`numpydoc_xref_param_type = True`, so **every token on a type line is turned into a
cross-reference**. Write types as real code, not prose:

- Use PEP 585 generics — `tuple[str, ...]`, `Mapping[str, str]`, `dict[str, object]`,
  `Sequence[tuple[str, str]]` — not `list of float`. Use `X or None` for unions in a
  `Returns` type.
- Project names resolve through `numpydoc_xref_aliases` in `docs/conf.py`. **Add new
  public names there** so they link; unaliased names render as plain text.
- ⚠️ Bare **`Image` is aliased to `modal.Image`**. Always write
  `~open_atp.images.Image` for the sandbox image or the link points at Modal's docs.

**Defaults go on the type line, never in the description**: `timeout_s : int, default
1800`, `effort : str, default "high"`, `image : ~open_atp.images.Image, default
DEFAULT_IMAGE`. Use `, optional` only when the default is `None`/empty and there is no
literal worth naming; if that default needs explaining, a short trailing sentence
(`Defaults to an empty mapping.`) is fine. Never write `Default ``5``.` as prose.

### Examples

`>>>` blocks are **executed twice**: by pytest (`--doctest-modules` over `src/open_atp`)
and by `make docs-doctest`. Only the Sphinx path gets `doctest_global_setup` (which stubs
`AutomatedProver.prove`), so a docstring `>>>` must stand on its own.

- Use `>>>` only when the example **genuinely runs offline** — construction, pure
  computation, cheap properties. No Docker, no network, no credentials, no billing.
- Otherwise use `.. code-block:: python` (see `ComputeSession`). It is still rendered and
  read, just not executed. Prefer this over stubbing: a doctest against a mock documents
  the mock, not the library.
- `# doctest: +SKIP` is for the **last line or two of an otherwise-live example** — e.g.
  the prover docstrings, where the setup really runs and only `prove()` is skipped. Don't
  use it to smuggle a wholly non-runnable example past the runner.
- Put a blank line after the `--------` underline, and a prose sentence before each block
  saying what it shows.

### Practical rules

- **Do not** add `:exclude-members:` for attributes, params, or `name` — the hook already
  handles them. The only legitimate `:exclude-members:` is to hide an **overridden
  method** from a child page so it stays documented on the base only (current use:
  `stage` on the harness impls).
- A new plain attribute only shows up if you add it to the docstring `Attributes`
  section. A new `@property` shows up automatically, with its own docstring.
- `make docs` builds with `-W` (warnings are errors) — a broken xref or duplicate fails
  the build. `make check` also runs `make docs-doctest`.

## Docs: prover comparison table

The prover table in both `README.md` and `docs/provers/index.md` is generated from a
single source of truth, `docs/provers.yaml` (skills / MCP / company / paper / source
metadata). There is no harness table or Harness column: most harnesses are 1:1 with a
model, so `source` names each prover's own CLI/model. The one provider-agnostic harness
(OpenCode) is documented as a subsection page under Provers (`docs/provers/opencode.md`)
with its model-specific provers (e.g. `deepseek`) nested beneath it in that page's own
toctree. **Edit the YAML, never the table by hand.**

- `docs/_ext/provers_table.py` renders the table. As a Sphinx extension
  (`provers_table` in `conf.py`) its `builder-inited` hook writes the gitignored
  `docs/provers/_table.md`, which `index.md` pulls in via `{include}`.
- It also writes a gitignored per-prover metadata bar, `docs/provers/_meta_<page>.md`
  (id / company), which each `docs/provers/<page>.md` includes right under its title.
  `company` is shown here even though it's not a table column — keep it in the YAML.
  (The `opencode` subsection page is not a prover row, so it has no generated meta.)
- The README table is materialized between `<!-- BEGIN/END PROVER TABLE -->` markers
  (GitHub can't run Sphinx). Run `make gen-provers` after editing the YAML and commit
  the README change.
- `make check-provers` (wired into `make check`) fails if the README table is stale.

## Conventions

- Commit directly to `main` unless told otherwise; warn before committing work that
  clearly belongs on another branch.
- Never modify or commit anything under `refs/` (read-only reference symlinks) or
  reformat anything under `vendor/` (upstream-tracked).
- Keep `mypy --strict` and ruff clean; run `make check` before pushing.
- A plain `threading.Thread` does **not** inherit the caller's `contextvars` context
  (each new thread starts fresh), so `structlog.contextvars`-bound fields (`prover`,
  `run_id`, `task`, set via `bound_contextvars` in `provers/base.py`) silently vanish
  from any logging done inside the thread. When running blocking work on a worker
  thread, capture `contextvars.copy_context()` on the calling thread and run the
  target via `ctx.run(fn)` (see `_run_bounded` in `backends/modal.py`).
