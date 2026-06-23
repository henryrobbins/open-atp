#!/usr/bin/env bash
set -euo pipefail

# $PROMPT is exported by the AgentProver before this script runs but is unused:
# ax-prover ships its own prompts. The backend has already cd'd into the workdir
# and symlinked .lake to the warm Mathlib cache.
#
# The Harness contract is project-wide, while ax-prover needs a target, so we
# self-discover every .lean carrying a `sorry` (skipping the warm .lake cache)
# and prove each in turn. No change to configure_wd's signature is needed.
#
# Notes:
#   * --config is a TOP-LEVEL flag and MUST precede the `prove` subcommand. The
#     CLI's --config is argparse action="append" with default ["default.yaml"], so
#     passing axprover.yaml *appends* to (does not replace) the bundled default.yaml:
#     the effective merge is [default.yaml, axprover.yaml]. AxProverHarness relies on
#     this (it overrides only deltas) and works around the deep-merge it implies --
#     see AxProverHarness._render_config.
#   * --skip-build reuses the warm Mathlib .lake instead of re-running lake build
#     (the same assumption the other harnesses make).
#   * --overwrite re-proves even files ax-prover thinks are already done.
#   * `|| true` keeps one unprovable file from aborting the rest; the final
#     Verifier pass is the source of truth either way.
#   * -o writes ax_output.<target>.json: per-target {success, error, summary,
#     input_tokens, output_tokens, ...}. AxProverHarness.parse sums the token fields
#     across these files for cost (the pinned fork commit adds the usage fields).
#   * ax-prover logs are human-readable (not the JSONL the parsers consume), so we
#     tee each run's stdout+stderr to ax_prover.<target>.log. The file lands in the
#     workdir and is pulled back with it (Modal) / lives on the bind mount (Docker),
#     so the logs survive even when the harness discards the stream. PYTHONUNBUFFERED
#     defeats CPython's block-buffering of a piped stdout so the tee is line-fresh.
#
# https://github.com/henryrobbins/ax-prover-base

export PYTHONUNBUFFERED=1

while IFS= read -r f; do
  [ -z "$f" ] && continue
  # Strip the leading ./ that `grep -rl . ` prepends: ax-prover derives a module
  # path via file_path.replace("/", ".") and a leading ./ corrupts it (-> ..Module),
  # which then resolves to a bogus file and hides every sorry ("No unproven found").
  f="${f#./}"
  safe=$(printf '%s' "$f" | tr '/.' '__')
  # `|| true` is on the pipeline: with `set -o pipefail` a failing ax-prover still
  # lets the loop continue, and tee captures the full log either way.
  ax-prover --config axprover.yaml prove "$f" \
    --folder . \
    --skip-build \
    --overwrite \
    -o "ax_output.${safe}.json" 2>&1 | tee "ax_prover.${safe}.log" || true
done < <(grep -rl --include='*.lean' '\bsorry\b' . | grep -v '/\.lake/' || true)
