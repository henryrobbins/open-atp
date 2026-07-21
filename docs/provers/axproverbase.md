# AxProverBase

```{include} _meta_axproverbase.md
:parser: myst
```

AxProverBase {cite:p}`requena2026a` is a self-contained LangGraph Lean agent with its own proposer → builder → reviewer → memory loop. This prover uses the {class}`~open_atp.provers.agent_prover.AgentProver` with the {class}`~open_atp.harness.axproverbase.AxProverBaseHarness`.

## Authentication

Billing is directly against an API provider. By default the harness reads the provider's key from the host environment:

```bash
export ANTHROPIC_API_KEY=...
```

Check if the key is in your environment with:

```bash
open-atp auth-status axproverbase
```

Alternatively, pass the key to the harness explicitly:

```{testcode}
from open_atp.harness import AxProverBaseHarness

AxProverBaseHarness(provider_api_key="sk-...")
```

The provider is inferred from the model prefix, and the harness forwards the key into the sandbox under its canonical env var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GOOGLE_API_KEY`). See {ref}`tracking-cost-and-usage-axproverbase` for details.

## Using the prover

### Standard prover via Python API

The simplest way to run the prover is through {func}`~open_atp.config.standard_prover` which uses a standard configuration pointing at the `claude-opus-4-8` model. Either set `ANTHROPIC_API_KEY` in the host environment or pass it explicitly to the harness. Here, we prove the {ref}`MUL_REORDER` example theorem:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover
from open_atp.examples import EXAMPLE, example_task

task = example_task(EXAMPLE.MUL_REORDER)
prover = standard_prover("axproverbase", backend=DockerBackend())
result = prover.prove(task, output_dir=Path("demo"))
```

### Standard prover via CLI

The standard prover can also be run from the CLI:

```bash
open-atp prove path/to/task.lean output_dir axproverbase
```

### Customizing the prover

To override knobs like `model`, `effort`, and `max_iterations`, construct the class directly.

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.examples import EXAMPLE, example_task
from open_atp.harness import AxProverBaseHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers import AgentProver

task = example_task(EXAMPLE.MUL_REORDER)
prover = AgentProver(
    harness=AxProverBaseHarness(
        model="claude-opus-4-8",
        effort="high",
        max_iterations=None,  # None keeps ax-prover's own default of 50; set an int to cap
    ),
    backend=DockerBackend(image=DEFAULT_IMAGE),
)
result = prover.prove(task, output_dir=Path("demo"))
```

See the {doc}`/api/index` for all {class}`~open_atp.harness.axproverbase.AxProverBaseHarness` configuration options.

## Harness details

AxProverBase has its own Lean tooling. It doesn't use any of the skills or the Lean LSP MCP server {cite:p}`leanprover_skills` used by the other agent harnesses. See the script below for the full `ax-prover` CLI invocation.

:::{dropdown} `src/open_atp/harness/assets/scripts/axprover_agent.sh`
:icon: code
```{literalinclude} ../../src/open_atp/harness/assets/scripts/axprover_agent.sh
:language: bash
```
:::

(tracking-cost-and-usage-axproverbase)=
## Tracking cost and usage

The `ax-prover` CLI does not report cost by default. We manually log input / output tokens to `ax_output.<target>.json` files, which are summed and combined with pricing estimates in {data}`~open_atp.harness.cost.COST_PER_MTOK` to populate `cost_usd` in {class}`~open_atp.provers.base.ProofResult`. Those files carry no cache breakdown, so the whole input is priced at the uncached rate and `cost_usd` is an upper bound. You can also monitor consumption from your provider's usage dashboard. For example, Anthropic's dashboard is at [Anthropic Usage](https://console.anthropic.com/usage).
