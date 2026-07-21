# Provers

An automated theorem prover takes a formal statement in [Lean](https://lean-lang.org/) and attempts to fill all `sorry` uses. OpenATP supports many theorem-provers ranging from general purpose coding agents to specialized theorem-proving agents.

```{include} _table.md
:parser: myst
```

Each prover is implemented as a subclass of {class}`~open_atp.provers.base.AutomatedProver`. There are multiple agent provers ({class}`~open_atp.provers.agent_prover.AgentProver`) that forward the task to a coding-agent harness ({class}`~open_atp.harness.base.Harness`). Agent harnesses are augmented with skills and MCP tooling.

- **Skills.** Most provers use the official Lean skills {cite:p}`leanprover_skills`. Claude Code additionally uses the `lean4` skills packaged in the `lean4` Claude Code plugin {cite:p}`lean4_skills`.
- **MCP.** The `lean-lsp-mcp` server {cite:p}`lean_lsp_mcp` exposes the Lean language server as tools to provide rich feedback while iterating on proofs.

```{toctree}
:maxdepth: 1
:hidden:

claude_code
codex
opencode
axproverbase
leanstral
kimi
numina
aristotle
```
