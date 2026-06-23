(harness-opencode)=
# OpenCode

The [OpenCode](https://opencode.ai/) harness runs the OpenCode CLI inside the
`open-afps` sandbox. Unlike Claude Code and Codex, OpenCode bills directly against an
API provider (Anthropic, OpenAI, Google, or DeepSeek) rather than a flat-rate
subscription.

## Choosing an API provider

OpenCode supports multiple providers. Sign up for an API account with your chosen
provider, fund it, and monitor consumption from the provider's usage dashboard. See
[OpenCode providers](https://opencode.ai/docs/providers/) for the full list.

## Authenticating

OpenCode reads provider API keys from the environment. Export the key matching your
chosen provider, for example:

```bash
export DEEPSEEK_API_KEY=...
```

Other supported keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`.
`open-afps` forwards the matching key into the sandbox at run time.

## Using the harness

```python
from open_afps.provers import AgentProverConfig
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN

config = AgentProverConfig(
    image=DEFAULT_IMAGE,
    supported_toolchain=DEFAULT_TOOLCHAIN,
    harness="opencode",
    model="deepseek-chat",
    effort="medium",
)
```

## Cost tracking

The OpenCode CLI reports a per-step cost for each provider call; the harness sums
these into `cost_usd` in
{class}`~open_afps.harness.base.HarnessRunResult`.
