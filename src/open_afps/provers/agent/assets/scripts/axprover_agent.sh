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
#   * --config is a TOP-LEVEL flag and MUST precede the `prove` subcommand. It
#     layers axprover.yaml (written by AxProverHarness) over ax-prover's bundled
#     default.yaml, which the CLI auto-prepends.
#   * --skip-build reuses the warm Mathlib .lake instead of re-running lake build
#     (the same assumption the other harnesses make).
#   * --overwrite re-proves even files ax-prover thinks are already done.
#   * `|| true` keeps one unprovable file from aborting the rest; the final
#     Verifier pass is the source of truth either way.
#   * AX_PROVER_USAGE_FILE is forward-compat: once the upstream usage patch lands
#     (AX_PROVER_HARNESS_PLAN.md step 3), ax-prover writes per-target token totals
#     there and AxProverHarness.parse sums every ax_usage.*.json after the run.
#
# https://github.com/Axiomatic-AI/ax-prover-base

while IFS= read -r f; do
  [ -z "$f" ] && continue
  # Strip the leading ./ that `grep -rl . ` prepends: ax-prover derives a module
  # path via file_path.replace("/", ".") and a leading ./ corrupts it (-> ..Module),
  # which then resolves to a bogus file and hides every sorry ("No unproven found").
  f="${f#./}"
  safe=$(printf '%s' "$f" | tr '/.' '__')
  export AX_PROVER_USAGE_FILE="ax_usage.${safe}.json"
  ax-prover --config axprover.yaml prove "$f" \
    --folder . \
    --skip-build \
    --overwrite \
    -o "ax_output.${safe}.json" || true
done < <(grep -rl --include='*.lean' '\bsorry\b' . | grep -v '/\.lake/' || true)
