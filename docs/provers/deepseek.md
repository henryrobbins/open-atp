# DeepSeek

```{include} _meta_deepseek.md
:parser: myst
```

Use [DeepSeek](https://www.deepseek.com/) as an automated theorem prover with Lean skills
and MCP tooling (default model `deepseek-v4-pro`).

The **standard `deepseek` prover** ({func}`~open_atp.config.standard_prover`) drives
DeepSeek through the {class}`~open_atp.harness.opencode.OpenCodeHarness`'s `deepseek`
provider under `auth="api_key"` — see the {doc}`/provers/opencode` harness page for the
authentication strategies, launch script, and cost tracking shared by all
OpenCode-backed provers.

## Authentication

DeepSeek bills against an API account. Fund an account, then set the key in the host
environment (a `.env` file at the project root is read automatically):

```bash
export DEEPSEEK_API_KEY=...
```

Alternatively pass it to the harness explicitly via `provider_api_key`. The harness
forwards the key into the sandbox under `DEEPSEEK_API_KEY`.

## Using the prover

### Standard prover via Python API

The simplest way to run the prover is through {func}`~open_atp.config.standard_prover`,
which uses a standard configuration pointing at `deepseek-v4-pro`. Set `DEEPSEEK_API_KEY`
in the host environment first. Here, we prove the {ref}`MUL_REORDER` example theorem:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover
from open_atp.examples import EXAMPLE, example_task

task = example_task(EXAMPLE.MUL_REORDER)
prover = standard_prover("deepseek", backend=DockerBackend())
result = prover.prove(task, output_dir=Path("demo"))
```

### Standard prover via CLI

The standard prover can also be run from the CLI:

```bash
open-atp prove path/to/task.lean output_dir deepseek
```

### Customizing the prover

To override knobs like `model` and `effort`, construct the harness directly:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.examples import EXAMPLE, example_task
from open_atp.harness import OpenCodeHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers import AgentProver

task = example_task(EXAMPLE.MUL_REORDER)
prover = AgentProver(
    harness=OpenCodeHarness(model="deepseek-v4-flash", effort="medium"),
    backend=DockerBackend(image=DEFAULT_IMAGE),
)
result = prover.prove(task, output_dir=Path("demo"))
```

## Harness details

It runs on the {class}`~open_atp.harness.opencode.OpenCodeHarness` — see the
{doc}`/provers/opencode` page for the shared agent prompt, launch script, and the
`lean-lsp-mcp` / Lean-skills configuration.

## Tracking cost and usage

The OpenCode CLI reports a per-step cost for each DeepSeek call, summed into `cost_usd`
on {class}`~open_atp.provers.base.ProofResult`. You can also monitor spend from the
[DeepSeek usage dashboard](https://platform.deepseek.com/usage).
