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
   :exclude-members: name

.. autoclass:: open_atp.provers.base.ProofResult
   :exclude-members: prover, verification, output_dir, completed_files, cost_usd, duration_s, metadata, error, wd, logs_dir, success
```

## Provers

The concrete candidate generators.

### AgentProver

```{eval-rst}
.. autoclass:: open_atp.provers.agent_prover.AgentProver
   :show-inheritance:
   :exclude-members: name, harness, skills, extra_env, env, timeout_s, prover_prompt
```

`AgentProver`'s `harness` is a {class}`~open_atp.harness.Harness` — pick the
CLI and its knobs by composing one (e.g.
`AgentProver(harness=VibeHarness(max_turns=8), backend=...)`). The per-harness classes
are documented under {doc}`harness`.

### NuminaProver

```{eval-rst}
.. autoclass:: open_atp.provers.numina.NuminaProver
   :show-inheritance:
   :exclude-members: name, skills, extra_env, env, timeout_s, max_rounds, max_consecutive_limits, helper_env_keys, guard_statements, on_statement_change, prover_prompt
```

### AristotleProver

```{eval-rst}
.. autoclass:: open_atp.provers.aristotle.AristotleProver
   :show-inheritance:
   :exclude-members: name, api_key_env, allow_agent_questions, max_connection_retries, max_resume_attempts, resume_backoff_seconds, env, timeout_s, prover_prompt
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

Names are the `agent:<cli>` agent provers (`"agent:claude"`, `"agent:codex"`,
`"agent:opencode"`, `"agent:vibe"`, `"agent:axprover"`) and the standalone provers
(`"numina"`, `"aristotle"`); {func}`~open_atp.config.standard_provers` lists them all.
Each builds its class's baked-in defaults — to customize any knob, use
{func}`~open_atp.config.build_prover` with a full config dict instead.

```{eval-rst}
.. autofunction:: open_atp.config.standard_prover

.. autofunction:: open_atp.config.standard_provers

.. autodata:: open_atp.config.STANDARD_PROVERS
```
