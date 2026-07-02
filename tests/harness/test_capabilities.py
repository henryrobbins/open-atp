"""End-to-end capability probes: does each agent CLI have what it needs?

Ported from milp_flare's ``test_docker.py`` harness suite. Each test drives a
real agent (``claude_code`` / ``codex`` / ``opencode`` / ``vibe``) through a real
compute backend (``docker`` / ``modal``) with a "make exactly one tool call then stop"
prompt, then inspects the streamed JSON event stream to confirm the capability
actually works inside the sandbox -- not merely that the harness *configured* it
(that is covered by the no-creds unit tests in ``test_agent_prover.py``).

The four capabilities the :class:`AgentProver` design depends on:

* **shell + Lean toolchain** -- the agent can ``lake build`` a fresh module;
* **skills** -- the ``probe-skill`` fixture ``stage_wd`` mounts is invocable
  from the harness-specific location (``.claude/skills`` vs ``.agents/skills``);
* **lean-lsp MCP** -- the ``mcp__lean-lsp__*`` tools the default prompt leans on
  are wired up; and
* **edit sync-back** -- a file the agent writes lands on the *host* workdir after
  the run completes (Docker bind-mounts it live; Modal pulls it on completion).

All four harnesses, ``vibe`` included, mount the ``probe-skill`` fixture (a no-op
skill, in place of the default bundle's skills) and run every probe; vibe stages
it under ``VIBE_HOME/skills`` (its trust-independent user skills dir) rather than
the project ``.agents/skills`` the others use.

Every test is marked ``agent_api`` (billable, needs agent creds) and excluded by
default via ``addopts``; the backend dimension carries the ``docker`` / ``modal``
marker per parametrization. Run e.g. ``pytest -m 'agent_api and docker'``. Each
case skips when its agent creds or its backend are unavailable.

Prerequisites:
  - claude_code: ``CLAUDE_CODE_OAUTH_TOKEN`` in env (or .env)
  - codex: ``~/.codex/auth.json`` from ``codex login``
  - opencode: ``DEEPSEEK_API_KEY`` in env (tests use deepseek-chat to keep
    integration spend off the Anthropic bill)
  - vibe: ``MISTRAL_API_KEY`` in env (tests run the non-Labs ``lean-standin``
    stand-in so they don't need Labs access to the builtin ``lean`` agent)
  - docker backend: docker on PATH + the ``open-atp:latest`` image
  - modal backend: ``MODAL_TOKEN_*`` env or ``~/.modal.toml`` + the published image
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from open_atp.backends.base import ComputeBackend
from open_atp.harness import (
    _HARNESSES,
    ClaudeCodeHarness,
    Harness,
    VibeHarness,
)
from open_atp.images import DEFAULT_IMAGE

pytestmark = pytest.mark.agent_api

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"
RUNS_DIR = Path(__file__).resolve().parents[1] / ".runs"

#: A no-op skill mounted into every probe so the skill-invocable check tests skill
#: discovery itself, not whatever the default config happens to ship. Staged via
#: ``stage_skills`` with no plugins -- plugin loading isn't what these probes exercise.
PROBE_SKILL = Path(__file__).parents[1] / "fixtures" / "skills" / "probe-skill"

#: A complete project the size of one tool call: edit in place, build, inspect.
#:
#: The framing matters for agents whose system prompt is a task scaffold (e.g.
#: vibe's ``lean`` agent, which otherwise ignores this and starts proving the
#: project's sorrys, churning/deleting the probe file and running to timeout). So
#: we state up front that this is a capability test -- not a coding/proof task --
#: and that stopping after the one call, without cleanup, IS the success condition.
ONE_CALL_PROMPT = """\
You are running inside an automated capability test for an agent tool harness.
We are verifying ONE thing: that a single specific tool works in this sandbox.
This is NOT a coding, editing, or proof task. Do not try to solve, prove, build,
or improve anything in this project, and do not explore it.

