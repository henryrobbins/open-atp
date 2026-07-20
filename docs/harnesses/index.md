# Harnesses

An agent prover ({class}`~open_atp.provers.agent_prover.AgentProver`) forwards the proof task to a coding-agent **harness** ({class}`~open_atp.harness.base.Harness`) — the CLI that runs the model, staged into the sandbox with Lean skills and MCP tooling. The harness is independent of the model it drives: the same OpenCode harness fronts DeepSeek, Anthropic, OpenAI, or Google models. Each prover on the {doc}`/provers/index` list pins a harness to a specific model.

```{include} _table.md
:parser: myst
```

Each prover's skills and MCP tooling are listed on the {doc}`/provers/index` table. Only harnesses with a dedicated page appear above as a link; for the others, harness detail currently lives on the prover page.

```{toctree}
:maxdepth: 1
:hidden:

opencode
```
