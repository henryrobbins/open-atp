# OpenCode harness

[OpenCode](https://opencode.ai/) is a provider-agnostic coding agent: one CLI fronts
any of its [supported providers](https://opencode.ai/docs/providers/). open-atp drives
it through the {class}`~open_atp.harness.opencode.OpenCodeHarness` (paired with the
{class}`~open_atp.provers.agent_prover.AgentProver`), so a single harness backs several
standard provers on different providers — {doc}`/provers/deepseek` (DeepSeek) and
{doc}`/provers/grok` (xAI Grok) today.

This page documents the harness the OpenCode-backed provers share: the two
authentication strategies, the launch script, and cost tracking. For a ready-to-run
prover, see the {doc}`/provers/deepseek` and {doc}`/provers/grok` pages.

(opencode-authentication)=
## Authentication

OpenCode resolves a provider's credentials from two channels, selected by the harness's
`auth` argument:

`auth="api_key"` (default)
: Forward the provider's API key as its canonical env var. The harness reads the key
  from the host environment (or an explicit `api_key`) and forwards it under
  the provider's standard name (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`,
  `XAI_API_KEY`, …; any other provider falls back to `<PROVIDER>_API_KEY`). Best for
  providers billed against an API account:

  ```bash
  export DEEPSEEK_API_KEY=...
  ```

  ```{testcode}
  from open_atp.harness import OpenCodeHarness

  OpenCodeHarness(provider="deepseek", model="deepseek-v4-pro", api_key="sk-...")
  ```

`auth="login"`
: Authenticate from OpenCode's own credential store, so an OAuth login (e.g. a
  subscription plan) or an `opencode auth login` API key works without exporting a key.
  Log in once on the host:

  ```bash
  opencode auth login
  ```

  This writes the provider's entry into `~/.local/share/opencode/auth.json`. The harness
  stages **only** the selected provider's entry into the sandbox (never the whole file,
  which may hold other providers' credentials) and points `XDG_DATA_HOME` at the mount,
  so OpenCode reads the credential there. See {doc}`/provers/grok` for a worked example.

The `provider` argument is required and names the OpenCode provider (`anthropic`, `xai`,
`deepseek`, …); any OpenCode provider is accepted.

## Customizing the harness

To override knobs like `model`, `effort`, `provider`, or `auth`, construct the harness
directly and wrap it in an {class}`~open_atp.provers.agent_prover.AgentProver`:

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

:::{tip}
If you are harness agnostic and want to use Anthropic or OpenAI models, prefer the
{doc}`/provers/claude_code` or {doc}`/provers/codex` provers. These are billed against
subscription plans rather than API usage, which is often much cheaper.
:::

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

(tracking-cost-and-usage-opencode)=
## Tracking cost and usage

The OpenCode CLI reports a per-step cost and token breakdown for each provider call.
When present, the cost is summed to populate `cost_usd` in
{class}`~open_atp.provers.base.ProofResult`; when a provider does not self-report USD
(e.g. an OAuth plan), the cost is estimated from the token counts using
{data}`~open_atp.harness.cost.COST_PER_MTOK` (see
{func}`~open_atp.harness.cost.compute_cost_usd`). You can also monitor consumption from
your provider's usage dashboard.
