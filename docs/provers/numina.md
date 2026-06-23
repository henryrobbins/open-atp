(prover-numina)=
# NuminaProver

The {class}`~open_afps.provers.numina.NuminaProver` is a configured variant of the
{class}`~open_afps.provers.agent_prover.AgentProver`. Structurally, Numina is
"Claude Code + a specific skills/prompts/search toolkit, run in a multi-round loop in
a sandbox", so rather than re-implement it, `open-afps` extends `AgentProver` pinned
to the `claude_code` harness with Numina's vendored assets and adds the two genuinely
different behaviours:

- a **round-continuation loop** — re-invoke the agent while it reports it hit a limit
  rather than completing; and
- the **statement tracker** — guard against the agent deleting or weakening the
  theorems it was asked to prove.

Numina's helper skills call out to Leandex / Gemini / GPT, so its config carries
those API keys
({attr}`~open_afps.provers.numina.NuminaProverConfig.helper_env_keys`) to forward
into the sandbox.

The `discussion_partner` skill (Gemini/GPT) appends a per-call token-usage record
to a workdir ledger (`.claude/helper_usage.jsonl`); after the run `prove()` prices
it via the {mod}`~open_afps.harness.cost` table and folds it into
`cost_usd`, so the reported cost includes discussion-partner spend rather than only
the Claude agent. The split is preserved in metadata (`agent_cost_usd`,
`helper_cost_usd`, `helper_breakdown`). Helper models absent from the price table
are billed at `0` but surfaced in `helper_unpriced_models` so the gap is visible —
the `gpt-5.4-pro` / `gemini-3.1-pro-preview` defaults carry **estimated** prices
that should be verified against the provider pricing pages.

:::{warning}
The `NuminaProver` is currently a **stub**:
{meth}`~open_afps.provers.numina.NuminaProver.prove` raises `NotImplementedError`.
The config surface below is stable, but the round loop and statement tracker are
still being implemented.
:::

## Configuration

```python
from open_afps.provers.numina import NuminaProverConfig
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN

config = NuminaProverConfig(
    image=DEFAULT_IMAGE,
    supported_toolchain=DEFAULT_TOOLCHAIN,
    max_rounds=20,
    guard_statements=True,
)
```

The `harness` is fixed to `claude_code` and `assets` to `numina`. See
{class}`~open_afps.provers.numina.NuminaProverConfig` in the {doc}`../api/provers`
reference for the full set of fields.
