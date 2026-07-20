# Harnesses

An agent prover ({class}`~open_atp.provers.agent_prover.AgentProver`) forwards the proof task to a coding-agent **harness** ({class}`~open_atp.harness.base.Harness`) — the CLI that runs the model, staged into the sandbox with Lean skills and MCP tooling. The harness is independent of the model it drives: the same OpenCode harness fronts DeepSeek, Anthropic, OpenAI, or Google models. Each prover on the {doc}`/provers/index` list pins a harness to a specific model.

```{include} _table.md
:parser: myst
```

Skills and MCP are properties of the harness, not the model it runs:

- **Skills.** Most harnesses use the official Lean skills {cite:p}`leanprover_skills`. Claude Code additionally uses the `lean4` skills packaged in the `lean4` Claude Code plugin {cite:p}`lean4_skills`.
- **MCP.** The `lean-lsp-mcp` server {cite:p}`lean_lsp_mcp` exposes the Lean language server as tools to provide rich feedback while iterating on proofs.

Only harnesses with a dedicated page appear in the table as a link; for the others, harness detail currently lives on the prover page.

```{toctree}
:maxdepth: 1
:hidden:

opencode
```