The single tool call you must make: {action}

Rules:
- Make this exact tool call exactly once. Do not retry if it fails.
- The instant that call returns, the test is COMPLETE -- stop immediately.
- Make no other tool call, before or after it. In particular, do not read, edit,
  prove, run, list, clean up after yourself, or DELETE or modify any file.
- Do not summarize. Make the one call, then stop.
"""

HARNESS_NAMES = ["claude_code", "codex", "opencode", "vibe"]

#: Cheap models per agent to keep the (billable) integration run inexpensive.
_MODELS = {
    "claude_code": "claude-haiku-4-5",
    # ChatGPT-subscription auth (the path these tests use) accepts the codex
    # defaults; an API-key-only mini model would 400 on the first turn.
    "codex": "gpt-5.5",
    # deepseek-chat keeps cost low and exercises OpenCode's non-anthropic branch
    # (provider is inferred from the model prefix in the harness).
    "opencode": "deepseek-chat",
    # The builtin ``lean`` agent pins a deprecated Leanstral; the ``lean-labs``
    # profile (selected in _make_harness) runs this model. Leanstral is $0-priced,
    # so the probe uses the real lab model.
    "vibe": "labs-leanstral-1-5",
}

# Backend dimension: each value carries its own opt-out marker so a run can pick
# one with ``-m 'agent_api and docker'`` / ``-m 'agent_api and modal'``.
BACKENDS = [
    pytest.param("docker", marks=pytest.mark.docker),
    pytest.param("modal", marks=pytest.mark.modal),
]


# --- availability gating ----------------------------------------------------


def _agent_available(harness: str) -> bool:
    if harness == "claude_code":
        return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    if harness == "codex":
        # Bills against the ChatGPT subscription via cached OAuth.
        return (Path.home() / ".codex" / "auth.json").exists()
    if harness == "opencode":
        return bool(os.environ.get("DEEPSEEK_API_KEY"))
    if harness == "vibe":
        return bool(os.environ.get("MISTRAL_API_KEY"))
    return False


def _modal_configured() -> bool:
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return True
    return (Path.home() / ".modal.toml").is_file()


def _backend_available(backend: str) -> bool:
    if backend == "docker":
        return shutil.which("docker") is not None
    if backend == "modal":
        return _modal_configured()
    return False


# --- construction -----------------------------------------------------------


def _make_backend(backend: str) -> ComputeBackend:
    if backend == "docker":
        from open_atp.backends.docker import DockerBackend

        return DockerBackend(image=DEFAULT_IMAGE)
    if backend == "modal":
        from open_atp.backends.modal import ModalBackend

        return ModalBackend(image=DEFAULT_IMAGE)
    raise AssertionError(backend)


def _make_harness(harness: str) -> Harness:
    if harness == "vibe":
        # The builtin ``lean`` agent pins a deprecated Leanstral; drive the vendored
        # ``lean-labs`` profile so the probe controls the model.
        return VibeHarness(
            model=_MODELS["vibe"],
            effort="low",
            agent="lean-labs",
        )
    if harness == "claude_code":
        # No plugins -- plugin loading isn't what these probes exercise.
        return ClaudeCodeHarness(model=_MODELS[harness], effort="low", plugins=[])
    return _HARNESSES[harness](model=_MODELS[harness], effort="low")


def _make_run_dir(backend: str, case_id: str) -> Path:
    """Return a fresh, empty run directory under ``tests/.runs/<backend>/<case_id>``.

    Clears any previous artifacts so each run starts clean; the directory
    (and the ``agent_output.jsonl`` written into it) persists after the test
    for post-mortem inspection.
    """
    run_dir = RUNS_DIR / backend / case_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    return run_dir


def _stage(run_dir: Path) -> Path:
    """Copy the fixture into ``run_dir/wd`` (a complete buildable project)."""
    wd = run_dir / "wd"
    shutil.copytree(FIXTURE, wd)
    return wd


def _auth(harness: Harness, backend: ComputeBackend) -> tuple[dict, list]:
    """Map the harness's resolved AgentAuth into backend env + mounts (like prove())."""
    auth = harness.agent_auth()
    home = backend.container_home
    mounts = [(str(src), f"{home}/{dest}") for src, dest in auth.mounts]
    return auth.env, mounts


