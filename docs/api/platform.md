# `api`

The `open_afps.api` module is the dispatch/orchestration layer: it accepts a lake
project (or bare `.lean` files) plus a list of prover names, fans the job out across
those provers concurrently, and returns compared per-prover
{class}`~open_afps.core.result.ProofResult`\s with verification and cost.

This is the top-level surface re-exported from `open_afps` itself.

## Factory

{func}`~open_afps.api.build_prover` maps a registry name (e.g. `agent`,
`agent:codex`, `aristotle`) to a constructed {class}`~open_afps.core.prover.AutomatedProver`,
wiring in the shared image/toolchain, the verify backend, and (for agentic provers)
the agent backend.

```{eval-rst}
.. autofunction:: open_afps.api.build_prover

.. autofunction:: open_afps.api.available_provers
```

## Platform

{class}`~open_afps.api.Platform` holds the shared sandbox image/toolchain and the
(verify, agent) compute backends. Construct it once, then call
{meth}`~open_afps.api.Platform.solve` per job.

```{eval-rst}
.. autoclass:: open_afps.api.Platform
   :members: build, solve
```

## SolveResult

```{eval-rst}
.. autoclass:: open_afps.api.SolveResult
```
