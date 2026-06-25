# API Reference

The reference mirrors the package layout under `src/open_atp/`:

- {doc}`provers` — `open_atp.provers`: the `AutomatedProver` base, its `ProofResult` output, the concrete candidate generators, and the standard catalog ({func}`~open_atp.config.standard_prover`, {func}`~open_atp.config.standard_provers`) that builds each ready-to-run default.
- {doc}`harness` — `open_atp.harness`: the agent-CLI harnesses composed by `AgentProver`.
- {doc}`backends` — `open_atp.backends`: the `ComputeBackend` sandbox primitive (`docker` | `modal`).
- {doc}`lean` — `open_atp.lean`: the Lean input contract (`LeanProject`, `ProofTask`) and the `stage_files` helper.
- {doc}`verify` — `open_atp.verify`: the `VerificationReport` and the shared `Verifier`.

```{toctree}
:maxdepth: 2

provers
harness
backends
lean
verify
```