def _run(harness_name: str, backend_name: str, run_dir: Path, action: str) -> Path:
    """Configure the workdir, launch the agent, and write output to a jsonl file.

    Returns the path to ``run_dir/agent_output.jsonl`` so callers can pass it
    directly to ``_load_events`` and include it in assertion messages.
    """
    if not _agent_available(harness_name):
        pytest.skip(f"credentials for {harness_name} not available")
    if not _backend_available(backend_name):
        pytest.skip(f"backend {backend_name} not available")

    harness = _make_harness(harness_name)
    backend = _make_backend(backend_name)
    wd = run_dir / "wd"
    harness.stage_wd(wd)
    harness.stage_skills(wd, [PROBE_SKILL])
    harness.write_prompt(wd, ONE_CALL_PROMPT.format(action=action))
    env, mounts = _auth(harness, backend)

    lines: list[str] = []
    with backend.start(
        wd, harness.command, env=env, mounts=mounts, timeout_s=600
    ) as handle:
        lines.extend(handle.stream())
        handle.wait()

    jsonl = run_dir / "agent_output.jsonl"
    jsonl.write_text("\n".join(lines))
    return jsonl


def _event_summary(events: list[dict]) -> str:
    """Compact description of the event types seen -- appended to failed asserts."""
    from collections import Counter

    def _key(ev: dict) -> str:
        t = ev.get("type", "?")
        item_t = (
            (ev.get("item") or {}).get("type")
            or (ev.get("part") or {}).get("tool")
            or ""
        )
        return f"{t}/{item_t}" if item_t else t

    counts = Counter(_key(ev) for ev in events)
    return f"events({sum(counts.values())}): " + ", ".join(
        f"{k}×{v}" for k, v in counts.most_common()
    )


