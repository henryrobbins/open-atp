# Provers

An automated theorem prover takes a formal statement in [Lean](https://lean-lang.org/) and attempts to fill all `sorry` uses. OpenATP supports many theorem-provers ranging from general purpose coding agents to specialized theorem-proving agents.

```{include} _table.md
:parser: myst
```

Each prover is implemented as a subclass of {class}`~open_atp.provers.base.AutomatedProver`. Most are agent provers ({class}`~open_atp.provers.agent_prover.AgentProver`) that forward the task to a coding-agent harness ({class}`~open_atp.harness.base.Harness`); the **Harness** column names it. The harness is a separate axis from the model — its skills and MCP tooling are described on the {doc}`/harnesses/index` page.

```{toctree}
:maxdepth: 1
:hidden:

claude_code
codex
deepseek
axproverbase
leanstral
numina
aristotle
```
