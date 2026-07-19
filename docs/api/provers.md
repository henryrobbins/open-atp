---
tocdepth: 3
---

# `provers`

The concrete provers and the registry/factory over them. Each prover subclasses
{class}`~open_atp.provers.base.AutomatedProver` and funnels its output through the
shared {class}`~open_atp.verify.Verifier`. The agentic provers compose an
{doc}`agent harness <harness>` (the *agent* concern) with a
{class}`~open_atp.backends.base.ComputeBackend` (the *compute* concern).

## Base

The base prover abstraction. An {class}`~open_atp.provers.base.AutomatedProver` is a
candidate generator; the base class owns the shared lifecycle (the public `prove`:
generate, then verify in the sandbox) so subclasses only implement `_generate`.

```{eval-rst}
.. autoclass:: open_atp.provers.base.AutomatedProver

.. autoclass:: open_atp.provers.base.ProofResult

.. autoclass:: open_atp.provers.base.ProofStatus
   :members:
```

## Provers

The concrete candidate generators.

```{eval-rst}
.. autoclass:: open_atp.provers.agent_prover.AgentProver
   :show-inheritance:
```

```{eval-rst}
.. autoclass:: open_atp.provers.numina.NuminaProver
   :show-inheritance:
```

```{eval-rst}
.. autoclass:: open_atp.provers.aristotle.AristotleProver
   :show-inheritance:
```

## Standard catalog

The standard catalog names each ready-to-run *default* prover and builds it against a
compute backend. {func}`~open_atp.config.standard_prover` maps a catalog name to a
constructed {class}`~open_atp.provers.base.AutomatedProver`, wiring in the shared
image/toolchain from the backend; a caller then drives it directly via
{meth}`~open_atp.provers.base.AutomatedProver.prove`, which returns a
{class}`~open_atp.provers.base.ProofResult` with verification and cost. Agentic provers
run generation in a live session over that backend and verify in the same hot sandbox.
This is the top-level surface re-exported from `open_atp` itself.

Names are the agentic provers (`"claude"`, `"codex"`,
`"opencode"`, `"leanstral"`, `"axproverbase"`) and the standalone provers
(`"numina"`, `"aristotle"`); {func}`~open_atp.config.standard_provers` lists them all.
Each builds its class's baked-in defaults — to customize any knob, use
{func}`~open_atp.config.build_prover` with a full config dict instead.

```{eval-rst}
.. autofunction:: open_atp.config.standard_prover

.. autofunction:: open_atp.config.standard_provers

.. autodata:: open_atp.config.STANDARD_PROVERS
```
