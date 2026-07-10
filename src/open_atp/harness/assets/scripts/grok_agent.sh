#!/usr/bin/env bash

# $PROMPT is exported by the AgentProver before this script runs (it reads
# agent_prompt.txt from the workdir). The backend has already cd'd into the
# workdir and symlinked .lake to the warm Mathlib cache.
#
# `grok --single` (aka -p) runs one prompt non-interactively and exits.
# --always-approve auto-approves tool executions (safe in the container);
# --no-auto-update suppresses the background update check in automation. The
# lean-lsp MCP server is registered via the project-scope .grok/config.toml the
# harness writes. The JSON result goes to stdout.
#
# https://docs.x.ai/build/cli/headless-scripting

grok --single "$PROMPT" \
    --output-format json \
    --model '<<MODEL>>' \
    --always-approve \
    --no-auto-update
