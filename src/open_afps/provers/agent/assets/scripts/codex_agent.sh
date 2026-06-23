#!/usr/bin/env bash

# $PROMPT is exported by the AgentProver before this script runs (it reads
# agent_prompt.txt from the workdir). The backend has already cd'd into the
# workdir and symlinked .lake to the warm Mathlib cache.
#
# `codex exec` runs non-interactively; danger-full-access gives broad permissions
# (safe in the container). Codex doesn't auto-discover .mcp.json, so the lean-lsp
# MCP server is registered via -c overrides. The --json event stream goes to stdout.
#
# https://developers.openai.com/codex/cli/reference
# https://developers.openai.com/codex/config-advanced#one-off-overrides-from-the-cli

codex exec --json --skip-git-repo-check \
    --sandbox danger-full-access \
    --model '<<MODEL>>' \
    -c 'mcp_servers.lean-lsp.command="lean-lsp-mcp"' \
    -c 'mcp_servers.lean-lsp.args=[]' \
    -c 'model_reasoning_effort="<<EFFORT>>"' \
    "$PROMPT"
