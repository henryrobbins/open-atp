# Harnesses

An agent prover ({class}`~open_atp.provers.agent_prover.AgentProver`) forwards the proof task to a coding-agent **harness** ({class}`~open_atp.harness.base.Harness`) — the CLI that runs the model, staged into the sandbox with Lean skills and MCP tooling. The harness is independent of the model it drives: the same OpenCode harness fronts DeepSeek, Anthropic, OpenAI, or Google models. Provers on this project's {doc}`/provers/index` list pin a harness to a specific model.

```{toctree}
:maxdepth: 1

opencode
```
