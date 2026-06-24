(prover-codex)=
# Codex

The Codex prover is the {class}`~open_afps.provers.agent_prover.AgentProver` on the
{class}`~open_afps.harness.codex.CodexHarness` — OpenAI's
[Codex](https://chatgpt.com/codex) CLI driving the `sorry`s in a sandbox with the
[lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp) server. The shared
{class}`~open_afps.core.verifier.Verifier` does the final compile / sorry / axiom
check. See {doc}`index` for the staging/diff lifecycle every agent harness shares.

## Usage

```python
from open_afps.backends.docker import DockerBackend, DockerConfig
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_afps.provers import AgentProver, AgentProverConfig

backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
config = AgentProverConfig(
    image=DEFAULT_IMAGE,
    supported_toolchain=DEFAULT_TOOLCHAIN,
    harness="codex",
    model="gpt-5.5",
    effort="high",
)
prover = AgentProver(config, verification_backend=backend)
```

Or by registry spec through {func}`~open_afps.api.build_prover` / the CLI:
`agent:codex`. Because Codex authenticates through ChatGPT/OpenAI it must run an
OpenAI model, so the `agent:codex` spec defaults `model` to `gpt-5.5` rather than the
`AgentProverConfig` Anthropic default.

## Harness details

Codex does not auto-discover `.mcp.json`, so `configure_wd` mounts the bundle's skills
under `.agents/skills/` and the lean-lsp MCP server is wired through `-c` overrides on
the command line instead of a config file. The launch script
(`assets/scripts/codex_agent.sh`) runs:

```bash
codex exec --json --skip-git-repo-check \
    --sandbox danger-full-access \
    --model '<MODEL>' \
    -c 'mcp_servers.lean-lsp.command="lean-lsp-mcp"' \
    -c 'mcp_servers.lean-lsp.args=[]' \
    -c 'model_reasoning_effort="<EFFORT>"' \
    "$PROMPT"
```

`codex exec` runs non-interactively; `danger-full-access` grants the broad
permissions the in-place edits need (safe in the container). Note `effort` is passed
via the `model_reasoning_effort` override, not a `--effort` flag. The `--json` event
stream goes to stdout.

## Authentication

Codex is available on paid ChatGPT plans — compare plans at
[ChatGPT pricing](https://chatgpt.com/pricing/) and monitor consumption at
[Analytics](https://chatgpt.com/codex/cloud/settings/analytics). Authenticate the
Codex CLI once on the host:

```bash
codex login
```

This writes credentials to `~/.codex`. The harness exposes that directory inside the
sandbox at run time so Codex can refresh its access token mid-session, billing against
your ChatGPT subscription.

## Cost tracking

The Codex CLI does not report per-run USD. `parse` sums token totals from the
`turn.completed` events (tolerating the `input_tokens` / `inputTokens` /
`prompt_tokens` field-name variants) and leaves `cost_usd` `None`, so the prover
estimates USD from the pricing table in
{data}`~open_afps.harness.cost.COST_PER_MTOK`. Keep that table aligned with current
OpenAI API prices.
