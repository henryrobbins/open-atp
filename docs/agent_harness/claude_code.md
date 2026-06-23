(harness-claude-code)=
# Claude Code

The Claude Code harness runs Anthropic's
[Claude Code](https://claude.com/claude-code) CLI inside the `open-afps` sandbox,
billing usage against your Claude subscription via a long-lived OAuth token.

## Choosing a plan

Claude Code is included with every paid Claude plan. Compare plans at
[Choose a Claude plan](https://support.claude.com/en/articles/11049762-choose-a-claude-plan),
then monitor consumption at [Usage](https://claude.ai/settings/usage).

## Authenticating

Generate a long-lived OAuth token following
[these instructions](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token):

```bash
claude setup-token
```

Save the printed token to a `.env` file in your project:

```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
```

`open-afps` forwards this token into the sandbox at run time. The token bills against
your Claude plan rather than the API.

## Using the harness

```python
from open_afps.provers import AgentProverConfig
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN

config = AgentProverConfig(
    image=DEFAULT_IMAGE,
    supported_toolchain=DEFAULT_TOOLCHAIN,
    harness="claude_code",
    model="claude-opus-4-8",
    effort="high",
)
```

## Cost tracking

The Claude Code CLI JSON output stream reports per-run USD directly. This populates
`cost_usd` in {class}`~open_afps.harness.base.HarnessRunResult`.
