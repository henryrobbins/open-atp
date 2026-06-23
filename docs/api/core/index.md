# `core`

The `open_afps.core` package holds the input/output contracts and the two reusable
primitives every prover builds on:

- {doc}`task` — the input contract (`LeanProject`, `ProofTask`).
- {doc}`result` — the output types (`VerificationReport`, `GenerationOutput`, `ProofResult`).
- {doc}`prover` — the `AutomatedProver` base (the shared generate-then-verify lifecycle).
- {doc}`verifier` — the shared `Verifier` (the final compile / sorry / axiom check).

```{toctree}
:maxdepth: 2

task
result
prover
verifier
```
