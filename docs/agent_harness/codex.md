(harness-codex)=
# Codex

The Codex harness runs OpenAI's [Codex](https://chatgpt.com/codex) CLI inside the
`open-afps` sandbox, billing usage against your ChatGPT subscription via OAuth
credentials stored in `~/.codex`.

## Choosing a plan

Codex is available on paid ChatGPT plans. Compare plans at
[ChatGPT pricing](https://chatgpt.com/pricing/), then monitor consumption at
[Analytics](https://chatgpt.com/codex/cloud/settings/analytics).

## Authenticating

Authenticate the Codex CLI on the host following
[these instructions](https://developers.openai.com/codex/auth/ci-cd-auth):

```bash
codex login
```

This writes credentials to `~/.codex`. `open-afps` exposes that directory inside the
sandbox at run time so Codex can refresh its access token mid-session.

## Using the harness

```python
from open_afps.provers import AgentProverConfig
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN

config = AgentProverConfig(
    image=DEFAULT_IMAGE,
    supported_toolchain=DEFAULT_TOOLCHAIN,
    harness="codex",
    model="gpt-5.4",
    effort="high",
)
```

## Cost tracking

The Codex CLI does not report per-run USD, so the harness estimates it from token
totals using the pricing table in
{data}`~open_afps.harness.cost.COST_PER_MTOK`, populating `cost_usd` in
{class}`~open_afps.harness.base.HarnessRunResult`. Ensure the pricing table
reflects current OpenAI API prices.
