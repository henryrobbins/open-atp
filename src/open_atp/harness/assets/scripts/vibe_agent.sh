#!/usr/bin/env bash

# $PROMPT is exported by the AgentProver before this script runs (it reads
# agent_prompt.txt from the workdir). The backend has already cd'd into the
# workdir and symlinked .lake to the warm Mathlib cache.
#
# Mistral Vibe's `lean` agent IS Leanstral: `--agent lean` pins active_model to
# leanstral (no --model flag exists; the agent profile fixes the model). `-p`
# runs non-interactively and auto-approves all tools; the --output streaming
# event stream (newline-delimited JSON, one message per line) goes to stdout.
#
# VIBE_HOME is pinned under the workdir so vibe's config (which un-gates the
# builtin `lean` agent via installed_agents) and the per-session log -- the only
# place vibe records cost/tokens -- are sandbox-local and sync back out with the
# workdir for cost parsing.
#
# --trust trusts the workdir for this invocation (the documented flag for
# non-interactive automation). Without it vibe treats the workdir's `.vibe/` as
# an untrusted project-config folder and ignores it -- "/workspace/wd is not
# trusted; project configuration (.vibe/) will be ignored" -- silently dropping
# our config.toml (mcp_servers, bypass_tool_permissions, installed_agents).
#
# https://docs.mistral.ai/mistral-vibe/
export VIBE_HOME="$PWD/.vibe"

vibe -p "$PROMPT" \
    --agent <<AGENT>> \
    --output streaming \
    --trust \
    --workdir "$PWD"<<EXTRA>>
