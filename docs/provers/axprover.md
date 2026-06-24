(prover-axprover)=
# AxProver

The AxProver prover is the {class}`~open_atp.provers.agent_prover.AgentProver` on the
{class}`~open_atp.harness.axprover.AxProverHarness` — it drives
[ax-prover-base](https://github.com/henryrobbins/ax-prover-base), a self-contained
LangGraph Lean agent with its own proposer → builder → reviewer → memory loop, via the
`ax-prover prove` CLI in a sandbox. ax-prover edits the target `.lean` file in place;
{class}`~open_atp.provers.agent_prover.AgentProver` supplies the staging, snapshot /
diff, key forwarding, and the shared {class}`~open_atp.verify.Verifier` remains
the source of truth for the compile / sorry / axiom check (we do not trust ax-prover's
own reviewer). See {doc}`index` for the lifecycle every agent harness shares.

## Usage

```python
from open_atp.backends.docker import DockerBackend, DockerConfig
from open_atp.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_atp.provers import AgentProver, AgentProverConfig

backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
config = AgentProverConfig(
    image=DEFAULT_IMAGE,
    supported_toolchain=DEFAULT_TOOLCHAIN,
    harness="axprover",
    model="claude-opus-4-8",
    effort="high",
    max_iterations=None,  # cap ax-prover's loop (its own default is 50)
)
prover = AgentProver(config, verification_backend=backend)
```

Or by registry spec through {func}`~open_atp.provers.get_prover` / the CLI:
`agent:axprover` (defaults: `claude-opus-4-8`, `effort="high"`). The
`max_iterations` field is AxProver-specific and caps ax-prover's
proposer → builder → reviewer loop.

## Harness details

Two things differ from the CLI harnesses:

- **Config lives in a workdir YAML, not flags.** `configure_wd` writes an
  `axprover.yaml` (emitted as JSON, which is valid YAML) selecting the model, effort,
  and optional `max_iterations`. ax-prover's `--config` *appends* to its bundled
  `default.yaml`, so only the deltas are set: the model is defined under a fresh
  `llm_configs.open_atp` key that `prover.prover_llm` references by interpolation
  (a fresh key avoids OmegaConf deep-merging stale `thinking` settings from the
  default's Claude config into ours).
- **The free-text prompt is ignored** — ax-prover ships its own prompts. The launch
  script (`assets/scripts/axprover_agent.sh`) self-discovers every `.lean` carrying a
  `sorry` (skipping the warm `.lake` cache) and proves each:

  ```bash
  ax-prover --config axprover.yaml prove "$f" \
      --folder . --skip-build --overwrite \
      -o "ax_output.<target>.json" 2>&1 | tee "ax_prover.<target>.log" || true
  ```

  `|| true` keeps one unprovable file from aborting the rest; the final Verifier pass
  is authoritative either way.

The model is mapped to ax-prover's LangChain `provider:model` form (e.g.
`anthropic:claude-opus-4-8`, `google_genai:...`), and `effort` maps to each provider's
reasoning knob. ax-prover's per-call LLM retries are capped at 3 (its default of
10 000 ignores non-retryable `400`s and can hang a run for hours).

## Authentication

The harness forwards one of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or
`GOOGLE_API_KEY` from the host (ax-prover reads the provider key from the process
env). At least one must be set or the harness raises.

## Cost tracking

ax-prover streams human-readable logs, not a JSON event stream, so cost is not on
stdout. `parse` sums `input_tokens` / `output_tokens` across the per-target
`ax_output.<target>.json` files written by the `-o` flag and leaves `cost_usd` `None`,
so the prover converts tokens → USD via {data}`~open_atp.harness.cost.COST_PER_MTOK`
(as with Codex). `collect_logs` relocates the `ax_output.*.json` and
`ax_prover.*.log` files to `logs/`. On an ax-prover build without the usage fields the
tokens are simply absent and the run reports zero cost.
