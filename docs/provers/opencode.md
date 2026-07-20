# OpenCode

Use [OpenCode](https://opencode.ai/) as an automated theorem prover with common skills and MCP tooling for working with Lean. OpenCode is an open-source coding-agent harness with many supported model providers. OpenATP supports OpenCode through the {class}`~open_atp.provers.agent_prover.AgentProver` and the {class}`~open_atp.harness.opencode.OpenCodeHarness`.

```{toctree}
:maxdepth: 1
:hidden:

deepseek
```

## Authentication

OpenCode bills directly against an API provider rather than a flat-rate subscription. Sign up for an API account with your chosen provider, fund it, and find the full provider list at [OpenCode providers](https://opencode.ai/docs/providers/). By default the harness reads the provider's key from the host environment, for example:

```bash
export DEEPSEEK_API_KEY=...
```

It is recommended to define this in a `.env` file in your project root. Alternatively, pass the key matching your chosen provider to the harness explicitly:

```{testcode}
from open_atp.harness import OpenCodeHarness

OpenCodeHarness(model="claude-opus-4-8", provider_api_key="sk-...")
```

The provider is inferred from the model prefix unless you pass `provider` explicitly. Either way the harness forwards the key into the sandbox under its canonical env var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, or `DEEPSEEK_API_KEY`).

:::{tip}
If you are harness agnostic and want to use Anthropic or OpenAI models, it is recommended to use the {doc}`/provers/claude_code` or {doc}`/provers/codex`  provers. These provers are billed against subscription plans rather than API usage, which is often much cheaper.
:::

## Harness details

By default, the OpenCode harness is equipped with:

- Official Lean skills {cite:p}`leanprover_skills`.
- `lean-lsp-mcp` MCP server {cite:p}`lean_lsp_mcp`.

The agent prompt (below) is written into the working directory and read into `$PROMPT`. The OpenCode CLI is then invoked in non-interactive mode with `$PROMPT` as the input. See the script below for the full OpenCode CLI invocation.

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

The OpenCode CLI reports a per-step cost and token breakdown for each provider call. The cost is summed to populate `cost_usd` in {class}`~open_atp.provers.base.ProofResult`. You can also monitor consumption from your provider's usage dashboard — each model page links its own.
