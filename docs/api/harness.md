---
tocdepth: 3
---

# `harness`

The `open_afps.harness` package is the *agent* concern composed by the
{class}`~open_afps.provers.agent_prover.AgentProver`: for one agent CLI it owns the
launch script, credential forwarding, asset staging, and token/cost parsing. The
*compute* concern (where the command runs, with Lean+Mathlib) lives in the injected
{class}`~open_afps.backends.base.ComputeBackend`.

See the per-harness prover pages under {doc}`../provers/index` for credential setup.

## Base

```{eval-rst}
.. autoclass:: open_afps.harness.base.Harness
   :exclude-members: name

.. autoclass:: open_afps.harness.base.HarnessRunResult
   :no-members:

.. autoclass:: open_afps.harness.base.AuthSpec
   :no-members:
```

## Harnesses

Each harness adapts one agent CLI. They are registered in
{data}`~open_afps.harness.HARNESSES`, keyed by `name` and selected through
{class}`~open_afps.provers.agent_prover.AgentProverConfig`'s `harness` field.

```{eval-rst}
.. autoclass:: open_afps.harness.claude_code.ClaudeCodeHarness
   :show-inheritance:
   :exclude-members: configure_wd, name

.. autoclass:: open_afps.harness.codex.CodexHarness
   :show-inheritance:
   :exclude-members: configure_wd, name

.. autoclass:: open_afps.harness.opencode.OpenCodeHarness
   :show-inheritance:
   :exclude-members: configure_wd, name

.. autoclass:: open_afps.harness.vibe.VibeHarness
   :show-inheritance:
   :exclude-members: configure_wd, name

.. autoclass:: open_afps.harness.axprover.AxProverHarness
   :show-inheritance:
   :exclude-members: configure_wd, name

.. autodata:: open_afps.harness.HARNESSES
```

## Asset bundles

An {class}`~open_afps.harness.bundles.AssetBundle` is the selectable set of skills,
default prompt, and extra directories mounted into the agent workdir (selected via
`AgentProverConfig.assets`).

```{eval-rst}
.. autoclass:: open_afps.harness.bundles.AssetBundle
   :no-members:

.. autofunction:: open_afps.harness.bundles.resolve_bundle

.. autodata:: open_afps.harness.bundles.BUNDLES

.. autodata:: open_afps.harness.bundles.DEFAULT_BUNDLE
```

## Pricing

```{eval-rst}
.. autofunction:: open_afps.harness.cost.compute_cost_usd

.. autodata:: open_afps.harness.cost.COST_PER_MTOK
```
