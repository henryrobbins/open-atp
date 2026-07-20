#!/usr/bin/env bash

# $PROMPT is exported by the AgentProver before this script runs (it reads
# agent_prompt.txt from the workdir). The backend has already cd'd into the
# workdir and symlinked .lake to the warm Mathlib cache.
#
# `opencode run` runs non-interactively; opencode.json (written by the harness)
# configures the model provider and the lean-lsp MCP server. The JSON event
# stream goes to stdout.
#
# https://opencode.ai/docs/cli/#run-1
#
# With auth='login' the harness mounts a minimal opencode data dir at
# $HOME/.opencode-data, so opencode reads the credential from there instead
# of an API-key env var.
[ -d "$HOME/.opencode-data" ] && export XDG_DATA_HOME="$HOME/.opencode-data"

opencode run --dir /workspace/wd --format json \
    --model '<<PROVIDER>>/<<MODEL>>' \
    "$PROMPT"
