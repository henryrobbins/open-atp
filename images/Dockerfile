# Base sandbox image for open-atp: Lean + Mathlib, with the olean cache pre-baked.
#
# Mirrors milp_flare's assets/docker/Dockerfile, minus the MILP-specific Lean
# skeleton. As of phase 3 it also carries the agent CLIs (claude-code / codex /
# opencode) and the lean-lsp-mcp MCP server so the AgentProver can drive an agent
# in the sandbox -- the same image still serves as the verification image.
#
# This Dockerfile backs the *Docker* compute backend (runs as the non-root `agent`
# user). The *Modal* backend uses a separate, programmatically-built image that
# installs the same toolchain globally as root -- see `open-atp build-modal-image`
# (src/open_atp/__main__.py). Keep the two recipes in sync when bumping versions.
#
# The toolchain pinned in images/lean/lean-toolchain is the contract: the verifier
# rejects any uploaded project whose lean-toolchain differs. Bump it (and the mathlib
# rev in images/lean/lakefile.toml) and rebuild to support a new pin.
#
# Build context is images/ :  docker build -t open-atp:latest images/

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# ripgrep is recommended for lean-lsp-mcp's local search; pipx installs the MCP
# server and uv.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        unzip \
        build-essential \
        python3 \
        python3-pip \
        pipx \
        ripgrep \
        procps \
    && rm -rf /var/lib/apt/lists/*

# Node 20 + agent CLIs (Claude Code, Codex, OpenCode). Installed as root, before
# we drop to the non-root user, so they land on the global PATH.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code @openai/codex opencode-ai \
 && rm -rf /var/lib/apt/lists/*

# Non-root user (matches milp_flare; Lean's elan lives under this user's HOME and
# Claude Code refuses to run as root).
RUN useradd -m -s /bin/bash agent
USER agent
WORKDIR /home/agent

# elan + Lean toolchain. `--default-toolchain none` lets the lean-toolchain file
# we copy below pin the version on the first `lake` invocation.
RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf \
        | sh -s -- -y --default-toolchain none --no-modify-path
ENV PATH="/home/agent/.elan/bin:/home/agent/.local/bin:${PATH}"

# lean-lsp MCP server (invoked directly as `lean-lsp-mcp` by the harnesses) + uv.
# https://github.com/oOo0oOo/lean-lsp-mcp
# mistral-vibe provides the `vibe` CLI the VibeHarness drives (Leanstral's `lean`
# agent). https://github.com/mistralai/mistral-vibe -- pinned so the builtin `lean`
# agent's model pin (Leanstral 1.5) stays stable across rebuilds; keep in sync with
# the Modal image in src/open_atp/__main__.py.
RUN pipx install lean-lsp-mcp \
 && pipx install uv \
 && pipx install mistral-vibe==2.19.0

# ax-prover (LangGraph Lean agent) backing the AxProverHarness. pipx keeps its heavy
# stack (langgraph, langchain-*, lean-interact, omegaconf, tavily) fully isolated from
# open-atp and the other CLIs. Pinned to a commit on our fork
# (henryrobbins/ax-prover-base) rather than the 0.1.1 PyPI release, for two fixes the
# release lacks: (1) lean_interact-based target discovery -- 0.1.1's regex discovery
# lists `import Mathlib` as a theorem `Mathlib` and flags it unproven when a nearby
# docstring mentions the word `sorry`, burning a prove loop on a phantom target; and
# (2) per-target token usage in the `-o` JSON that AxProverHarness.parse reads for cost.
# Keep AX_PROVER_REF in sync with src/open_atp/__main__.py. Fork is public; the HTTPS
# clone needs no credentials.
ARG AX_PROVER_REF=361e5b3451267785bfd70f173e7ab0be667d4987
RUN pipx install "git+https://github.com/henryrobbins/ax-prover-base@${AX_PROVER_REF}"

WORKDIR /workspace

# Bake the lake project skeleton: a bare "require mathlib" project. lake update
# resolves the manifest and clones mathlib; cache get downloads its oleans so any
# uploaded project on the same pin compiles against a warm cache.
COPY --chown=agent:agent lean/ /workspace/
RUN lake update && lake exe cache get

# Verification workdirs are bind-mounted to /workspace/wd; the DockerBackend
# symlinks /workspace/wd/.lake -> /workspace/.lake so they reuse this warm cache.

# Numina's helper skills (vendor/numina/skills/cli/*.py) declare their third-party
# deps via PEP 723 inline metadata and are invoked with `uv run`. Pre-warm the uv
# cache with the union of those deps so the first `uv run` in the sandbox resolves
# from cache instead of cold-downloading mid-proof. Placed after the Mathlib cache
# layer so editing it never invalidates that expensive build step.
RUN printf '%s\n' \
        '# /// script' \
        '# requires-python = ">=3.11"' \
        '# dependencies = ["requests", "google-genai", "openai", "anthropic"]' \
        '# ///' \
        'print("uv skill cache warmed")' > /tmp/warm_skills.py \
 && uv run --no-project /tmp/warm_skills.py \
 && rm /tmp/warm_skills.py

CMD ["bash"]