def _load_events(jsonl: Path) -> list[dict]:
    """Parse the agent output jsonl, tolerating objects that span multiple lines.

    Most CLIs emit one JSON object per line, but some (opencode) embed raw
    newlines in tool payloads, so accumulate physical lines until the buffer
    decodes. ``strict=False`` tolerates embedded control characters.
    """
    decoder = json.JSONDecoder(strict=False)
    events: list[dict] = []
    buf = ""
    for raw in jsonl.read_text().splitlines():
        buf = raw if not buf else f"{buf}\n{raw}"
        if not buf.strip():
            buf = ""
            continue
        try:
            obj = decoder.decode(buf.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
        buf = ""
    return events


# --- per-agent classifiers --------------------------------------------------


def _claude_classify(
    events: list[dict],
    tool_name: str,
    matches: Callable[[dict], bool] = lambda _: True,
) -> tuple[str, str]:
    target_id: str | None = None
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for c in ev.get("message", {}).get("content", []) or []:
            if (
                c.get("type") == "tool_use"
                and c.get("name") == tool_name
                and matches(c.get("input") or {})
            ):
                target_id = c.get("id")
                break
        if target_id is not None:
            break
    if target_id is None:
        return "missing", "no matching tool_use"
    for ev in events:
        if ev.get("type") != "user":
            continue
        for c in ev.get("message", {}).get("content", []) or []:
            if c.get("type") == "tool_result" and c.get("tool_use_id") == target_id:
                if c.get("is_error"):
                    return "error", f"tool_result is_error: {c.get('content')!r}"
                return "success", "tool_result ok"
    return "missing", "no tool_result for id"


def _codex_classify(
    events: list[dict],
    item_type: str,
    matches: Callable[[dict], bool] = lambda _: True,
) -> tuple[str, str]:
    """Classify codex ``item.completed`` (or ``item.started``) events.

    Some model versions (e.g. gpt-5.5) emit ``item.started/command_execution``
    but never emit the corresponding ``item.completed`` -- the turn ends with an
    ``agent_message`` instead. Fall back to ``item.started`` so the capability
    probe still passes when the tool was clearly invoked.
    """
    started: dict | None = None
    target: dict | None = None
    for ev in events:
        t = ev.get("type")
        item = ev.get("item") or {}
        if item.get("type") != item_type or not matches(item):
            continue
        if t == "item.completed":
            target = item
            break
        if t == "item.started" and started is None:
            started = item
    if target is None and started is not None:
        # Completed event missing; treat a matching started event as success
        # for the capability probe (exit code unavailable).
        return "success", "item.started matched (no item.completed emitted)"
    if target is None:
        return "missing", f"no matching `{item_type}` in stream"
    if item_type == "command_execution":
        ok = target.get("status") == "completed" and target.get("exit_code") == 0
        return (
            ("success", "exit 0")
            if ok
            else (
                "error",
                f"status={target.get('status')!r} exit={target.get('exit_code')!r}",
            )
        )
    if item_type == "mcp_tool_call":
        result = target.get("result") or {}
        if result.get("isError") or result.get("is_error"):
            return "error", f"mcp is_error: {result!r}"
        return "success", "mcp ok"
    return "missing", f"unhandled item_type {item_type}"


def _opencode_classify(
    events: list[dict],
    tool_name: str,
    matches: Callable[[dict], bool] = lambda _: True,
) -> tuple[str, str]:
    """Classify opencode ``tool_use`` events (one event per call, state.status)."""
    target: dict | None = None
    for ev in events:
        if ev.get("type") != "tool_use":
            continue
        part = ev.get("part") or {}
        if part.get("tool") != tool_name:
            continue
        state = part.get("state") or {}
        if not matches(state.get("input") or {}):
            continue
        target = state
        break
    if target is None:
        return "missing", f"no tool_use for `{tool_name}`"
    status = target.get("status")
    if status == "error":
        return "error", f"state.error: {target.get('error')!r}"
    if status != "completed":
        return "error", f"status={status!r}"
    if tool_name == "bash":
        exit_code = (target.get("metadata") or {}).get("exit")
        if exit_code != 0:
            return "error", f"exit={exit_code!r}"
    return "success", f"status={status!r}"


#: Vibe wraps any failed tool call's content in this tag (vibe.core.utils.tags);
#: a non-zero bash exit is raised as a ToolError and surfaces the same way, so its
#: presence in a ``role:"tool"`` message is the uniform "this call failed" signal.
_VIBE_TOOL_ERROR_TAG = "<tool_error>"


def _vibe_classify(
    events: list[dict],
    tool_name: str,
    matches: Callable[[dict], bool] = lambda _: True,
) -> tuple[str, str]:
    """Classify vibe NDJSON messages (assistant ``tool_calls`` + ``role:"tool"``).

    Vibe's ``--output streaming`` dumps one ``LLMMessage`` per line: the assistant
    message carries OpenAI-style ``tool_calls`` (``function.name`` + JSON-string
    ``arguments``); the tool result is a separate ``role:"tool"`` message keyed by
    ``tool_call_id``. Success is a matching result with no ``<tool_error>`` tag.
    """
    target_id: str | None = None
    for ev in events:
        if ev.get("role") != "assistant":
            continue
        for tc in ev.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if fn.get("name") != tool_name:
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict) or not matches(args):
                continue
            target_id = tc.get("id")
            break
        if target_id is not None:
            break
    if target_id is None:
        return "missing", f"no assistant tool_call for `{tool_name}`"
    for ev in events:
        if ev.get("role") != "tool" or ev.get("tool_call_id") != target_id:
            continue
        content = ev.get("content") or ""
        if _VIBE_TOOL_ERROR_TAG in content:
            return "error", f"tool_error: {content!r}"
        return "success", "tool result ok"
    return "missing", "no tool result for id"


