---
tocdepth: 3
---

# `harness`

The `open_atp.harness` package is the *agent* concern composed by the
{class}`~open_atp.provers.agent_prover.AgentProver`: for one agent CLI it owns the
launch script, credential forwarding, asset staging, and token/cost parsing. The
*compute* concern (where the command runs, with Lean+Mathlib) lives in the injected
{class}`~open_atp.backends.base.ComputeBackend`.

See the per-harness prover pages under {doc}`../provers/index` for credential setup.

## Base

```{eval-rst}
.. autoclass:: open_atp.harness.base.Harness

.. autoclass:: open_atp.harness.base.HarnessRunResult
   :no-members:

.. autoclass:: open_atp.harness.base.AgentAuth
   :no-members:
```

## Harnesses

Each harness adapts one agent CLI and is a
{class}`~open_atp.harness.Harness` subclass (set as
{class}`~open_atp.provers.agent_prover.AgentProver`'s `harness`).

```{eval-rst}
.. autoclass:: open_atp.harness.claude_code.ClaudeCodeHarness
   :show-inheritance:
   :exclude-members: stage_wd

.. autoclass:: open_atp.harness.codex.CodexHarness
   :show-inheritance:
   :exclude-members: stage_wd

.. autoclass:: open_atp.harness.opencode.OpenCodeHarness
   :show-inheritance:
   :exclude-members: stage_wd

.. autoclass:: open_atp.harness.vibe.VibeHarness
   :show-inheritance:
   :exclude-members: stage_wd

.. autoclass:: open_atp.harness.axproverbase.AxProverBaseHarness
   :show-inheritance:
   :exclude-members: stage_wd
```

## Pricing

```{eval-rst}
.. autofunction:: open_atp.harness.cost.compute_cost_usd

.. autodata:: open_atp.harness.cost.COST_PER_MTOK
```
