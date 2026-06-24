# Provers

A prover is a *candidate generator*: it takes a
{class}`~open_afps.core.task.ProofTask` and produces completed Lean files. The base
{class}`~open_afps.core.prover.AutomatedProver` owns the shared lifecycle — generate,
then verify in the sandbox — so every prover gets the same final check for free.

| Prover | Spec | Generation | Credential |
| --- | --- | --- | --- |
| [Claude Code](claude_code.md) | `agent` | AgentProver on the Claude Code CLI | `CLAUDE_CODE_OAUTH_TOKEN` |
| [Codex](codex.md) | `agent:codex` | AgentProver on the Codex CLI (OpenAI) | `~/.codex` (OAuth) |
| [OpenCode](opencode.md) | `agent:opencode` | AgentProver on the OpenCode CLI (any provider) | `<PROVIDER>_API_KEY` |
| [AxProver](axprover.md) | `agent:axprover` | AgentProver driving ax-prover-base | `ANTHROPIC` / `OPENAI` / `GOOGLE_API_KEY` |
| [Vibe / Leanstral](vibe.md) | `vibe` | AgentProver on Mistral Vibe's `lean` agent | `MISTRAL_API_KEY` |
| [NuminaProver](numina.md) | `numina` | AgentProver (Claude) + Numina assets + round loop | harness + helper API keys |
| [AristotleProver](aristotle.md) | `aristotle` | Harmonic's hosted Aristotle API | `ARISTOTLE_API_KEY` |

The Claude Code, Codex, OpenCode, AxProver, and Vibe provers are all the same
{class}`~open_afps.provers.agent_prover.AgentProver` composed with a different
{class}`~open_afps.harness.base.Harness`; `Spec` is the
{func}`~open_afps.api.build_prover` registry name. Every prover subclasses
{class}`~open_afps.core.prover.AutomatedProver` and funnels its output through the
shared {class}`~open_afps.core.verifier.Verifier`.

## How the agent provers work

An {class}`~open_afps.provers.agent_prover.AgentProver` composes two concerns:

- a {class}`~open_afps.harness.base.Harness` — the *agent* concern: launch script,
  credential forwarding, and output parsing (one per harness page below); and
- a {class}`~open_afps.backends.base.ComputeBackend` — the *compute* concern: where
  the agent runs, with Lean+Mathlib and the
  [lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp) server.

`prove` stages the project into the workdir, lets the agent fill the `sorry`s in
place, then diffs the `.lean` files against the staged originals to report what
changed. The shared {class}`~open_afps.core.verifier.Verifier` does the final
compile / sorry / axiom check. Configuration fields are documented under
{class}`~open_afps.provers.agent_prover.AgentProverConfig` in the
{doc}`../api/provers` reference.

```{toctree}
:maxdepth: 1

claude_code
codex
opencode
axprover
vibe
numina
aristotle
```
