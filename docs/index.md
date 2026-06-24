# OpenATP

**Open Automated Formal Proof Synthesis.** Upload one or more
[Lean](https://lean-lang.org/) files containing `sorry`, run them through leading
proof-synthesis backends, and get back verified completed proofs with metadata
(verification status, cost, duration).

The whole platform reduces to two reusable primitives plus thin candidate
generators:

1. A {class}`~open_atp.backends.base.ComputeBackend` (`docker` | `modal`) — run a
   command over a working directory in a Lean+Mathlib sandbox.
2. A {class}`~open_atp.verify.Verifier` — compile a candidate project in a
   backend and report whether it compiles, is `sorry`-free, and is axiom-clean.

Every prover funnels its output through the **shared verifier**, including the
remote Aristotle path:

```
ComputeBackend (docker | modal)         ← the sandbox primitive
        │
        ├── Verifier  ──────────────────← shared final check (ALL provers)
        │
AutomatedProver (base)
 ├── AgentProver      coding agent (claude/opencode/codex) + lean-lsp-mcp in sandbox
 ├── NuminaProver     configured AgentProver: claude + Numina assets + round loop
 └── AristotleProver  remote aristotle submit --wait, no sandbox to generate
```

See below for installation instructions, user guides, the prover catalogue, and the
API reference.

```{toctree}
:maxdepth: 2
:caption: Contents

installation
user_guide/index
provers/index
compute_backend/index
api/index
```