def _codex_skill_classify(
    events: list[dict],
    _tool: str | None,
    _matches: Callable[[dict], bool] | None,
) -> tuple[str, str]:
    """Pass iff no agent_message reports the skill missing/unavailable.

    Codex has no distinct Skill tool -- skills are markdown at
    ``.agents/skills/<name>/SKILL.md`` the agent reads on its own -- so a positive
    tool-call check is impossible; use the negative behavioral check instead.
    """
    NEG = (
        "not installed",
        "not available",
        "no skill",
        "skills are not",
        "couldn't find",
        "could not find",
        "cannot find",
    )
    for ev in events:
        if ev.get("type") != "item.completed":
            continue
        item = ev.get("item") or {}
        if item.get("type") != "agent_message":
            continue
        text = (item.get("text") or "").lower()
        for needle in NEG:
            if needle in text:
                return "error", f"agent reported skill missing: {needle!r}"
    return "success", "no skill-missing report in agent_message"


# --- per-agent check shapes (action prompt + classifier + tool + matcher) ----


def _bash_check(harness: str, command: str):
    if harness == "claude_code":
        return (
            f"Use the Bash tool to run exactly: `{command}`.",
            _claude_classify,
            "Bash",
            lambda inp: command in (inp.get("command") or ""),
        )
    if harness == "codex":
        return (
            f"Run exactly this shell command: `{command}`.",
            _codex_classify,
            "command_execution",
            lambda item: command in (item.get("command") or ""),
        )
    if harness == "opencode":
        return (
            f"Use the bash tool to run exactly: `{command}`.",
            _opencode_classify,
            "bash",
            lambda inp: command in (inp.get("command") or ""),
        )
    if harness == "vibe":
        return (
            f"Use the bash tool to run exactly: `{command}`.",
            _vibe_classify,
            "bash",
            lambda inp: command in (inp.get("command") or ""),
        )
    raise AssertionError(harness)


def _skill_check(harness: str, skill_name: str):
    if harness == "claude_code":
        return (
            f"Use the Skill tool to invoke the `{skill_name}` skill.",
            _claude_classify,
            "Skill",
            lambda inp: skill_name in str(inp.get("skill") or inp.get("name") or inp),
        )
    if harness == "opencode":
        return (
            f"Use the skill tool to invoke the `{skill_name}` skill.",
            _opencode_classify,
            "skill",
            lambda inp: skill_name in str(inp.get("name") or inp.get("skill") or inp),
        )
    if harness == "codex":
        return (
            f"Look up the `{skill_name}` skill and use it.",
            _codex_skill_classify,
            None,
            None,
        )
    if harness == "vibe":
        return (
            f"Use the skill tool to load the `{skill_name}` skill.",
            _vibe_classify,
            "skill",
            lambda inp: skill_name in str(inp.get("name") or inp),
        )
    raise AssertionError(harness)


def _lean_lsp_check(harness: str, file_rel: str):
    tool = "mcp__lean-lsp__lean_diagnostic_messages"
    prompt = f"Call the MCP tool `{tool}` on the file `{file_rel}`."
    if harness == "claude_code":
        return prompt, _claude_classify, tool, lambda _: True
    if harness == "codex":
        return (
            prompt,
            _codex_classify,
            "mcp_tool_call",
            lambda item: "lean_diagnostic_messages" in (item.get("tool") or ""),
        )
    if harness == "opencode":
        # OpenCode flattens MCP tool names to ``server_toolname``.
        return (
            prompt,
            _opencode_classify,
            "lean-lsp_lean_diagnostic_messages",
            lambda _: True,
        )
    if harness == "vibe":
        # Vibe publishes MCP tools as ``<server_alias>_<remote_name>`` (alias is the
        # config server name ``lean-lsp``), and its smaller model invokes the exact
        # name it's told to -- so the prompt must name the flattened form, not the
        # ``mcp__lean-lsp__*`` Claude alias the other prompts use.
        vibe_tool = "lean-lsp_lean_diagnostic_messages"
        return (
            f"Call the MCP tool `{vibe_tool}` on the file `{file_rel}`.",
            _vibe_classify,
            vibe_tool,
            lambda _: True,
        )
    raise AssertionError(harness)


