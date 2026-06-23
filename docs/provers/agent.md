(prover-agent)=
# AgentProver

The {class}`~open_afps.provers.agent_prover.AgentProver` runs a coding agent
(Claude Code, Codex, or OpenCode) with the
[lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp) server inside a
{class}`~open_afps.backends.base.ComputeBackend`. It composes two concerns:

- a {class}`~open_afps.harness.base.Harness` — the *agent* concern: launch
  script, credential forwarding, and output parsing (see
  {doc}`../agent_harness/index`); and
- a {class}`~open_afps.backends.base.ComputeBackend` — the *compute* concern: where
  the agent runs, with Lean+Mathlib and the lean-lsp MCP server.

`prove` stages the project into the workdir, lets the agent fill the `sorry`s in
place, then diffs the `.lean` files against the staged originals to report what
changed. The shared {class}`~open_afps.core.verifier.Verifier` does the final
compile / sorry / axiom check.

## Usage

```python
from open_afps.backends.docker import DockerBackend, DockerConfig
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_afps.provers import AgentProver, AgentProverConfig

backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
config = AgentProverConfig(
    image=DEFAULT_IMAGE,
    supported_toolchain=DEFAULT_TOOLCHAIN,
    harness="claude_code",   # one of: claude_code | codex | opencode
    model="claude-opus-4-8",
    effort="high",
)
prover = AgentProver(config, verification_backend=backend)
```

See {doc}`../user_guide/run_provers` for an end-to-end run and
{doc}`../agent_harness/index` for per-harness credential setup. Configuration fields
are documented under {class}`~open_afps.provers.agent_prover.AgentProverConfig` in the
{doc}`../api/provers` reference.

## Cost tracking

How a run's `cost_usd` is determined depends on the harness: Claude Code reports USD
directly, OpenCode sums provider step costs, and Codex is estimated from token totals
via {data}`~open_afps.harness.cost.COST_PER_MTOK`. See each
{doc}`harness page <../agent_harness/index>` for details.
