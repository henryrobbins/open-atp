#!/usr/bin/env bash

# $PROMPT is exported by the AgentProver before this script runs (it reads
# agent_prompt.txt from the workdir). The backend has already cd'd into the
# workdir and symlinked .lake to the warm Mathlib cache.
#
# Both XDG roots are workdir-local so muse's settings.json (which carries the
# lean-lsp MCP server) and its per-session event logs live under the workdir --
# isolating concurrent runs and syncing the session logs back out, which is where
# parse_result reads token usage from (the stdout stream reports none).
#
# https://dev.meta.ai/docs/muse-code/extending.md
#
# `muse exec` runs one prompt headlessly and streams JSONL events to stdout.
# --yolo disables muse's own approval prompts and OS sandbox and trusts the
# workspace; the docs prescribe it for "an already-isolated environment such as a
# disposable CI container", which is exactly this container, and workspace trust
# is also what loads the staged .agents/skills. --user-input-auto-resolve
# auto-cancels any question the agent asks rather than hanging on a prompt no one
# is there to answer. --no-foreign-personal-context keeps a run hermetic by
# ignoring personal rules/skills imported from other agent CLIs.
#
# MUSE_NO_AUTO_UPDATE stops the launcher from checking the release channel and
# forking a background self-update: in an ephemeral sandbox that does unwanted
# network work and can swap the binary mid-run. The image pins the version it
# baked in.

export XDG_CONFIG_HOME="$PWD/.muse-config"
export XDG_DATA_HOME="$PWD/.muse-data"
export MUSE_NO_AUTO_UPDATE=1
muse exec "$PROMPT" \
    --json \
    --yolo \
    --user-input-auto-resolve \
    --no-foreign-personal-context \
    --model '<<MODEL>>' \
    --reasoning-effort '<<EFFORT>>'
