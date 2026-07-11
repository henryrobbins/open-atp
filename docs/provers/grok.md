# Grok

```{include} _meta_grok.md
:parser: myst
```

Use xAI's Grok as an automated theorem prover with Lean skills and MCP tooling
(default model `grok-4.5`).

The **standard `grok` prover** ({func}`~open_atp.config.standard_prover`) drives Grok
through the {class}`~open_atp.harness.opencode.OpenCodeHarness`'s `xai` provider, **not**
the native Grok CLI. The Grok CLI drives tools over [ACP](https://docs.x.ai/build/cli/headless-scripting#acp)
with aggressive parallel dispatch: it fires several `lean-lsp` MCP calls at once at the
single-threaded language server, they serialize behind the first cold
`lean_diagnostic_messages`, and each trips the MCP tool timeout — burning the whole
generation budget. Grok exposes no knob to serialize tool calls, so the standard prover
routes Grok through opencode instead, which drives `lean-lsp` without that contention.

The native {class}`~open_atp.harness.grok.GrokHarness` (Grok Build, `grok agent`) is
still available for explicit use and is documented below.

:::{warning}
**Regional availability.** Grok is a hosted xAI model and `grok-4.5` is geo-gated:
requests are refused with `403 permission-denied: The model grok-4.5 is not available in
your region` from some countries. This depends only on the country the request egresses
from, not your account. On the {class}`~open_atp.backends.modal.ModalBackend`, sandboxes
are scheduled wherever Modal has capacity, so **without a region pin some sandboxes
egress from outside the US and 403 while others succeed in the same sweep**. The backend
therefore defaults to `region="us"`. Override it via the benchmark `compute` config if
you need a different region:

```yaml
compute:
  type: modal
  region: us   # or "eu", "us-east", ["us-east", "us-west"], ...
```
:::

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

The Grok CLI does not self-report a USD cost, so the run cost is estimated from token
counts using the pricing table in {data}`~open_atp.harness.cost.COST_PER_MTOK` (see
{func}`~open_atp.harness.cost.compute_cost_usd`). Getting those counts right takes some
care, because of how the ACP protocol reports usage.

### Why the token counts are estimated

ACP exposes usage in exactly one place: the `_meta` on the final `session/prompt`
response (`inputTokens`, `outputTokens`, `cachedReadTokens`, `reasoningTokens`). That
`_meta` covers **only the final assistant turn**, not the whole agentic loop. A run
that took hundreds of tool-call turns — streaming thousands of reasoning and file-edit
tokens — reports an `outputTokens` of only a few hundred (just the closing summary
message), and an `inputTokens` that is ~99% `cachedReadTokens` (the final turn re-reads
the whole conversation from cache). None of the intermediate turns' usage is exposed by
the protocol, and grok's on-disk session state records only *context-window occupancy*
(a snapshot that rises and falls), never cumulative billed tokens. So the raw ACP
`outputTokens` undercounts a real run by an order of magnitude or more.

### How the estimate is built

To make the figure meaningful, {class}`~open_atp.harness.grok.GrokHarness` reconstructs
a **cumulative output estimate** from the event stream instead of trusting the
final-turn count:

- **Output tokens** — sum the characters of everything the model *generated* across the
  whole run: the coalesced `agent_message` and `agent_thought` text plus each
  `tool_call`'s `rawInput` (the file contents and arguments it wrote), then divide by
  `_CHARS_PER_TOKEN` (≈ 4 chars/token, the standard rough heuristic for English +
  code). This is dependency-free — no tokenizer is bundled.
- **Input tokens** — kept as the reported final-turn `inputTokens`. Because every token
  enters the context as full-price ("fresh") input exactly once and is cache-read
  thereafter, the final-turn value (≈ peak context size) approximates the run's total
  *distinct* input.

Cost is then `input × input_rate + output × output_rate` from the pricing table.

### Accuracy and limits

The estimate is a **rough floor**, not an exact charge:

- The `≈ 4 chars/token` heuristic is typically within ~15–20%; grok's real tokenizer is
  not available locally.
- **Cumulative cached-context re-reads are not counted.** Each turn re-reads the prior
  context at the discounted cached-input rate; on a long multi-turn run that volume is
  large (though individually cheap). The estimate omits it, so it under-counts the
  input side of long runs.
- It assumes no context **compaction**; a compacted run re-summarizes history, breaking
  the "each token is fresh input once" approximation.

For authoritative spend, use the [xAI console](https://console.x.ai/) usage dashboard —
the CLI uploads usage there for server-side accounting.
