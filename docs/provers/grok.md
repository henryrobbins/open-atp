# Grok

```{include} _meta_grok.md
:parser: myst
```

Use xAI's Grok as an automated theorem prover with Lean skills and MCP tooling
(default model `grok-4.5`).

The **standard `grok` prover** ({func}`~open_atp.config.standard_prover`) drives Grok
through the {class}`~open_atp.harness.opencode.OpenCodeHarness`'s `xai` provider, **not**
the native Grok CLI. The Grok CLI drives tools over [ACP](https://docs.x.ai/build/cli/headless-scripting#acp)
with aggressive parallel dispatch: it fires several `lean-lsp` MCP calls at once at the
single-threaded language server, they serialize behind the first cold
`lean_diagnostic_messages`, and each trips the MCP tool timeout — burning the whole
generation budget. Grok exposes no knob to serialize tool calls, so the standard prover
routes Grok through opencode instead, which drives `lean-lsp` without that contention.

:::{warning}
**Regional availability.** Grok is a hosted xAI model and `grok-4.5` is geo-gated:
requests are refused with `403 permission-denied: The model grok-4.5 is not available in
your region` from some countries. This depends only on the country the request egresses
from, not your account. On the {class}`~open_atp.backends.modal.ModalBackend`, sandboxes
are scheduled wherever Modal has capacity, so **without a region pin some sandboxes
egress from outside the US and 403 while others succeed in the same sweep**. The backend
therefore defaults to `region="us"`. Override it via the benchmark `compute` config if
you need a different region:

```yaml
compute:
  type: modal
  region: us   # or "eu", "us-east", ["us-east", "us-west"], ...
```
:::

## Authentication

Because the prover runs on the {class}`~open_atp.harness.opencode.OpenCodeHarness`'s `xai` provider, it authenticates through opencode's credential store rather than an xAI API key. Log in once on the host with opencode's OAuth flow (see [opencode xAI docs](https://opencode.ai/docs/providers/#xai)):

```bash
opencode auth login
```

Select **xAI** and complete the login. This writes an `xai` entry into opencode's `auth.json` (`~/.local/share/opencode/auth.json`). The harness stages **only** that entry into the sandbox (never the whole file, which may hold other providers' keys) and points `XDG_DATA_HOME` at the mount, so opencode reads the credential there. Runs draw on the logged-in xAI plan.

## Using the prover

### Standard prover via Python API

The simplest way to run the prover is through {func}`~open_atp.config.standard_prover`, which uses a standard configuration pointing at `grok-4.5` on the `xai` provider. Log in with `opencode auth login` -> xAI on the host first. Here, we prove the {ref}`MUL_REORDER` example theorem:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover
from open_atp.examples import EXAMPLE, example_task

task = example_task(EXAMPLE.MUL_REORDER)
prover = standard_prover("grok", backend=DockerBackend())
result = prover.prove(task, output_dir=Path("demo"))
```

### Standard prover via CLI

The standard prover can also be run from the CLI:

```bash
open-atp prove path/to/task.lean output_dir grok
```

### Customizing the prover

To override knobs like `model` and `effort`, construct the harness directly. Pass `provider="xai"` so the model routes to xAI (it is also inferred from the `grok-` model prefix):

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.examples import EXAMPLE, example_task
from open_atp.harness import OpenCodeHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers import AgentProver

task = example_task(EXAMPLE.MUL_REORDER)
prover = AgentProver(
    harness=OpenCodeHarness(model="grok-4.5", provider="xai", effort="medium"),
    backend=DockerBackend(image=DEFAULT_IMAGE),
)
result = prover.prove(task, output_dir=Path("demo"))
```

## Harness details

By default, the Grok prover is equipped with:

- Official Lean skills {cite:p}`leanprover_skills`.
- `lean-lsp-mcp` MCP server {cite:p}`lean_lsp_mcp`.

It runs on the {class}`~open_atp.harness.opencode.OpenCodeHarness`, so the agent prompt and launch script are shared with the {doc}`/provers/opencode` prover — see that page for the full invocation.

## Tracking cost and usage

Runs on the xAI OAuth plan do not carry a per-token USD cost, so opencode reports no cost and the run cost is estimated from the token counts it does report, using the pricing table in {data}`~open_atp.harness.cost.COST_PER_MTOK` (see {func}`~open_atp.harness.cost.compute_cost_usd`). For authoritative spend, use the [xAI console](https://console.x.ai/) usage dashboard.
