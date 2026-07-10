#!/usr/bin/env bash

# $PROMPT is exported by the AgentProver before this script runs (it reads
# agent_prompt.txt from the workdir). The backend has already cd'd into the
# workdir and symlinked .lake to the warm Mathlib cache.
#
# We drive grok over ACP (`grok agent stdio`, JSON-RPC 2.0) via grok_acp.py rather
# than `grok --single`: the single-shot mode emits only one terminal JSON object, so
# a run's tool calls / usage never reach stdout, whereas ACP streams them as
# session/update events. The Python driver re-emits that stream as JSONL on stdout,
# which the backend captures as the transcript. The lean-lsp MCP server is registered
# via the project-scope .grok/config.toml the harness writes.
#
# GROK_HOME points at the mounted credential dir (the harness stages just auth.json
# there); grok reads the OAuth login from it and writes its own state alongside, so
# the image's ~/.grok binary is never shadowed. It sits under $HOME, which the backend
# sets equal to the mount root (container_home). GROK_DISABLE_AUTOUPDATER suppresses
# the background update check (there is no --no-auto-update flag on `grok agent`).
#
# https://docs.x.ai/build/cli/headless-scripting#acp

export GROK_HOME="$HOME/.grok-home"
export GROK_DISABLE_AUTOUPDATER=1
export GROK_MODEL='<<MODEL>>'
export GROK_EFFORT='<<EFFORT>>'

# grok validates --model against a model list it fetches from xAI at startup. On a
# cold sandbox that fetch can transiently fail (network not yet ready, or throttled
# under concurrent starts), returning an empty list so --model fails with "unknown
# model id" and aborts the whole run. Gate the (expensive) proof call on a warm list
# via the cheap `grok models` probe. Note it exits 0 even on a failed fetch (printing
# an empty list), so gate on the target model actually appearing -- not the exit
# code -- retrying until the fetch lands.
for _ in $(seq 1 10); do
    grok models 2>/dev/null | grep -qF '<<MODEL>>' && break
    sleep 3
done

exec python3 grok_acp.py
