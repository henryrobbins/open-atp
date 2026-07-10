# Grok

```{include} _meta_grok.md
:parser: myst
```

Use xAI's [Grok CLI](https://docs.x.ai/build/overview) (Grok Build) as an automated theorem prover with Lean skills and MCP tooling. This prover uses the {class}`~open_atp.provers.agent_prover.AgentProver` with the {class}`~open_atp.harness.grok.GrokHarness`, driving the `grok` coding agent non-interactively against an xAI model (default `grok-4.5`).

## Authentication

Grok bills against an xAI API account. Create a key in the [xAI console](https://console.x.ai/) and expose it as `XAI_API_KEY`:

```bash
export XAI_API_KEY=xai-...
```

It is recommended to define this in a `.env` file in your project root. Alternatively, pass the key to the harness explicitly:

```{testcode}
from open_atp.harness import GrokHarness

GrokHarness(xai_api_key="xai-...")
```

Either way the harness forwards the key into the sandbox as `XAI_API_KEY`.

## Using the prover

### Standard prover via Python API

The simplest way to run the prover is through {func}`~open_atp.config.standard_prover`, which uses a standard configuration pointing at `grok-4.5`. Set `XAI_API_KEY` in the host environment (or pass it to the harness). Here, we prove the {ref}`MUL_REORDER` example theorem:

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

To override knobs like `model` and `effort`, construct the class directly. Set `model="grok-build-0.1"` to use xAI's code-specialized model instead of `grok-4.5`:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.examples import EXAMPLE, example_task
from open_atp.harness import GrokHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers import AgentProver

task = example_task(EXAMPLE.MUL_REORDER)
prover = AgentProver(
    harness=GrokHarness(model="grok-4.5", effort="medium"),
    backend=DockerBackend(image=DEFAULT_IMAGE),
)
result = prover.prove(task, output_dir=Path("demo"))
```

## Harness details

By default, the Grok harness is equipped with:

- Official Lean skills {cite:p}`leanprover_skills`.
- `lean-lsp-mcp` MCP server {cite:p}`lean_lsp_mcp`, wired in via a project-scope `.grok/config.toml`.

The agent prompt (below) is written into the working directory and read into `$PROMPT`. The Grok CLI is then invoked in non-interactive mode (`grok --single`) with `$PROMPT` as the input. See the script below for the full Grok CLI invocation.

:::{dropdown} Agent Prompt
:icon: book
```{literalinclude} ../../src/open_atp/provers/agent_prover.py
:language: text
:start-after: PROVER_PROMPT = """
:end-before: END PROVER_PROMPT
```
:::

:::{dropdown} `src/open_atp/harness/assets/scripts/grok_agent.sh`
:icon: code
```{literalinclude} ../../src/open_atp/harness/assets/scripts/grok_agent.sh
:language: bash
```
:::

(tracking-cost-and-usage-grok)=
## Tracking cost and usage

The Grok CLI does not self-report a USD cost, so the run cost is estimated from the token totals in its JSON output using the pricing table in {data}`~open_atp.harness.cost.COST_PER_MTOK` (see {func}`~open_atp.harness.cost.compute_cost_usd`). You can also monitor consumption from the [xAI console](https://console.x.ai/) usage dashboard.
