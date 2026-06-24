(prover-opencode)=
# OpenCode

The OpenCode prover is the {class}`~open_atp.provers.agent_prover.AgentProver` on the
{class}`~open_atp.harness.opencode.OpenCodeHarness` — the
[OpenCode](https://opencode.ai/) CLI driving the `sorry`s in a sandbox with the
[lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp) server. Unlike Claude Code and
Codex, OpenCode is provider-agnostic: one CLI fronts Anthropic, OpenAI, Google, or
DeepSeek, billed directly against that provider's API. The shared
{class}`~open_atp.verify.Verifier` does the final compile / sorry / axiom
check. See {doc}`index` for the staging/diff lifecycle every agent harness shares.

## Usage

```python
from open_atp.backends.docker import DockerBackend, DockerConfig
from open_atp.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_atp.provers import AgentProver, AgentProverConfig

backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
config = AgentProverConfig(
    image=DEFAULT_IMAGE,
    supported_toolchain=DEFAULT_TOOLCHAIN,
    harness="opencode",
    model="claude-opus-4-8",
    effort="medium",
)
prover = AgentProver(config, verification_backend=backend)
```

Or by registry spec through {func}`~open_atp.provers.get_prover` / the CLI:
`agent:opencode`. The provider is inferred from the model prefix (`claude-*` →
`anthropic`, `gpt-*` → `openai`, and so on), so any provider's model is selected by
name through the same `model` knob.

## Harness details

`configure_wd` writes an `opencode.json` carrying the inferred provider, the model and
its reasoning-effort config, and the lean-lsp MCP server, plus mounts the bundle's
skills — the host-agnostic [`leanprover/skills`](https://github.com/leanprover/skills)
— under `.agents/skills/`. The MCP `timeout` is raised to **180 000 ms (180 s)**
— the first `lean_diagnostic_messages` call starts `lake serve` and loads the file's
full Mathlib import closure, which blows past the 60 s default on a cold, few-CPU
sandbox. Reasoning effort maps per provider: Anthropic gets `thinking: {type:
"adaptive"}` plus an effort `output_config`, while OpenAI / Google / DeepSeek get
`reasoningEffort`. The launch script (`assets/scripts/opencode_agent.sh`) runs:

```bash
opencode run --dir /workspace/wd --format json \
    --model '<PROVIDER>/<MODEL>' \
    "$PROMPT"
```

The `--format json` event stream goes to stdout.

`$PROMPT` is the task's `instructions` when set, otherwise the shared default agent
prompt baked into the {class}`~open_atp.provers.agent_prover.AgentProver`:

:::{dropdown} Default agent prompt
:icon: code
```{literalinclude} ../../src/open_atp/provers/agent_prover.py
:language: text
:start-after: _DEFAULT_PROMPT = """
:end-before: END _DEFAULT_PROMPT
```
:::

## Authentication

OpenCode bills directly against an API provider rather than a flat-rate subscription.
Sign up for an API account with your chosen provider, fund it, and monitor consumption
from that provider's usage dashboard — see
[OpenCode providers](https://opencode.ai/docs/providers/) for the full list. Export the
key matching your chosen provider on the host, for example:

```bash
export DEEPSEEK_API_KEY=...
```

The harness forwards whichever provider keys are present —
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `DEEPSEEK_API_KEY` — into the
sandbox; the one matching the chosen model is used.

## Cost tracking

The OpenCode CLI reports a per-step cost and token breakdown for each provider call.
`parse` sums `step_finish` events — input (`tokens.input` plus cache write/read),
output (`tokens.output`), and `cost` — into `cost_usd` in
{class}`~open_atp.harness.base.HarnessRunResult`, so cost comes straight from the
provider via OpenCode.
