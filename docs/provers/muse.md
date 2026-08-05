# Muse Code

```{include} _meta_muse.md
:parser: myst
```

Use Meta's [Muse Code](https://dev.meta.ai/docs/muse-code) as an automated theorem prover with common skills and MCP tooling for working with Lean. Muse Code is Meta's terminal coding agent, powered by the Muse Spark models. OpenATP supports it through the {class}`~open_atp.provers.agent_prover.AgentProver` and the {class}`~open_atp.harness.muse.MuseHarness`.

:::{warning}
The standard prover defaults to `muse-spark-1.2-contributor`, the **contributor tier**. It is roughly an order of magnitude cheaper than the standard tier, but the discount is granted [in exchange for permission](https://dev.meta.ai/docs/pricing-rate-limits.md) for Meta to train future models on the run's prompts and completions — which for this prover means your Lean statements and the proofs the agent writes. If that is not acceptable for your work, pin the standard tier instead:

```{testcode}
from open_atp.harness import MuseHarness

MuseHarness(model="muse-spark-1.2")
```
:::

## Authentication

Muse Code bills against a Meta Model API account. Create a key in the [Meta developer console](https://dev.meta.ai/), then set it in the environment:

```bash
export META_MODEL_API_KEY=...
```

Check if the key is in your environment with:

```bash
open-atp auth-status muse
```

Alternatively, pass the key to the harness explicitly:

```{testcode}
from open_atp.harness import MuseHarness

MuseHarness(api_key="...")
```

The harness reads `META_MODEL_API_KEY` on the host — the name shared with the other Meta-billed provers — and forwards it into the sandbox as `META_API_KEY`, which is what the `muse` CLI itself reads. Muse Code's browser sign-in (`muse login`) is not used: an API key takes priority over it, and only the key can be forwarded into a sandbox.

## Using the prover

### Standard prover via Python API

The simplest way to run the prover is through {func}`~open_atp.config.standard_prover`. Set `META_MODEL_API_KEY` in the host environment (or pass it to the harness explicitly). Here, we prove the {ref}`MUL_REORDER` example theorem:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover
from open_atp.examples import EXAMPLE, example_task

task = example_task(EXAMPLE.MUL_REORDER)
prover = standard_prover("muse", backend=DockerBackend())
result = prover.prove(task, output_dir=Path("demo"))
```

### Standard prover via CLI

The standard prover can also be run from the CLI:

```bash
open-atp prove path/to/task.lean output_dir muse
```

### Customizing the prover

To override knobs like `model` or `effort`, construct the class directly. Muse Code accepts a reasoning effort of `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `ultra`:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.examples import EXAMPLE, example_task
from open_atp.harness import MuseHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers import AgentProver

task = example_task(EXAMPLE.MUL_REORDER)
prover = AgentProver(
    harness=MuseHarness(model="muse-spark-1.2", effort="xhigh"),
    backend=DockerBackend(image=DEFAULT_IMAGE),
)
result = prover.prove(task, output_dir=Path("demo"))
```

## Tracking cost and usage

Muse Code reports no cost and no token counts on its event stream, so the harness reads token totals from the session logs the CLI writes under the working directory, summing the main session together with every subagent it spawned. Cost is then estimated from those totals against the {data}`~open_atp.harness.cost.COST_PER_MTOK` table and reported as `cost_usd` on {class}`~open_atp.provers.base.ProofResult`. Because it is an estimate rather than a figure the CLI reported, treat the [Meta developer console](https://dev.meta.ai/) as the source of truth for billing.

The session logs themselves are moved out of the working directory into the run's `logs/muse-session/` directory, alongside the streamed `stdout.txt`.
