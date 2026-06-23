# Agent Harnesses

The {class}`~open_afps.provers.agent_prover.AgentProver` (and, by extension, the
{doc}`NuminaProver <../provers/numina>`) supports three agent harnesses. All three
implement the {class}`~open_afps.harness.base.Harness` interface and are
registered in {data}`~open_afps.harness.HARNESSES`.

| Harness | Models | Billing | Credential |
| --- | --- | --- | --- |
| [Claude Code](claude_code.md) | Anthropic Claude | Claude subscription | `CLAUDE_CODE_OAUTH_TOKEN` (OAuth) |
| [Codex](codex.md) | OpenAI GPT | ChatGPT subscription | `~/.codex` (OAuth) |
| [OpenCode](opencode.md) | **All** providers | Provider API | `<PROVIDER>_API_KEY` |

Each harness page covers:

1. **Choosing a plan** (or API account) — what to sign up for and where to track
   usage.
2. **Authentication** — the one-time setup step that lets `open-afps` invoke the
   agent.
3. **Using the harness** — selecting it via `AgentProverConfig`.
4. **Cost tracking** — how a run's cost is determined.

A harness is selected by name through
{class}`~open_afps.provers.agent_prover.AgentProverConfig`'s `harness` field (one of
`claude_code`, `codex`, `opencode`). The harness is the *agent* concern — its launch
script, credential forwarding, and output parsing — while the injected
{class}`~open_afps.backends.base.ComputeBackend` is the *compute* concern (see
{doc}`../provers/agent`).

```{toctree}
:maxdepth: 1
:hidden:

claude_code
codex
opencode
```
