# Base sandbox image for open-afps: Lean + Mathlib, with the olean cache pre-baked.
#
# Mirrors milp_flare's assets/docker/Dockerfile, minus the MILP-specific Lean
# skeleton. As of phase 3 it also carries the agent CLIs (claude-code / codex /
# opencode) and the lean-lsp-mcp MCP server so the AgentProver can drive an agent
# in the sandbox -- the same image still serves as the verification image.
#
# One image, two compute backends. The Lean toolchain (elan) and the pipx tools are
# installed *globally* (ELAN_HOME=/opt/elan, pipx bins on /usr/local/bin) so both
# runtime users find `lake`/`lean`/`uvx` on PATH:
#   * Docker runs as the non-root `agent` user (Claude Code refuses to run as root
#     unless IS_SANDBOX=1), and
#   * Modal ignores the image USER and runs everything as root.
# That single recipe is what lets `modal.Image.from_dockerfile("images/Dockerfile")`
# reuse this Dockerfile for the Modal backend (see `open-afps build-modal-image`).
#
# The toolchain pinned in images/lean/lean-toolchain is the contract: the verifier
# rejects any uploaded project whose lean-toolchain differs. Bump it (and the mathlib
# rev in images/lean/lakefile.toml) and rebuild to support a new pin.
#
# Build context is images/ :  docker build -t open-afps:latest images/

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# ripgrep is recommended for lean-lsp-mcp's local search; pipx installs the MCP
# server and uv (the agents invoke `uvx lean-lsp-mcp`).
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

# Node 20 + agent CLIs (Claude Code, Codex, OpenCode). Installed globally as root
# (before the `agent` user exists) so they land on the global PATH for both users.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code @openai/codex opencode-ai \
 && rm -rf /var/lib/apt/lists/*

# elan + Lean toolchain in a *global* ELAN_HOME (not a user HOME) so `lake`/`lean`
# resolve on PATH for root (Modal) and the agent user (Docker) alike. The pinned
# toolchain installs lazily on the first `lake` call (lake update, below).
# `--default-toolchain none` lets images/lean/lean-toolchain pin the version.
ENV ELAN_HOME=/opt/elan
ENV PATH="/opt/elan/bin:/usr/local/bin:${PATH}"
RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf \
        | sh -s -- -y --default-toolchain none --no-modify-path

# lean-lsp MCP server (matches the `uvx lean-lsp-mcp` invocation the harnesses
# render) + uv, installed to global dirs so their entrypoints land on /usr/local/bin
# for every user. https://github.com/oOo0oOo/lean-lsp-mcp
ENV PIPX_HOME=/opt/pipx
ENV PIPX_BIN_DIR=/usr/local/bin
RUN pipx install lean-lsp-mcp \
 && pipx install uv

# Shared, world-writable uv cache so both root (Modal) and agent (Docker) resolve
# the Numina skills' `uv run` deps from the same warmed cache at runtime.
ENV UV_CACHE_DIR=/opt/uv-cache
RUN mkdir -p /opt/uv-cache && chmod 0777 /opt/uv-cache

# The non-root user the Docker backend runs as (Claude Code refuses root without
# IS_SANDBOX=1). Created after the global installs so it inherits them via PATH.
RUN useradd -m -s /bin/bash agent

WORKDIR /workspace

# Bake the lake project skeleton: a bare "require mathlib" project. lake update
# resolves the manifest and clones mathlib (installing the pinned toolchain into
# /opt/elan on this first `lake` call); cache get downloads its oleans so any
# uploaded project on the same pin compiles against a warm cache.
COPY lean/ /workspace/
RUN lake update && lake exe cache get

# Verification/agent workdirs are synced to /workspace/wd (bind-mounted by the
# Docker backend, pushed by the Modal backend); both symlink /workspace/wd/.lake ->
# /workspace/.lake to reuse this warm cache. Hand /workspace to `agent` so the Docker
# backend (running as agent) can write oleans into the cache at runtime; root (Modal)
# ignores ownership.
RUN chown -R agent:agent /workspace

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

# Docker runs as `agent`; Modal ignores USER and runs as root. The build steps above
# ran as root (global installs); set the runtime user last for the Docker backend.
USER agent

CMD ["bash"]
