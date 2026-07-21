# Numina

```{include} _meta_numina.md
:parser: myst
```

Numina-Lean-Agent {cite:p}`liu2026numina` is an automated theorem prover built on top of Claude Code. It adds a custom selection of skills, prompts, and search tooling to the base harness, and runs in a multi-round loop with a statement tracker. The prover uses the {class}`~open_atp.provers.numina.NuminaProver` with the {class}`~open_atp.harness.claude_code.ClaudeCodeHarness`.

## Authentication

Numina runs on the Claude Code CLI, so it authenticates exactly like the {doc}`claude_code` prover. Generate a long-lived OAuth token once on the host:

```bash
claude setup-token
```

Check you are properly authenticated with:

```bash
open-atp auth-status numina
```

By default {class}`~open_atp.provers.numina.NuminaProver` reads `CLAUDE_CODE_OAUTH_TOKEN` from the host environment (for example a `.env` file in your project) and forwards it into the sandbox. To supply the token explicitly, pass it as the `oauth_token` argument to {class}`~open_atp.provers.numina.NuminaProver`.

Numina's helper skills additionally call out to Leandex / Gemini / GPT (and Claude for the informal prover). Their keys (`LEAN_LEANDEX_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, and `ANTHROPIC_API_KEY`) are forwarded into the sandbox when present in the host env. A skill whose key is absent degrades or skips rather than failing the run. It is recommended to define all keys in a `.env` file in your project root.

```
CLAUDE_CODE_OAUTH_TOKEN=...
LEAN_LEANDEX_API_KEY=...
GEMINI_API_KEY=...
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
```

Also see {ref}`tracking-cost-and-usage-numina`.

## Using the prover

### Standard prover via Python API

The simplest way to run the prover is through {func}`~open_atp.config.standard_prover` which uses a standard configuration. Here, we prove the {ref}`MUL_REORDER` example theorem:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover
from open_atp.examples import EXAMPLE, example_task

task = example_task(EXAMPLE.MUL_REORDER)
prover = standard_prover("numina", backend=DockerBackend())
result = prover.prove(task, output_dir=Path("demo"))
```

### Standard prover via CLI

The standard prover can also be run from the CLI:

```bash
open-atp prove path/to/task.lean output_dir numina
```

### Customizing the prover

To override knobs like `max_rounds` and `guard_statements`, construct {class}`~open_atp.provers.numina.NuminaProver` directly (the harness is fixed to Claude Code to match Numina-Lean-Agent's configuration):

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.examples import EXAMPLE, example_task
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers.numina import NuminaProver

task = example_task(EXAMPLE.MUL_REORDER)
prover = NuminaProver(
    backend=DockerBackend(image=DEFAULT_IMAGE),
    max_rounds=20,
    guard_statements=True,
)
result = prover.prove(task, output_dir=Path("demo"))
```

See the {doc}`/api/index` for all {class}`~open_atp.provers.numina.NuminaProver` configuration options.

## Prover details

The Numina prover uses Claude Code without any of the skills used by the other agent harnesses {cite:p}`leanprover_skills`. Instead, it defines its own set of skills and prompts. It runs a multi-round loop with a statement tracker. Each round makes a single call to the Claude Code CLI with the `main_entry.md` prompt below.

:::{dropdown} `vendor/numina/prompts/main_entry.md`
```{literalinclude} ../../vendor/numina/prompts/main_entry.md
:class: wrap
:language: markdown
```
:::

(tracking-cost-and-usage-numina)=
## Tracking cost and usage

The Claude Code CLI's JSON output reports per-run cost directly. The `discussion_partner` skill makes API calls to Gemini and GPT — the cost of each API call is recorded in the working directory. Claude Code CLI and API costs are added to populate `cost_usd` in {class}`~open_atp.provers.base.ProofResult`. The helper ledger records only input and output totals, so those API calls are priced at the uncached input rate. Gemini and GPT usage can be monitored from their respective dashboards.

:::{warning}
Unlike the {doc}`/provers/claude_code` prover, the Numina prover does not solely bill your Claude plan. The `discussion_partner` skill makes API calls to Gemini and GPT, which can quickly become costly.
:::
