# Phase 3 handoff: AgentProver

Implement `AgentProver` — a coding agent (Claude Code / Codex / OpenCode) driving
`lean-lsp-mcp` inside the sandbox to fill a project's `sorry`s. This is a port of
milp_flare's `harness/` package onto the abstractions phases 1–2 already built.

Read this whole doc first, then `src/open_afps/provers/agent.py` (the stub you're
filling) and the milp_flare sources it points at.

## Context: what already exists (don't rebuild it)

- **`ComputeBackend`** (`src/open_afps/backends/`): `DockerBackend.start(workdir, command, env)` runs a shell command in the sandbox over a bind-mounted workdir and streams stdout (`CommandHandle.stream()` / `.wait()`). It already `cd`s into the mount and symlinks `.lake` → the image's warm Mathlib cache. **Reuse this to launch the agent** — do not write new subprocess/Docker code.
- **`Verifier`** + **`AutomatedProver.run()`** (`core/`): `run()` calls your `prove()`, then verifies the workdir in Docker. **You only implement `prove()`.** It must leave `workdir` containing the full completed project and return a `GenerationOutput(completed_files, cost_usd, logs, metadata)`.
- **`AgentProver` / `AgentProverConfig`** (`provers/agent.py`): stub with the right shape. `AgentProverConfig` already has `harness`, `model`, `effort`, `assets`, `extra_env`. The constructor already separates `agent_backend` (generation) from the verification backend.
- **Pattern to copy**: `provers/aristotle.py` shows the full `prove()` shape — stage project into workdir, do the work, diff `.lean` files vs the original to build `completed_files`, return `GenerationOutput`. Phase 2's tests show how to gate a credentialed integration test (`aristotle_api` marker + `.env`).

## Sources to port (read-only, under `refs/milp_flare/src/milp_flare/`)

| milp_flare file | What to take |
|---|---|
| `harness/base.py` | `Harness` ABC shape: `configure_wd`, `auth_spec`, `_agent_command`, `_parse_lines`, `collect`. `HarnessRunResult` (tokens, cost, raw). |
| `harness/claude_code.py` | Claude Code harness: launch flags, `.mcp.json`, stream-json parsing, `CLAUDE_CODE_OAUTH_TOKEN`, `IS_SANDBOX=1`. |
| `harness/codex.py` | Codex harness: MCP via `-c` overrides, `~/.codex` cred dir mount, `--json` parsing. |
| `harness/opencode.py` | OpenCode harness: `opencode.json`, provider API keys, `run --format json`. |
| `harness/cost.py` | `COST_PER_MTOK` table + `compute_cost_usd(model, in, out)`. Port near-verbatim. |
| `assets/scripts/*.sh` | Per-harness launch scripts (`claude_code_agent.sh`, etc.). Adapt. |
| `assets/configs/mcp.json` | `uvx lean-lsp-mcp` MCP server config. |
| `assets/skills/` | Skill bundle layout. **Drop the MILP-specific skills**; write a generic "fill the sorrys" skill/prompt. |
| `harness/runner/{docker,modal}.py` | Already ported to `backends/`. **Skip** — only reference for `AuthSpec` (env + home-dir forwarding) which you'll re-add. |

Note milp_flare's `entrypoint.sh` does post-hoc compilation; **we don't need that** — our shared `Verifier` already does the final check. The agent just needs to produce files.

## Work items

