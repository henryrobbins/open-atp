(prover-vibe)=
# Vibe / Leanstral

The Vibe prover is the {class}`~open_atp.provers.agent_prover.AgentProver` on the
{class}`~open_atp.harness.vibe.VibeHarness` — it drives Mistral
[Vibe](https://docs.mistral.ai/mistral-vibe/)'s builtin `lean` agent in a sandbox.
Vibe's `lean` agent *is* Leanstral (`vibe -p ... --agent lean` pins the model to
`leanstral`; there is no `--model` flag). The shared
{class}`~open_atp.verify.Verifier` does the final compile / sorry / axiom
check. See {doc}`index` for the lifecycle every agent harness shares.

## The Leanstral stand-in

The bare `lean` profile is Labs-gated: the real model `labs-leanstral-2603` returns a
`403` until a Mistral org admin enables Labs. Until then, the harness runs the same
Lean scaffold on a non-Labs **reasoning** model any La Plateforme key can reach
(default Magistral) through a vendored `lean-standin` agent profile. Since Vibe has no
`--model` flag, the harness templates the configured model into the stand-in profile
(`<<MODEL>>`) at `configure_wd` time — so the model is an ordinary knob. Repoint
`agent` to `lean` (and the model to `labs-leanstral-2603`) once Labs is enabled.

## Usage

```python
from open_atp.backends.docker import DockerBackend, DockerConfig
from open_atp.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_atp.provers import AgentProver, AgentProverConfig

backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
config = AgentProverConfig(
    image=DEFAULT_IMAGE,
    supported_toolchain=DEFAULT_TOOLCHAIN,
    harness="vibe",
    agent="lean-standin",            # "lean" once Labs is enabled
    model="magistral-medium-latest",
    max_turns=None,                  # passed to `vibe -p --max-turns`
    max_price=None,                  # passed to `vibe -p --max-price`
)
prover = AgentProver(config, verification_backend=backend)
```

Or by registry spec through {func}`~open_atp.provers.get_prover` / the CLI: `vibe`
(defaults: `agent="lean-standin"`, `model="magistral-medium-latest"`). Swap the model
with `overrides={"model": "devstral-medium-latest"}`. The `agent`, `max_turns`, and
`max_price` fields are Vibe-specific.

## Harness details

`configure_wd` pins a workdir-local `VIBE_HOME` (`.vibe/`) so Vibe's config, the
vendored stand-in profile, and the per-session log all live under the workdir and sync
back out with it. The written `.vibe/config.toml`:

- sets `installed_agents = ["lean"]` to un-gate the builtin `lean` agent;
- sets `bypass_tool_permissions = true` — the only way to un-gate mutating tools
  (`edit`, `write_file`) in `vibe -p` programmatic mode, which has no approval
  callback (without it the agent's edits are silently skipped);
- enables `[session_logging]` (where cost/token totals are recorded); and
- wires the lean-lsp MCP server with `tool_timeout_sec = 180` (the seconds-valued
  mirror of the OpenCode 180 s fix, for the cold first `lean_diagnostic_messages`
  call that loads the full Mathlib import closure).

The stand-in profile is written to `.vibe/agents/<agent>.toml`, and the bundle's
skills — the host-agnostic [`leanprover/skills`](https://github.com/leanprover/skills)
— are mounted under `.vibe/skills` (Vibe's user skills dir, which loads
regardless of project-folder trust). The launch script
(`assets/scripts/vibe_agent.sh`) runs:

```bash
export VIBE_HOME="$PWD/.vibe"
vibe -p "$PROMPT" --agent <AGENT> --output streaming --workdir "$PWD" <EXTRA>
```

`<EXTRA>` appends `--max-turns` / `--max-price` when set. The `--output streaming`
NDJSON message stream (one message per line) goes to stdout.

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

The harness forwards `MISTRAL_API_KEY` (a Mistral La Plateforme key) from the host;
the lean agent's provider reads it from the process env. It must be set or the harness
raises.

## Cost tracking

The streaming output carries only conversation messages — no token/cost totals. Those
live in Vibe's per-session `meta.json`; `parse` reads `session_cost`,
`session_prompt_tokens`, and `session_completion_tokens` from its `stats` to populate
`cost_usd` and the token totals in
{class}`~open_atp.harness.base.HarnessRunResult`. `collect_logs` relocates the
`.vibe/logs` tree to `logs/vibe-session`.
