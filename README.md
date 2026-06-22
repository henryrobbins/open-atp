# open-afps

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

## Status: Docker spine + Aristotle + AgentProver done

`DockerBackend` + `Verifier` work end-to-end against the Mathlib image; the
AristotleProver and AgentProver (claude/codex/opencode + lean-lsp-mcp) are
implemented. The Modal backend and the NuminaProver are still stubs with `TODO`s
pointing at the milp_flare / numina source to port from.

```bash
# Build the Mathlib base image (pins Lean/Mathlib v4.28.0).
docker build -t open-afps:latest images/

# Run the phase-1 verifier test (compiles a trivial Mathematics-in-Lean file).
uv run pytest -m docker
```

```python
from open_afps.core.task import LeanProject
from open_afps.core.verifier import docker_verifier

report = docker_verifier().verify(LeanProject("path/to/lake/project"))
print(report.verified, report.sorry_free, report.axioms)
```

### Build order

1. ~~**Backend + verifier (the spine).**~~ ✅ Docker done: `DockerBackend` ported from
   milp_flare, `images/Dockerfile` builds Mathlib with a warm olean cache, `Verifier`
   compiles file-by-file and checks sorry/axioms. (Modal backend still to port.)
2. ~~**AristotleProver.**~~ ✅ Done: submits the lake project via `aristotlelib`
   (submit → wait → download), unpacks the result over the workdir, and funnels it
   through the shared Docker verifier. Needs `ARISTOTLE_API_KEY` for real runs; tests
   stub the remote call and verify a real proof locally.
3. ~~**AgentProver.**~~ ✅ Done: ports milp_flare's `harness/` (claude/opencode/codex
   + lean-lsp-mcp, `cost.py`) onto the backend with a generic "fill the sorrys" prompt.
   The agent edits the staged project in place; `prove` diffs the `.lean` files and the
   shared verifier does the final check. Needs `CLAUDE_CODE_OAUTH_TOKEN` (or a provider
   key) for real runs; the fast tests mock the launch and parse a captured stream.
4. **NuminaProver.** Vendor Numina's `skills/`+`prompts/` (see `vendor/numina/VENDOR.md`),
   extend `AgentProver` with the round-continuation loop + statement tracker.
5. **Common API surface** (`api.py`): files + chosen provers + configs → `ProofResult`s;
   concurrency/job tracking last.

## Layout

```
src/open_afps/
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
