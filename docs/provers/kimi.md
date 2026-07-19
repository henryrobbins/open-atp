# Kimi Code

```{include} _meta_kimi.md
:parser: myst
```

Use Moonshot AI's [Kimi Code](https://moonshotai.github.io/kimi-code/) CLI as an automated theorem prover with Lean skills and MCP tooling. This prover uses the {class}`~open_atp.provers.agent_prover.AgentProver` with the {class}`~open_atp.harness.kimi.KimiHarness`.

## Authentication

Kimi Code authenticates with a Moonshot account via a device-code flow. [Install](https://moonshotai.github.io/kimi-code/en/guides/getting-started) the Kimi Code CLI and log in once on the host:

```bash
kimi login
```

This writes an OAuth credential (plus provider config and a device id) under Kimi's data directory, `~/.kimi-code` (or `$KIMI_CODE_HOME`). Unlike the API-key harnesses, there is no env var to forward; the harness stages that directory into a workdir-local `KIMI_CODE_HOME` inside the sandbox so Kimi can refresh its access token mid-session. Point the harness at a different data directory to override:

```{testcode}
from pathlib import Path

from open_atp.harness import KimiHarness

KimiHarness(home_dir=Path("~/.kimi-code").expanduser())
```

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

- Official Lean skills {cite:p}`leanprover_skills`, staged into the user-scope skills directory under `KIMI_CODE_HOME`.
- `lean-lsp-mcp` MCP server {cite:p}`lean_lsp_mcp`, wired in via a user-scope `mcp.json`.

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

Kimi Code's `stream-json` output carries only messages and tool calls, not token totals. The harness reads token usage from Kimi's per-session `wire.jsonl` (`usage.record` events) synced back from the workdir-local `KIMI_CODE_HOME`, populating `input_tokens`/`output_tokens` in {class}`~open_atp.provers.base.ProofResult`. Kimi bills a flat subscription rate and reports no per-run USD, so `cost_usd` is left unset.
