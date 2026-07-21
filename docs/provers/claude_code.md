# Claude Code

```{include} _meta_claude_code.md
:parser: myst
```

Use [Claude Code](https://claude.com/claude-code) as an automated theorem prover with common skills and MCP tooling for working with Lean. This prover uses the {class}`~open_atp.provers.agent_prover.AgentProver` with the {class}`~open_atp.harness.claude_code.ClaudeCodeHarness`.

## Authentication

Claude Code is included with every paid Claude plan. [Choose a Claude plan](https://support.claude.com/en/articles/11049762-choose-a-claude-plan) and sign up if you don't have an account. [Install](https://code.claude.com/docs/en/quickstart) the Claude Code CLI and generate a long-lived OAuth token once on the host:

```bash
claude setup-token
```

Check you are properly authenticated with:

```bash
open-atp auth-status claude
```

By default, the harness will read `CLAUDE_CODE_OAUTH_TOKEN` from the host environment. It is recommended to define this in a `.env` file in your project root.

```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
```

Alternatively, pass the token to the harness explicitly:

```{testcode}
from open_atp.harness import ClaudeCodeHarness

ClaudeCodeHarness(oauth_token="sk-ant-oat01-...")
``` 

The harness forwards `CLAUDE_CODE_OAUTH_TOKEN` into the sandbox at run
time, billing against your Claude plan (not the API). See {ref}`tracking-cost-and-usage-claude`.

## Using the prover

### Standard prover via Python API

The simplest way to run the prover is through {func}`~open_atp.config.standard_prover` which uses a standard configuration. Here, we prove the {ref}`MUL_REORDER` example theorem:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover
from open_atp.examples import EXAMPLE, example_task

task = example_task(EXAMPLE.MUL_REORDER)
prover = standard_prover("claude", backend=DockerBackend())
result = prover.prove(task, output_dir=Path("demo"))
```

### Standard prover via CLI

The standard prover can also be run from the CLI:

```bash
open-atp prove path/to/task.lean output_dir claude
```

### Customizing the prover

To override knobs like `model` and `effort`, construct the class directly. In this example, we use the `claude-opus-4-8` model with `high` effort:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.examples import EXAMPLE, example_task
from open_atp.harness import ClaudeCodeHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers import AgentProver

task = example_task(EXAMPLE.MUL_REORDER)
prover = AgentProver(
    harness=ClaudeCodeHarness(model="claude-opus-4-8", effort="high"),
    backend=DockerBackend(image=DEFAULT_IMAGE),
)
result = prover.prove(task, output_dir=Path("demo"))
```

See the {doc}`/api/index` for all {class}`~open_atp.harness.claude_code.ClaudeCodeHarness` configuration options.

:::{warning}
Claude Code does not support all of the models available in the Claude API.
:::

## Harness details

By default, the Claude Code harness is equipped with:

- Official Lean skills {cite:p}`leanprover_skills`.
- Lean4 Claude Code plugin {cite:p}`lean4_skills`.
- `lean-lsp-mcp` MCP server {cite:p}`lean_lsp_mcp`.

The agent prompt (below) is written into the working directory and read into `$PROMPT`. The Claude Code CLI is then invoked in non-interactive mode with `$PROMPT` as the input. See the script below for the full Claude Code CLI invocation.

:::{dropdown} Agent Prompt
:icon: book
```{literalinclude} ../../src/open_atp/provers/agent_prover.py
:language: text
:start-after: PROVER_PROMPT = """
:end-before: END PROVER_PROMPT
```
:::

:::{dropdown} `src/open_atp/harness/assets/scripts/claude_code_agent.sh`
:icon: code
```{literalinclude} ../../src/open_atp/harness/assets/scripts/claude_code_agent.sh
:language: bash
```
:::

(tracking-cost-and-usage-claude)=
## Tracking cost and usage

The Claude Code CLI's JSON event stream reports per-run USD directly (`total_cost_usd` in the final `result` object). This populates `cost_usd` in
{class}`~open_atp.provers.base.ProofResult`. Note that this cost is based on API rates. Usage within your plan's quota is not billed. You can monitor plan consumption at [Usage](https://claude.ai/settings/usage).

:::{warning}
There are plans to migrate the Claude Agent SDK and `claude -p` non-interactive usage to no longer count towards Claude plan usage limits. This was originally planned for June 15, 2026. It has been delayed to a later date. See [this article](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan).
:::

:::{warning}
Running a large number of proofs can quickly consume your plan's 5 hour session quota.
:::
