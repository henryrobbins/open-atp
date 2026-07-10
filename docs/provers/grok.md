# Grok

```{include} _meta_grok.md
:parser: myst
```

Use xAI's [Grok CLI](https://docs.x.ai/build/overview) (Grok Build) as an automated theorem prover with Lean skills and MCP tooling. This prover uses the {class}`~open_atp.provers.agent_prover.AgentProver` with the {class}`~open_atp.harness.grok.GrokHarness`, driving the `grok` coding agent non-interactively against an xAI model (default `grok-4.5`).

## Authentication

The harness reuses the `grok` CLI's OAuth login, so runs draw on the logged-in xAI plan rather than a metered API key. Log in once on the host:

```bash
grok login
```

This writes `~/.grok/auth.json`. The harness stages **only** that file into the sandbox (never the whole `~/.grok`, which also holds the CLI binary and personal state) and points `GROK_HOME` at it, so grok reads the credential there. To use a login from a non-default location, pass it explicitly:

```{testcode}
from pathlib import Path

from open_atp.harness import GrokHarness

GrokHarness(auth_file=Path("~/.grok/auth.json").expanduser())
```

The mounted access token is short-lived; if it has expired, re-run `grok login` on the host before a batch.

## Using the prover

### Standard prover via Python API

The simplest way to run the prover is through {func}`~open_atp.config.standard_prover`, which uses a standard configuration pointing at `grok-4.5`. Log in with `grok login` on the host first (or pass `auth_file` to the harness). Here, we prove the {ref}`MUL_REORDER` example theorem:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover
from open_atp.examples import EXAMPLE, example_task

task = example_task(EXAMPLE.MUL_REORDER)
prover = standard_prover("grok", backend=DockerBackend())
result = prover.prove(task, output_dir=Path("demo"))
```

### Standard prover via CLI

The standard prover can also be run from the CLI:

```bash
open-atp prove path/to/task.lean output_dir grok
```

### Customizing the prover

To override knobs like `model` and `effort`, construct the class directly. Set `model="grok-build-0.1"` to use xAI's code-specialized model instead of `grok-4.5`:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.examples import EXAMPLE, example_task
from open_atp.harness import GrokHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers import AgentProver

task = example_task(EXAMPLE.MUL_REORDER)
prover = AgentProver(
    harness=GrokHarness(model="grok-4.5", effort="medium"),
    backend=DockerBackend(image=DEFAULT_IMAGE),
)
result = prover.prove(task, output_dir=Path("demo"))
```

## Harness details

By default, the Grok harness is equipped with:

- Official Lean skills {cite:p}`leanprover_skills`.
- `lean-lsp-mcp` MCP server {cite:p}`lean_lsp_mcp`, wired in via a project-scope `.grok/config.toml`.

The agent prompt (below) is written into the working directory and read into `$PROMPT`. Grok is then driven over [ACP](https://docs.x.ai/build/cli/headless-scripting#acp) (its Agent Client Protocol: `grok agent stdio`, JSON-RPC 2.0) by a small staged Python driver, `grok_acp.py`, rather than `grok --single` — the single-shot mode emits only a terminal result object, so a run's tool calls never reach stdout, whereas the ACP stream surfaces them (and the token usage) as events the driver re-emits as JSONL. See the launch script below for the full invocation.

:::{dropdown} Agent Prompt
:icon: book
```{literalinclude} ../../src/open_atp/provers/agent_prover.py
:language: text
:start-after: PROVER_PROMPT = """
:end-before: END PROVER_PROMPT
```
:::

:::{dropdown} `src/open_atp/harness/assets/scripts/grok_agent.sh`
:icon: code
```{literalinclude} ../../src/open_atp/harness/assets/scripts/grok_agent.sh
:language: bash
```
:::

:::{dropdown} `src/open_atp/harness/assets/scripts/grok_acp.py`
:icon: code
```{literalinclude} ../../src/open_atp/harness/assets/scripts/grok_acp.py
:language: python
```
:::

(tracking-cost-and-usage-grok)=
## Tracking cost and usage

The Grok CLI does not self-report a USD cost, so the run cost is estimated from the token totals in its JSON output using the pricing table in {data}`~open_atp.harness.cost.COST_PER_MTOK` (see {func}`~open_atp.harness.cost.compute_cost_usd`). You can also monitor consumption from the [xAI console](https://console.x.ai/) usage dashboard.
