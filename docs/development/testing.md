# Running tests

`open-atp` ships a `pytest` suite with three tiers, split by what each test needs to
run. They are separated by [marker](https://docs.pytest.org/en/stable/example/markers.html)
so the fast, free, offline tier runs by default and the slow, billable, or
compute-bound tiers are opt-in. This guide assumes the development setup in {doc}`index`.

## Tiers at a glance

| Tier | Marker | Needs | Default |
| --- | --- | --- | --- |
| Unit | *(none)* | nothing | **runs** |
| Compute backend | `docker`, `modal` | Docker image / Modal token | skipped |
| Agent capability | `agent_api` | agent CLI creds (billable) | skipped |
| End-to-end | `docker`/`modal` + `*_api` | backend **and** API creds (billable) | skipped |

The default `addopts` in `pyproject.toml` excludes every gated marker:

```
-m 'not docker and not modal and not aristotle_api and not agent_api and not numina_api'
```

So a bare `make test` (or `uv run pytest`) runs only the unit tier. Use `-n 0` to run
serially when debugging (the default is `-n 5` via `pytest-xdist`).

## Unit tests

The default tier: fast, offline, no credentials, no sandbox. Covers the registry and
dispatch (`test_api.py`), CLI parsing (`test_cli.py`), config (`test_config.py`),
cost/token capture (`test_capture.py`), and the harness/prover construction paths
(`tests/harness/`, `tests/provers/`) — everything that can be exercised without
spending money or launching compute.

```bash
make test            # uv run pytest, gated markers excluded
uv run pytest tests/test_api.py        # a single module
uv run pytest -n 0 tests/test_cli.py   # serial, for debugging
```

`make check` (lint + typecheck + `check-provers` + this tier) is the pre-push gate.

```{note}
Even when running a gated test file by explicit path, the default marker exclusion
still applies — slow/billable/compute tests skip unless you opt into their marker.
```

## Compute-backend tests

The `docker` and `modal` markers exercise the {class}`~open_atp.backends.base.ComputeBackend`
implementations and the shared {class}`~open_atp.verify.Verifier` against the real
Lean+Mathlib sandbox. They are **opt-out** by default — skipped unless you select the
marker — because they need the built image or a Modal token.

```bash
make build-image     # docker build -t open-atp:latest images/  (first time)
make test-docker     # uv run pytest -m docker

uv run open-atp build-modal-image --name open-atp --app open-atp
make test-modal      # uv run pytest -m modal  (needs MODAL_TOKEN_ID/SECRET)
```

See {doc}`../guides/docker` and {doc}`../guides/modal` for backend setup details.

## Agent-capability tests

`tests/harness/test_capabilities.py` is an **end-to-end capability probe**: it drives a
real agent CLI (`claude_code` / `codex` / `opencode` / `vibe`) through a real backend
with a "make exactly one tool call, then stop" prompt, then inspects the streamed event
stream to confirm the four capabilities the {class}`~open_atp.provers.agent_prover.AgentProver`
design depends on actually work *inside the sandbox* — not merely that the harness
configured them (the unit tier covers configuration). The four probes:

- **shell + Lean toolchain** — the agent can `lake build` a fresh module;
- **skills** — a staged skill is invocable from the harness-specific mount point;
- **lean-lsp MCP** — the `mcp__lean-lsp__*` tools are wired up; and
- **edit sync-back** — a file the agent writes lands on the host workdir.

Every probe is marked `agent_api` (billable, needs agent creds) and carries a backend
marker, so you select both dimensions:

```bash
make test-agent                       # uv run pytest -m agent_api (both backends)
uv run pytest -m 'agent_api and docker'   # pin to the Docker backend
```

Each case **skips** (never fails) when its agent creds or its backend are unavailable.
Per-agent prerequisites (`CLAUDE_CODE_OAUTH_TOKEN`, `~/.codex/auth.json`,
`DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`) are listed in the module docstring; populate
`.env` from `.env.example`.

## End-to-end prover tests

`tests/test_e2e_provers.py` is the single live test the project needs: one parametrized
function over `backend (docker, modal) × prover`, routed through `standard_prover` and
{meth}`~open_atp.provers.base.AutomatedProver.prove` against a trivial one-`sorry`
fixture — so it exercises the backend factory, the catalog, and the shared verifier
exactly as a caller would. Each case is gated twice and **skips** when either side is
missing:

- the **compute** marker (`docker` / `modal`); and
- the **API** marker (`aristotle_api` / `agent_api` / `numina_api`), excluded by default
  because it is billable.

```bash
uv run pytest -m aristotle_api              # Aristotle on every ready backend
uv run pytest -m 'agent_api and docker'     # Claude Code, Docker backend only
make test-aristotle                         # convenience target
```

`test_catalog_is_fully_covered` is a fast, unmarked guard that runs in the unit tier and
**fails** if a new prover lands in the standard catalog without an end-to-end row here —
so adding a prover forces you to wire up its live test.

## Coverage

```bash
make cov         # term + HTML (htmlcov/) + XML (coverage.xml)
make cov-open    # build and open the HTML report
make cov-clean   # remove coverage artifacts
```

Integration artifacts (agent logs, workdirs) land in `tests/.runs/` (gitignored).
