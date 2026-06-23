# Running a prover

A prover takes a {class}`~open_afps.core.task.ProofTask` (a lake project plus
optional instructions and target files) and returns a
{class}`~open_afps.core.result.ProofResult` (the completed files, a
{class}`~open_afps.core.result.VerificationReport`, cost, and duration). Every prover
shares the same lifecycle: **generate candidate files, then verify them in a sandbox**
via the shared {class}`~open_afps.core.verifier.Verifier`.

## Prerequisites

- Docker running and the `open-afps:latest` image built (see {doc}`docker`).
- A credential for the prover you choose:
  - **AristotleProver** — `ARISTOTLE_API_KEY`.
  - **AgentProver / NuminaProver** — a harness credential (see
    {doc}`../agent_harness/index`).

## Verifying without a prover

If your project already contains candidate proofs, you can skip generation and run
the shared verifier directly:

```python
from open_afps.core.task import LeanProject
from open_afps.core.verifier import docker_verifier

report = docker_verifier().verify(LeanProject("path/to/lake/project"))
print("verified:", report.verified)
print("sorry_free:", report.sorry_free)
print("axioms:", report.axioms)
```

## Filling sorrys with the AgentProver

The {class}`~open_afps.provers.agent_prover.AgentProver` runs a coding agent
(Claude Code, Codex, or OpenCode) with the [lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp)
server inside the sandbox, then diffs the `.lean` files it changed:

```python
from pathlib import Path

from open_afps.backends.docker import DockerBackend, DockerConfig
from open_afps.core.task import LeanProject, ProofTask
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_afps.provers import AgentProver, AgentProverConfig

backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
config = AgentProverConfig(
    image=DEFAULT_IMAGE,
    supported_toolchain=DEFAULT_TOOLCHAIN,
    harness="claude_code",
    model="claude-opus-4-8",
    effort="high",
)
prover = AgentProver(config, verification_backend=backend)

task = ProofTask(project=LeanProject("path/to/lake/project"))
result = prover.run(task, workdir=Path("runs/demo"))

print("success:", result.success)
print("cost_usd:", result.cost_usd)
print("duration_s:", result.duration_s)
```

Swap `harness` for `"codex"` or `"opencode"` (and the matching `model`) to use a
different agent CLI — see {doc}`../agent_harness/index`.

## Filling sorrys with Aristotle

The {class}`~open_afps.provers.aristotle.AristotleProver` hands the whole lake
project to Harmonic's hosted Aristotle agent (submit → wait → download), unpacks the
result over the workdir, and runs the same shared verifier locally:

```python
from pathlib import Path

from open_afps.backends.docker import DockerBackend, DockerConfig
from open_afps.core.task import LeanProject, ProofTask
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_afps.provers.aristotle import AristotleProver, AristotleProverConfig

backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
config = AristotleProverConfig(
    image=DEFAULT_IMAGE, supported_toolchain=DEFAULT_TOOLCHAIN
)
prover = AristotleProver(config, verification_backend=backend)

task = ProofTask(project=LeanProject("path/to/lake/project"))
result = prover.run(task, workdir=Path("runs/aristotle_demo"))
```

## Inspecting the result

A {class}`~open_afps.core.result.ProofResult` records everything a run produced:

```python
result.prover            # "agent" | "aristotle" | "numina"
result.success           # compiles, sorry-free, no foreign axioms
result.completed_files   # {relative path -> new file contents}
result.verification      # VerificationReport (per-file compile, axioms, log)
result.cost_usd          # estimated USD, when the prover reports it
result.duration_s        # wall-clock seconds
```

The {class}`~open_afps.core.result.VerificationReport` exposes the individual
sub-checks behind `success`: whether the project `compiles`, whether it is
`sorry_free`, and which `axioms` the proofs depend on (anything outside
{data}`~open_afps.core.result.STANDARD_AXIOMS` means the proof is not actually
complete).