# --- tests ------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("harness", HARNESS_NAMES)
def test_bash_lake_build_fresh_module(harness: str, backend: str) -> None:
    """Agent can run a shell command that builds a fresh Mathlib-importing module.

    Probes both the shell capability and that the warm-cache ``.lake`` symlink lets
    a freshly written module build (the macOS-seatbelt EPERM regression milp_flare
    chased). The agent writes the module, then builds it via one bash call.
    """
    run_dir = _make_run_dir(backend, f"bash_lake_build_fresh_module_{harness}")
    wd = _stage(run_dir)
    (wd / "Probe.lean").write_text("import Mathlib\n")
    action, classifier, tool, matcher = _bash_check(harness, "lake env lean Probe.lean")
    jsonl = _run(harness, backend, run_dir, action)
    events = _load_events(jsonl)
    outcome, evidence = classifier(events, tool, matcher)
    assert outcome == "success", (
        f"{outcome}: {evidence} | {_event_summary(events)} | see {jsonl}"
    )


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("harness", HARNESS_NAMES)
def test_skill_invocable(harness: str, backend: str) -> None:
    """Agent invokes the ``probe-skill`` fixture ``stage_wd`` mounted for it."""
    run_dir = _make_run_dir(backend, f"skill_invocable_{harness}")
    _stage(run_dir)
    action, classifier, tool, matcher = _skill_check(harness, "probe-skill")
    jsonl = _run(harness, backend, run_dir, action)
    events = _load_events(jsonl)
    outcome, evidence = classifier(events, tool, matcher)
    assert outcome == "success", (
        f"{outcome}: {evidence} | {_event_summary(events)} | see {jsonl}"
    )


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("harness", HARNESS_NAMES)
def test_lean_lsp_mcp(harness: str, backend: str) -> None:
    """Agent calls a lean-lsp MCP tool inside the sandbox (the default-prompt dep)."""
    run_dir = _make_run_dir(backend, f"lean_lsp_mcp_{harness}")
    _stage(run_dir)
    action, classifier, tool, matcher = _lean_lsp_check(harness, "MILExample.lean")
    jsonl = _run(harness, backend, run_dir, action)
    events = _load_events(jsonl)
    outcome, evidence = classifier(events, tool, matcher)
    assert outcome == "success", (
        f"{outcome}: {evidence} | {_event_summary(events)} | see {jsonl}"
    )


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("harness", HARNESS_NAMES)
def test_edits_present_on_host_after_completion(harness: str, backend: str) -> None:
    """A file the agent writes is on the host workdir once the run completes.

    The load-bearing assumption ``prove()``'s diff relies on: edits must reach the
    host. Docker bind-mounts the workdir live; Modal pulls it back only on
    completion -- so this asserts on the host filesystem *after* the run, not the
    event stream, making it backend-agnostic.
    """
    marker = "PROBE_SYNC_OK"
    action = (
        "Use your file-editing/write tool to create a file named "
        f"`probe_output.txt` whose entire contents are exactly: {marker}"
    )
    run_dir = _make_run_dir(backend, f"edits_present_on_host_{harness}")
    _stage(run_dir)
    jsonl = _run(harness, backend, run_dir, action)

    probe = run_dir / "wd" / "probe_output.txt"
    assert probe.is_file(), (
        f"agent's written file did not reach the host workdir | see {jsonl}"
    )
    assert marker in probe.read_text()
