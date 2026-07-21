# Leanstral

```{include} _meta_leanstral.md
:parser: myst
```

Leanstral {cite:p}`mistral2026leanstral` is a Mistral Labs model fine-tuned for Lean theorem proving using Mistral's [Vibe](https://docs.mistral.ai/mistral-vibe/) agent harness. The prover uses the {class}`~open_atp.provers.agent_prover.AgentProver` with the {class}`~open_atp.harness.vibe.VibeHarness`.

```{note}
The prover drives Vibe's builtin `lean` agent, which pins [Leanstral 1.5](https://docs.mistral.ai/models/model-cards/leanstral-1-5) (`labs-leanstral-1-5`). Reaching it requires Lab Model access enabled by a Mistral org admin. Vibe exposes no `--model` flag, so the model is fixed by the agent; the harness's `model` field is recorded in the run metadata but not passed to Vibe. Vibe gates the `lean` agent behind an opt-in — the interactive `/leanstall` command — which does nothing but add `"lean"` to `installed_agents`; the harness writes that config key directly, so no interactive install step is needed.
```

## Authentication

By default the harness reads the Mistral La Plateforme key from the host environment:

```bash
export MISTRAL_API_KEY=...
```

Check if the key is in your environment with:

```bash
open-atp auth-status leanstral
```

Alternatively, pass the key to the harness explicitly:

```{testcode}
from open_atp.harness import VibeHarness

VibeHarness(mistral_api_key="msk-...")
```

Either way the harness forwards it into the sandbox as `MISTRAL_API_KEY`, where the lean agent's provider reads it from the process env. See {ref}`tracking-cost-and-usage-leanstral` for details.

## Using the prover

### Standard prover via Python API

The simplest way to run the prover is through {func}`~open_atp.config.standard_prover` which uses a standard configuration. Here, we prove the {ref}`MUL_REORDER` example theorem:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover
from open_atp.examples import EXAMPLE, example_task

task = example_task(EXAMPLE.MUL_REORDER)
prover = standard_prover("leanstral", backend=DockerBackend())
result = prover.prove(task, output_dir=Path("demo"))
```

### Standard prover via CLI

The standard prover can also be run from the CLI:

```bash
open-atp prove path/to/task.lean output_dir leanstral
```

### Customizing the prover

To override knobs like `max_turns` and `max_price`, construct the class directly.

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.examples import EXAMPLE, example_task
from open_atp.harness import VibeHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers import AgentProver

task = example_task(EXAMPLE.MUL_REORDER)
prover = AgentProver(
    harness=VibeHarness(
        max_turns=None,                  # passed to `vibe -p --max-turns`
        max_price=None,                  # passed to `vibe -p --max-price`
    ),
    backend=DockerBackend(image=DEFAULT_IMAGE),
)
result = prover.prove(task, output_dir=Path("demo"))
```

See the {doc}`/api/index` for all {class}`~open_atp.harness.vibe.VibeHarness` configuration options.

## Harness details

By default, the Leanstral harness is equipped with:

- Official Lean skills {cite:p}`leanprover_skills`.
- `lean-lsp-mcp` MCP server {cite:p}`lean_lsp_mcp`.

The agent prompt (below) is written into the working directory and read into `$PROMPT`. The Vibe CLI is then invoked in non-interactive mode with `$PROMPT` as the input. See the script below for the full Vibe CLI invocation.

:::{dropdown} Agent Prompt
:icon: book
```{literalinclude} ../../src/open_atp/provers/agent_prover.py
:language: text
:start-after: PROVER_PROMPT = """
:end-before: END PROVER_PROMPT
```
:::

:::{dropdown} `src/open_atp/harness/assets/scripts/vibe_agent.sh`
:icon: code
```{literalinclude} ../../src/open_atp/harness/assets/scripts/vibe_agent.sh
:language: bash
```
:::

(tracking-cost-and-usage-leanstral)=
## Tracking cost and usage

The Vibe harness provides per-session cost and token usage in its `meta.json` file. This is used to populate `cost_usd` in {class}`~open_atp.provers.base.ProofResult`. You can also monitor consumption from your Mistral La Plateforme dashboard.
