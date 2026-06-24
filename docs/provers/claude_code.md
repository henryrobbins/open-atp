(prover-claude-code)=
# Claude Code

The Claude Code prover is the {class}`~open_atp.provers.agent_prover.AgentProver` on
the {class}`~open_atp.harness.claude_code.ClaudeCodeHarness` — Anthropic's
[Claude Code](https://claude.com/claude-code) CLI driving the `sorry`s in a sandbox
with the [lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp) server. It is the
default harness: the bare `agent` registry spec selects it, and the shared
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
    harness="claude_code",
    model="claude-opus-4-8",
    effort="high",
)
prover = AgentProver(config, verification_backend=backend)
```

Or by registry spec through {func}`~open_atp.provers.get_prover` / the CLI: `agent`
(Claude Code is the default harness, so no `:harness` suffix is needed).

## Harness details

`configure_wd` writes a project-scope `.mcp.json` registering the lean-lsp MCP server
and mounts the bundle's skills — the host-agnostic
[`leanprover/skills`](https://github.com/leanprover/skills) — under `.claude/skills/`.
Claude Code is the only harness that also loads **plugins** — the default bundle's
`lean4` plugin, vendored from
[`cameronfreer/lean4-skills`](https://github.com/cameronfreer/lean4-skills), is staged
under `.plugins/<name>/`. The launch script
(`assets/scripts/claude_code_agent.sh`) runs:

```bash
claude -p "$PROMPT" \
    --output-format stream-json --verbose \
    --permission-mode bypassPermissions \
    --mcp-config .mcp.json --strict-mcp-config \
    --model '<MODEL>' --effort '<EFFORT>'<PLUGIN_FLAGS>
```

`<PLUGIN_FLAGS>` expands to one `--plugin-dir .plugins/<name>` per mounted plugin —
the only way to load a local plugin in a headless `-p` run, so its `SessionStart`
hooks and subagents fire. `bypassPermissions` skips approval prompts (safe in the
container); the prover sets `IS_SANDBOX=1` so that mode runs non-interactively, and
`CLAUDE_CODE_FORK_SUBAGENT=1` when plugins are mounted. The `stream-json` event
stream goes to stdout.

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

Claude Code is included with every paid Claude plan — compare plans at
[Choose a Claude plan](https://support.claude.com/en/articles/11049762-choose-a-claude-plan)
and monitor consumption at [Usage](https://claude.ai/settings/usage). Generate a
long-lived OAuth token once on the host:

```bash
claude setup-token
```

Save the printed token to a `.env` file in your project:

```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
```

The harness forwards `CLAUDE_CODE_OAUTH_TOKEN` into the sandbox at run time, billing
against your Claude plan rather than the API.

## Cost tracking

The Claude Code CLI's JSON event stream reports per-run USD directly (`total_cost_usd`
in the final `result` object), so `cost_usd` in
{class}`~open_atp.harness.base.HarnessRunResult` is read straight from the stream
along with input/output token totals.
