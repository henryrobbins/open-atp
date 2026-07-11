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
# The xai (Grok) provider authenticates from opencode's credential store instead of
# an env key: the harness mounts a minimal data dir (just the xai auth entry) at
# $HOME/.opencode-data, so point XDG_DATA_HOME there when that mount is present.
[ -d "$HOME/.opencode-data" ] && export XDG_DATA_HOME="$HOME/.opencode-data"

opencode run --dir /workspace/wd --format json \
    --model '<<PROVIDER>>/<<MODEL>>' \
    "$PROMPT"
