# API Reference

The reference mirrors the package layout under `src/open_atp/`:

- {doc}`provers` — `open_atp.provers`: the `AutomatedProver` base, its `ProofResult` output, the concrete candidate generators, and the standard catalog ({func}`~open_atp.config.standard_prover`, {func}`~open_atp.config.standard_provers`) that builds each ready-to-run default.
- {doc}`harness` — `open_atp.harness`: the agent-CLI harnesses composed by `AgentProver`.
- {doc}`backends` — `open_atp.backends`: the `ComputeBackend` sandbox primitive (`docker` | `modal`).
- {doc}`lean` — `open_atp.lean`: the Lean input contract (`LeanProject`, `ProofTask`) and the `create_project` helper.
- {doc}`verify` — `open_atp.verify`: the `VerificationReport` and the shared `Verifier`.
- {doc}`auth` — `open_atp.auth`: the `AuthStatus` each prover reports for its credential — present, expiring, or missing.
- {doc}`benchmark` — `open_atp.benchmark`: run provers across named tasks and tabulate the results ({func}`~open_atp.benchmark.run_benchmark`), build tasks from a directory, and download the public datasets (PutnamBench, FATE).
- {doc}`examples` — `open_atp.examples`: bundled `sorry`'d example tasks ({func}`~open_atp.examples.example_task`, {class}`~open_atp.examples.EXAMPLE`).

```{toctree}
:hidden:

provers
harness
backends
lean
verify
auth
benchmark
examples
```
