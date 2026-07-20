# DeepSeek

```{include} _meta_deepseek.md
:parser: myst
```

Use a [DeepSeek](https://www.deepseek.com/) model as an automated theorem prover. This prover is the {class}`~open_atp.provers.agent_prover.AgentProver` running the {doc}`/harnesses/opencode` harness pinned to DeepSeek's `deepseek-v4-pro` model. OpenCode is provider-agnostic, so any harness detail (skills, MCP tooling, the agent invocation, cost tracking) lives on the {doc}`/harnesses/opencode` page; this page covers only the model and how to run it.

## Authentication

DeepSeek bills against an API account. Sign up at the [DeepSeek platform](https://platform.deepseek.com/), fund it, and provide the key through the environment:

```bash
export DEEPSEEK_API_KEY=...
```

It is recommended to define this in a `.env` file in your project root. Alternatively, pass the key to the harness explicitly:

```{testcode}
from open_atp.harness import OpenCodeHarness

OpenCodeHarness(model="deepseek-v4-pro", provider_api_key="sk-...")
```

## Using the prover

### Standard prover via Python API

The simplest way to run the prover is through {func}`~open_atp.config.standard_prover`, which pins the `deepseek-v4-pro` model on the OpenCode harness. Set `DEEPSEEK_API_KEY` in the host environment (or pass it to the harness explicitly). Here, we prove the {ref}`MUL_REORDER` example theorem:

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

To override knobs like `model` and `effort`, construct the class directly:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.examples import EXAMPLE, example_task
from open_atp.harness import OpenCodeHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers import AgentProver

task = example_task(EXAMPLE.MUL_REORDER)
prover = AgentProver(
    harness=OpenCodeHarness(model="deepseek-reasoner", effort="medium"),
    backend=DockerBackend(image=DEFAULT_IMAGE),
)
result = prover.prove(task, output_dir=Path("demo"))
```

The OpenCode harness fronts more than DeepSeek: passing an Anthropic, OpenAI, or Google model switches provider automatically. See the {doc}`/harnesses/opencode` page for the full provider matrix and cost tracking.

:::{tip}
If you are harness agnostic and want to use Anthropic or OpenAI models, it is recommended to use the {doc}`/provers/claude_code` or {doc}`/provers/codex` provers. These provers are billed against subscription plans rather than API usage, which is often much cheaper.
:::
