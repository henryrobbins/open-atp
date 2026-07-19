#!/usr/bin/env bash

# $PROMPT is exported by the AgentProver before this script runs (it reads
# agent_prompt.txt from the workdir). The backend has already cd'd into the
# workdir and symlinked .lake to the warm Mathlib cache.
#
# KIMI_CODE_HOME is workdir-local so kimi's OAuth credential, provider config,
# the user-scope mcp.json (lean-lsp), the user-scope skills dir, and the
# per-session wire log all live under the workdir -- isolating concurrent runs
# and syncing the telemetry back out. `kimi -p` runs non-interactively and
# auto-approves tool calls (--yolo is rejected in prompt mode); the stream-json
# event stream goes to stdout.
#
# https://moonshotai.github.io/kimi-code/
#
# KIMI_DISABLE_CRON / KIMI_CODE_NO_AUTO_UPDATE stop kimi from spawning its
# background cron daemon and auto-updater: in an ephemeral sandbox those do
# unwanted network/CPU work and can self-modify the CLI mid-run, and a lingering
# child destabilizes a short-lived Modal sandbox (it outlives the `-p` exit).

export KIMI_CODE_HOME="$PWD/.kimi-home"
export KIMI_DISABLE_CRON=1
export KIMI_CODE_NO_AUTO_UPDATE=1
kimi -p "$PROMPT" \
    --model '<<MODEL>>' \
    --output-format stream-json
