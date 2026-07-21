# Codex

```{include} _meta_codex.md
:parser: myst
```

Use OpenAI's [Codex](https://chatgpt.com/codex) CLI as an automated theorem prover with Lean skills and MCP tooling. This prover uses the {class}`~open_atp.provers.agent_prover.AgentProver` with the {class}`~open_atp.harness.codex.CodexHarness`.

## Authentication

Codex is available on paid ChatGPT plans. [Choose a plan](https://chatgpt.com/pricing/) and sign up if you don't have an account. [Install](https://developers.openai.com/codex/cli) the Codex CLI and generate an ephemeral API token once on the host:

```bash
codex login
```

This writes credentials to `~/.codex/auth.json`. By default the harness reads that file; pass it explicitly to override:

```{testcode}
from pathlib import Path

from open_atp.harness import CodexHarness

CodexHarness(auth_file=Path("~/.codex/auth.json").expanduser())
```

The harness mounts the credential into the sandbox at run time so Codex can refresh its access token mid-session, billing against your ChatGPT subscription. See {ref}`tracking-cost-and-usage-codex` for details.

## Using the prover

### Standard prover via Python API

The simplest way to run the prover is through {func}`~open_atp.config.standard_prover` which uses a standard configuration. Here, we prove the {ref}`MUL_REORDER` example theorem:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover
from open_atp.examples import EXAMPLE, example_task

task = example_task(EXAMPLE.MUL_REORDER)
prover = standard_prover("codex", backend=DockerBackend())
result = prover.prove(task, output_dir=Path("demo"))
```

### Standard prover via CLI

The standard prover can also be run from the CLI:

```bash
open-atp prove path/to/task.lean output_dir codex
```

### Customizing the prover

To override knobs like `model` and `effort`, construct the class directly:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.examples import EXAMPLE, example_task
from open_atp.harness import CodexHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers import AgentProver

task = example_task(EXAMPLE.MUL_REORDER)
prover = AgentProver(
    harness=CodexHarness(effort="high"),
    backend=DockerBackend(image=DEFAULT_IMAGE),
)
result = prover.prove(task, output_dir=Path("demo"))
```

See the {doc}`/api/index` for all {class}`~open_atp.harness.codex.CodexHarness` configuration options.

:::{warning}
Codex does not support all of the models available in the OpenAI API.
:::

## Harness details

By default, the Codex harness is equipped with:

- Official Lean skills {cite:p}`leanprover_skills`.
- `lean-lsp-mcp` MCP server {cite:p}`lean_lsp_mcp`.

The agent prompt (below) is written into the working directory and read into `$PROMPT`. The Codex CLI is then invoked in non-interactive mode with `$PROMPT` as the input. See the script below for the full Codex CLI invocation.

:::{dropdown} Agent Prompt
:icon: book
```{literalinclude} ../../src/open_atp/provers/agent_prover.py
:language: text
:start-after: PROVER_PROMPT = """
:end-before: END PROVER_PROMPT
```
:::

:::{dropdown} `src/open_atp/harness/assets/scripts/codex_agent.sh`
:icon: code
```{literalinclude} ../../src/open_atp/harness/assets/scripts/codex_agent.sh
:language: bash
```
:::

(tracking-cost-and-usage-codex)=
## Tracking cost and usage

The Codex CLI does not report per-run USD. Token totals from the `turn.completed` events are summed and the pricing table in {data}`~open_atp.harness.cost.COST_PER_MTOK` is used to compute the cost in USD. This populates `cost_usd` in {class}`~open_atp.provers.base.ProofResult`. Usage within your plan's quota is not billed. You can monitor plan consumption at [Analytics](https://chatgpt.com/codex/cloud/settings/analytics).

:::{warning}
Running a large number of proofs can quickly consume your plan's 5 hour session quota.
:::
