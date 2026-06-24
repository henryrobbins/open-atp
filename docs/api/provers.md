---
tocdepth: 3
---

# `provers`

The concrete provers and the registry/factory over them. Each prover subclasses
{class}`~open_atp.provers.base.AutomatedProver` and funnels its output through the
shared {class}`~open_atp.verify.Verifier`. The agentic provers compose an
{doc}`agent harness <harness>` (the *agent* concern) with a
{class}`~open_atp.backends.base.ComputeBackend` (the *compute* concern).

## Registry

The `open_atp.provers` registry maps a prover name to a constructed
{class}`~open_atp.provers.base.AutomatedProver`, wired to a shared sandbox
image/toolchain and compute backend. A caller then drives the prover directly
via {meth}`~open_atp.provers.base.AutomatedProver.prove`, which returns a
{class}`~open_atp.verify.ProofResult` with verification and cost. This is the
top-level surface re-exported from `open_atp` itself.

{class}`~open_atp.provers.PROVERS` enumerates the available provers (e.g.
`PROVERS.CLAUDE`, `PROVERS.CODEX`, `PROVERS.ARISTOTLE`).
{func}`~open_atp.provers.get_prover` maps a member (or its string value) to a constructed
{class}`~open_atp.provers.base.AutomatedProver`, wiring in the shared image/toolchain,
the verify backend, and (for agentic provers) the agent backend.

```{eval-rst}
.. autoclass:: open_atp.provers.PROVERS
   :members:

.. autofunction:: open_atp.provers.get_prover

.. autofunction:: open_atp.provers.available_provers
```

## Staging input

A full lake project on disk is just `LeanProject(Path(path))`.
{func}`~open_atp.lean.stage_files` stages one or more bare `.lean` files into the
pinned Mathlib skeleton (documented under {doc}`lean`).

## Base

The base prover abstraction. An {class}`~open_atp.provers.base.AutomatedProver` is a
candidate generator; the base class owns the shared lifecycle (the public `prove`:
generate, then verify in the sandbox) so subclasses only implement `_generate`.

```{eval-rst}
.. autoclass:: open_atp.provers.base.AutomatedProver
   :exclude-members: name

.. autoclass:: open_atp.provers.base.AutomatedProverConfig
   :no-members:
```

## AgentProver

```{eval-rst}
.. autoclass:: open_atp.provers.agent_prover.AgentProver
   :show-inheritance:
   :exclude-members: name

.. autoclass:: open_atp.provers.agent_prover.AgentProverConfig
   :show-inheritance:
   :no-members:
```

## NuminaProver

```{eval-rst}
.. autoclass:: open_atp.provers.numina.NuminaProver
   :show-inheritance:
   :exclude-members: name

.. autoclass:: open_atp.provers.numina.NuminaProverConfig
   :show-inheritance:
   :no-members:
```

## AristotleProver

```{eval-rst}
.. autoclass:: open_atp.provers.aristotle.AristotleProver
   :show-inheritance:
   :exclude-members: name

.. autoclass:: open_atp.provers.aristotle.AristotleProverConfig
   :show-inheritance:
   :no-members:
```
