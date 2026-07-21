# Kimi Code

```{include} _meta_kimi.md
:parser: myst
```

Use Moonshot AI's [Kimi Code](https://moonshotai.github.io/kimi-code/) CLI as an automated theorem prover with Lean skills and MCP tooling. This prover uses the {class}`~open_atp.provers.agent_prover.AgentProver` with the {class}`~open_atp.harness.kimi.KimiHarness`.

## Authentication

Kimi Code is included with every paid Kimi Code plan. [Choose a plan](https://www.kimi.com/code) and sign up if you don't already have an account. [Install](https://www.kimi.com/code) the Kimi Code CLI and generate an ephemeral API token once on the host:

```bash
kimi login
```

This writes OAuth credentials to `~/.kimi-code`. By default the harness reads that file; pass it explicitly to override:

```{testcode}
from pathlib import Path

from open_atp.harness import KimiHarness

KimiHarness(home_dir=Path("~/.kimi-code").expanduser())
```

The harness mounts the credential into the sandbox at run time so Kimi Code can refresh its access token mid-session, billing against your Kimi Code subscription. See {ref}`tracking-cost-and-usage-kimi` for details.

## Using the prover

### Standard prover via Python API

The simplest way to run the prover is through {func}`~open_atp.config.standard_prover` which uses a standard configuration. Here, we prove the {ref}`MUL_REORDER` example theorem:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover
from open_atp.examples import EXAMPLE, example_task

task = example_task(EXAMPLE.MUL_REORDER)
prover = standard_prover("kimi", backend=DockerBackend())
result = prover.prove(task, output_dir=Path("demo"))
```

### Standard prover via CLI

The standard prover can also be run from the CLI:

```bash
open-atp prove path/to/task.lean output_dir kimi
```

### Customizing the prover

To override knobs like `model`, construct the class directly:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.examples import EXAMPLE, example_task
from open_atp.harness import KimiHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers import AgentProver

task = example_task(EXAMPLE.MUL_REORDER)
prover = AgentProver(
    harness=KimiHarness(model="kimi-code/kimi-for-coding"),
    backend=DockerBackend(image=DEFAULT_IMAGE),
)
result = prover.prove(task, output_dir=Path("demo"))
```

See the {doc}`/api/index` for all {class}`~open_atp.harness.kimi.KimiHarness` configuration options.

## Harness details

By default, the Kimi Code harness is equipped with:

- Official Lean skills {cite:p}`leanprover_skills`.
- `lean-lsp-mcp` MCP server {cite:p}`lean_lsp_mcp`.

The agent prompt (below) is written into the working directory and read into `$PROMPT`. The Kimi CLI is then invoked in non-interactive mode with `$PROMPT` as the input. See the script below for the full Kimi CLI invocation.

:::{dropdown} Agent Prompt
:icon: book
```{literalinclude} ../../src/open_atp/provers/agent_prover.py
:language: text
:start-after: PROVER_PROMPT = """
:end-before: END PROVER_PROMPT
```
:::

:::{dropdown} `src/open_atp/harness/assets/scripts/kimi_agent.sh`
:icon: code
```{literalinclude} ../../src/open_atp/harness/assets/scripts/kimi_agent.sh
:language: bash
```
:::

(tracking-cost-and-usage-kimi)=
## Tracking cost and usage

The Kimi Code CLI does not report per-run USD, and its `stream-json` output carries only messages and tool calls, not token totals. Token totals are read from Kimi's per-session `wire.jsonl` (`usage.record` events) synced back from the workdir-local `KIMI_CODE_HOME`, and the pricing table in {data}`~open_atp.harness.cost.COST_PER_MTOK` is used to compute the cost in USD. This populates `cost_usd` in {class}`~open_atp.provers.base.ProofResult`. The wire log breaks input down into cache reads, cache writes, and uncached tokens, so cache reads are priced at Kimi's discounted rate rather than the cache-miss rate. Usage within your plan's quota is not billed. You can monitor plan consumption at [Kimi Console](https://www.kimi.com/code/console).