### 1. Image: add agent CLIs + lean-lsp-mcp (`images/Dockerfile`)
The phase-1 image is verification-only. Add (mirror milp_flare's Dockerfile lines, which were intentionally dropped in phase 1):
- Node 20 + `npm i -g @anthropic-ai/claude-code @openai/codex opencode-ai`.
- `pipx install lean-lsp-mcp` and `pipx install uv` (ripgrep is already installed).
- Keep the non-root `agent` user (Claude Code refuses root).
Rebuild: `docker build -t open-afps:latest images/`. **This is the long pole** (~10–15 min) — kick it off first, in the background. Watch Docker VM disk (phase 1 hit a "no space left" failure; `docker builder prune -af` reclaims inactive cache safely).

### 2. Per-run auth forwarding (`backends/`)
Agents need credentials the verifier never did. milp_flare modeled this as an `AuthSpec(env: list[str], home_dirs: list[(host_dir, dest)])`. Add the minimal equivalent:
- `ComputeBackend.start` already takes `env: Mapping[str,str]` — use it for token/API-key vars.
- Add an optional per-call `mounts: list[tuple[str,str]]` (host→container) to `start`, or accept them on the config, so Codex's `~/.codex` dir can be mounted read-write. Thread into `DockerBackend._build_cmd` as `-v` args. Keep it small.

### 3. Harness layer (`provers/agent/` — promote the module to a package)
Port `Harness` as an internal helper used by `AgentProver`:
- `Harness.configure_wd(workdir)`: copy the harness's launch script + MCP config + the generic skills into the workdir, and write the prompt file. Vendor assets under `src/open_afps/provers/agent/assets/` (package data — add to `[tool.hatch.build]` includes).
- `Harness._agent_command()`: render the bash command (model/effort templated).
- `Harness._parse_lines(lines)`: parse the harness's streamed JSON for input/output tokens and (when reported) cost.
- `Harness.auth_spec()`: which env vars / cred dirs to forward.
- Registry `HARNESSES = {"claude_code": ..., "codex": ..., "opencode": ...}` selected by `config.harness`.
- Port `cost.py` as `provers/agent/cost.py`; use it as the fallback when the harness doesn't self-report cost (Codex).

### 4. `AgentProver.prove(task, workdir)`
1. `shutil.copytree(task.project.root, workdir, ignore=.lake/.git)` (as Aristotle does).
2. Snapshot original `.lean` contents for the later diff.
3. `harness = HARNESSES[self.config.harness](model, effort)`; `harness.configure_wd(workdir)` with a generic prompt: *"Complete every `sorry`; make the project compile and be sorry-free without new axioms; do not weaken or delete the stated theorems. Use the lean-lsp-mcp tools to check your work."*
4. `handle = self.agent_backend.start(workdir, harness.command, env=auth_env)`; drain `handle.stream()` (collect lines for parsing + logs), then `handle.wait()`.
5. `parsed = harness.parse(lines)` → tokens; `cost = harness.cost or compute_cost_usd(model, tokens...)`.
6. Diff workdir `.lean` vs snapshot → `completed_files`.
7. Return `GenerationOutput(completed_files, cost_usd=cost, logs="\n".join(lines), metadata={tokens, harness, model, effort})`.

### 5. Tests (`tests/test_agent_prover.py`)
- **Fast unit (no Docker, no creds):** feed a captured stream-json sample (save a small fixture under `tests/fixtures/agent_streams/`) to `Harness._parse_lines`; assert token counts and `compute_cost_usd`. Test the `completed_files` diff logic by stubbing the backend `.start` to write a solved file into the workdir (mirror Aristotle's `_fake_result` seam — consider factoring the agent launch behind a small overridable method so it's mockable).
- **Credentialed integration (opt-in):** add an `agent_api` marker, excluded by default via `addopts` exactly like `aristotle_api`. Read `CLAUDE_CODE_OAUTH_TOKEN` (and/or provider keys) from `.env`; skip if absent. Run Claude Code on the `mil_trivial` fixture and assert `result.success` (real agent + real Docker verify). Add `CLAUDE_CODE_OAUTH_TOKEN=...` to `.env.example`.

## Conventions to follow (already established)
- `ruff` (line-length 88, E/F/I/UP) + `mypy --strict` must pass; lefthook runs them on commit via `uv run`. Untyped third-party libs get a `[[tool.mypy.overrides]] ignore_missing_imports` block (see `aristotlelib`).
- Keep `prove()` returning `GenerationOutput`; let the base `run()` own verification.
- Tests that need the image are marked `docker`; credentialed/billable tests get their own opt-in marker + `.env`, never run by default.
- Commit per milestone with the established message style; `Co-Authored-By: Claude Opus 4.8`.

## Open decisions to confirm with the user
- **Default harness/model** for `AgentProverConfig` (stub currently says `claude_code` / `claude-opus-4-8` / effort `high`).
- **One image with all three CLIs**, or per-harness images? (Recommend one image; it's simplest and the CLIs are small relative to Mathlib.)
- **MCP**: confirm `uvx lean-lsp-mcp` is the intended server (it's what milp_flare uses).

## Definition of done
`uv run pytest -m docker` green (incl. a mocked AgentProver path); `uv run pytest -m agent_api` green locally with creds, completing `mil_trivial` to a verified, sorry-free proof; ruff + mypy clean; README build-order item 3 checked off.
