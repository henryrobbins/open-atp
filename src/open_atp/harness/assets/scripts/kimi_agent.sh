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

export KIMI_CODE_HOME="$PWD/.kimi-home"
kimi -p "$PROMPT" \
    --model '<<MODEL>>' \
    --output-format stream-json
