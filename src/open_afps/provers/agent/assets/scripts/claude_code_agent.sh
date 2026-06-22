#!/usr/bin/env bash

# $PROMPT is exported by the AgentProver before this script runs (it reads
# agent_prompt.txt from the workdir). The backend has already cd'd into the
# workdir and symlinked .lake to the warm Mathlib cache.
#
# bypassPermissions skips all permission prompts (safe in the container);
# IS_SANDBOX=1 (set by the prover) lets that mode run non-interactively.
# .mcp.json registers the lean-lsp MCP server; --strict-mcp-config restricts
# the agent to exactly those servers. The stream-json event stream goes to stdout.
#
# https://code.claude.com/docs/en/cli-reference
# https://code.claude.com/docs/en/mcp#project-scope

claude -p "$PROMPT" \
    --output-format stream-json --verbose \
    --permission-mode bypassPermissions \
    --mcp-config .mcp.json --strict-mcp-config \
    --model '<<MODEL>>' --effort '<<EFFORT>>'
