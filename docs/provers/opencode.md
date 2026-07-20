# OpenCode

Use [OpenCode](https://opencode.ai/) as an automated theorem prover with common skills and MCP tooling for working with Lean. OpenCode is an open-source coding-agent harness with many supported [model providers](https://opencode.ai/docs/providers/). OpenATP supports OpenCode through the {class}`~open_atp.provers.agent_prover.AgentProver` and the {class}`~open_atp.harness.opencode.OpenCodeHarness`.

:::{tip}
We recommend {doc}`/provers/claude_code` and {doc}`/provers/codex` prover over using OpenCode with the `anthropic` or `openai` providers. These provers use their native agent harness and are billed against subscription plans.
:::

```{toctree}
:maxdepth: 1
:hidden:

deepseek
```

(opencode-authentication)=
## Authentication

Each OpenCode provider requires authentication. OpenATP supports two authentication strategies: API key and login. The harness's `auth` argument selects the strategy.

If your provider supports authentication via OAuth with **billing against a subscription plan**, use the OpenCode login strategy to avoid paying for API usage. Otherwise, use the API key strategy. 

### API key

By default, the harness will look for the provider's canonical API key in the host environment. E.g., the `deepseek` provider uses `DEEPSEEK_API_KEY`. You can also set the key explicitly using the `api_key` argument.

```{testcode}
from open_atp.harness import OpenCodeHarness

OpenCodeHarness(provider="deepseek", model="deepseek-v4-pro", api_key="sk-...")
```

### OpenCode Login

Alternatively, you can authenticate with OpenCode login. Run the following command command and select your provider from the dropdown.

```bash
opencode auth login
```

This generates the necessary authentication credentials on your machine. You can then use the harness with the `auth="login"` argument to forward the credentials into the agent sandbox.

```{testcode}
from open_atp.harness import OpenCodeHarness

OpenCodeHarness(provider="xai", model="grok-4.5", auth="login")
```

## Using the harness

The OpenCode harness can be used directly with {class}`~open_atp.provers.agent_prover.AgentProver` for automated theorem proving. Once you've selected a provider and an authentication strategy, just select a model and effort level. Note the model must be supported on the chosen provider. Here, we prove the {ref}`MUL_REORDER` example theorem:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.examples import EXAMPLE, example_task
from open_atp.harness import OpenCodeHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers import AgentProver

task = example_task(EXAMPLE.MUL_REORDER)
prover = AgentProver(
    harness=OpenCodeHarness(provider="deepseek", model="deepseek-v4-pro", effort="medium"),
    backend=DockerBackend(image=DEFAULT_IMAGE),
)
result = prover.prove(task, output_dir=Path("demo"))
```

## Harness details

By default, the OpenCode harness is equipped with:

- Official Lean skills {cite:p}`leanprover_skills`.
- `lean-lsp-mcp` MCP server {cite:p}`lean_lsp_mcp`.

The agent prompt (below) is written into the working directory and read into `$PROMPT`.
The OpenCode CLI is then invoked in non-interactive mode with `$PROMPT` as the input. See
the script below for the full OpenCode CLI invocation.

:::{dropdown} Agent Prompt
:icon: book
```{literalinclude} ../../src/open_atp/provers/agent_prover.py
:language: text
:start-after: PROVER_PROMPT = """
:end-before: END PROVER_PROMPT
```
:::

:::{dropdown} `src/open_atp/harness/assets/scripts/opencode_agent.sh`
:icon: code
```{literalinclude} ../../src/open_atp/harness/assets/scripts/opencode_agent.sh
:language: bash
```
:::

See the {doc}`/api/index` for all {class}`~open_atp.harness.opencode.OpenCodeHarness` configuration options.

(tracking-cost-and-usage-opencode)=
## Tracking cost and usage

The OpenCode CLI reports a per-step cost and token breakdown for each provider call.
When present, the cost is summed to populate `cost_usd` in
{class}`~open_atp.provers.base.ProofResult`; when a provider does not self-report USD
(e.g. an OAuth plan), the cost is estimated from the token counts using
{data}`~open_atp.harness.cost.COST_PER_MTOK` (see
{func}`~open_atp.harness.cost.compute_cost_usd`). You can also monitor consumption from
your provider's usage dashboard.
