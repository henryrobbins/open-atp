# OpenATP

[![CI](https://github.com/henryrobbins/open-atp/actions/workflows/ci-python.yml/badge.svg)](https://github.com/henryrobbins/open-atp/actions/workflows/ci-python.yml)
[![codecov](https://codecov.io/gh/henryrobbins/open-atp/branch/main/graph/badge.svg?flag=src)](https://codecov.io/gh/henryrobbins/open-atp)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Docs](https://readthedocs.org/projects/open-atp/badge/?version=latest)](https://open-atp.readthedocs.io/en/latest/)

**Open Automated Formal Proof Synthesis.** Upload one or more Lean files containing
`sorry`, run them through leading proof-synthesis backends, and get back verified
completed proofs with metadata (verification status, cost, duration).

## Core idea

The whole platform reduces to two reusable primitives plus thin candidate generators:

1. **`ComputeBackend`** (`backends/`) — run a command over a working directory in a
   Lean+Mathlib sandbox. Two implementations: `DockerBackend`, `ModalBackend`.
2. **`Verifier`** (`core/verifier.py`) — compile a candidate project in a backend and
   report whether it compiles, is sorry-free, and is axiom-clean.

Every prover funnels its output through the **shared verifier**, including Aristotle.

```
ComputeBackend (docker | modal)         ← the sandbox primitive
        │
        ├── Verifier  ──────────────────← shared final check (ALL provers)
        │
AutomatedProver (base)
 ├── AgentProver      coding agent (claude/opencode/codex) + lean-lsp-mcp in sandbox
 ├── NuminaProver     configured AgentProver: claude + vendored Numina assets + round loop
 └── AristotleProver  remote `aristotle submit --project-dir --wait`, no sandbox to generate
```

## Input contract

Submit a **full lake project** (carries `lean-toolchain` + `lake-manifest.json`). The
verifier **rejects** projects whose toolchain doesn't match the sandbox image's pin
(`ToolchainMismatch`) rather than failing deep in a build. One Mathlib image to start;
`image` is a config field so more can be added without refactoring.

## Available provers

Each name below is accepted by the `--provers` CLI flag and the `Platform` registry
(`api.py`). The agentic provers all run as an `AgentProver` on a selectable *harness*
(a coding-agent CLI staged into the sandbox); the shared `Verifier` does the final
compile/sorry/axiom check regardless of which tool generated the proof.

| Prover name | Backing tool | Generation | Source / website |
| --- | --- | --- | --- |
| `aristotle` | Harmonic Aristotle (hosted) | remote API via `aristotlelib` | [harmonic.fun](https://www.harmonic.fun) · [aristotlelib](https://pypi.org/project/aristotlelib/) |
| `agent` | Claude Code (`claude_code` harness) | coding agent + lean-lsp-mcp | [anthropics/claude-code](https://github.com/anthropics/claude-code) |
| `agent:codex` | OpenAI Codex CLI | coding agent + lean-lsp-mcp | [openai/codex](https://github.com/openai/codex) |
| `agent:opencode` | opencode | coding agent + lean-lsp-mcp | [sst/opencode](https://github.com/sst/opencode) |
| `agent:axprover` | ax-prover (LangGraph Lean agent) | proposer→builder→reviewer loop | [Axiomatic-AI/ax-prover-base](https://github.com/Axiomatic-AI/ax-prover-base) ([fork](https://github.com/henryrobbins/ax-prover-base)) |
| `numina` | Numina skills/prompts on Claude Code | round-continuation loop | [vendor/numina/VENDOR.md](vendor/numina/VENDOR.md) |
| `vibe` | Mistral Vibe `lean` scaffold (Leanstral stand-in on Magistral; `--model` configurable) | hosted model, no GPU | [mistralai/mistral-vibe](https://github.com/mistralai/mistral-vibe) |

The shared LSP server used by the agentic harnesses is
[lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp).

## Compute backends

`DockerBackend` and `ModalBackend` both run the shared `Verifier` and the agentic
provers end-to-end against the Mathlib image. Docker bind-mounts the workdir; Modal
pushes/pulls it around an isolated Sandbox filesystem. Docker uses `images/Dockerfile`
(runs as the `agent` user); Modal runs as root, so its image is built
programmatically with the same toolchain installed globally (`build-modal-image`).

```bash
# Build the Mathlib base image (pins Lean/Mathlib v4.28.0).
docker build -t open-atp:latest images/

# Run the verifier test (compiles a trivial Mathematics-in-Lean file).
uv run pytest -m docker

# Modal backend: publish the same image to Modal, then run its parity suite.
uv run open-atp build-modal-image --name open-atp --app open-atp
uv run pytest -m modal   # needs MODAL_TOKEN_ID / MODAL_TOKEN_SECRET

# Pick a backend (or split: Modal for generation, Docker for the cheap verify).
uv run open-atp solve path/to/project --provers agent --backend modal
uv run open-atp solve path/to/project --provers agent \
    --agent-backend modal --backend docker
```

```python
from open_atp.lean import LeanProject
from open_atp.verify import docker_verifier

report = docker_verifier().verify(LeanProject("path/to/lake/project"))
print(report.verified, report.sorry_free, report.axioms)
```

## Layout

```
src/open_atp/
  api.py     Platform + prover registry (the dispatch/orchestration layer)
  __main__.py  `open-atp solve` / `build-image` / `build-modal-image` CLI
  core/      task.py result.py prover.py verifier.py
  backends/  base.py docker.py modal.py
  provers/   agent.py numina.py aristotle.py
images/      Dockerfile (Mathlib base)
vendor/numina/  vendored skills/prompts + VENDOR.md (MIT, tracked to upstream SHA)
```

## References (read-only symlinks under `refs/`)

- `milp_flare` — harness/runner/verification to port (the agentic + LSP MCP setup).
- `numina-lean-agent` — MIT; vendor its skills/prompts, re-implement the runner.
- `aristotle` — example client for `aristotle submit`.
