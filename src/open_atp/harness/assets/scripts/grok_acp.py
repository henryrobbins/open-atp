#!/usr/bin/env python3
"""Drive ``grok agent stdio`` over ACP and re-emit its event stream as JSONL.

``grok --single --output-format json`` prints only a single terminal object, so a
run's tool calls, reasoning, and progress never reach ``stdout.txt``. ACP (xAI's
Agent Client Protocol, JSON-RPC 2.0 over stdin/stdout) surfaces all of it as
``session/update`` notifications plus a final ``session/prompt`` response carrying
token usage -- https://docs.x.ai/build/cli/headless-scripting#acp

This driver speaks that protocol to a child ``grok agent stdio`` and writes one JSON
object per line to *its* stdout, which the backend captures as the run's transcript:

* every ``session/update``'s inner ``update`` dict (``sessionUpdate`` is the type
  discriminator: ``tool_call`` / ``tool_call_update`` / ...), **except** the assistant's
  message and reasoning, which ACP delivers as one token-level ``*_chunk`` event per
  delta (hundreds per turn). Those are coalesced: each contiguous run of
  ``agent_message_chunk`` / ``agent_thought_chunk`` deltas collapses to a single
  ``{"sessionUpdate": "agent_message" | "agent_thought", "content": ...}`` line.
  Coalescing is flush-on-transition -- a buffered run is emitted the moment the stream
  switches to a different ``sessionUpdate`` (including switching between message and
  thought), so at most one buffer is ever non-empty and stream order is preserved.
  Everything else is passed through unchanged.
* one final ``{"sessionUpdate": "result", ...}`` line with ``stopReason`` and the
  token counts pulled from the response ``_meta`` (the full ``_meta`` is preserved under
  ``raw_meta``). Those counts cover only the **final** assistant turn; the harness's
  ``_parse_lines`` reconstructs a cumulative-output estimate from the coalesced stream.

The reader thread never dies on a handler bug: each dispatch is wrapped so a malformed
event is logged and skipped rather than silently killing the loop (which would leave
the run hung until timeout). ACP ``content`` is a dict only on the message/thought
chunks -- on ``tool_call_update`` it is a *list* -- so text is read defensively.

Config comes from the environment the launch script exports: ``PROMPT`` (the task),
``GROK_MODEL`` / ``GROK_EFFORT`` (agent flags), and ``GROK_HOME`` (mounted OAuth).
``OPEN_ATP_TIMEOUT_S`` (forwarded by the prover) bounds the ``session/prompt`` wait.
fs/terminal client capabilities are advertised *off* so grok runs its own tools
inside the sandbox rather than delegating them back to this driver.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from typing import Any

#: Bound on the fast ACP handshake calls (initialize / authenticate / session/new).
#: These should return in well under a second; a short cap surfaces a stuck startup
#: quickly rather than hiding it behind the long prompt budget.
_HANDSHAKE_TIMEOUT = 120

#: Slack added to the prompt wait over the run's wall-clock budget, so on Modal the
#: backend's coreutils ``timeout`` kills the exec first (clean, leaves headroom to
#: sync the partial workdir out). On Docker (no backend cap) this self-terminating
#: wait is the only bound, firing a minute past budget.
_PROMPT_SLACK = 60

#: Fallback prompt budget when OPEN_ATP_TIMEOUT_S is unset (matches the prover default).
_DEFAULT_TIMEOUT = 1800


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class ACPDriver:
    def __init__(self) -> None:
        model = os.environ.get("GROK_MODEL", "grok-4.5")
        effort = os.environ.get("GROK_EFFORT", "high")
        # --always-approve auto-approves tool execution at the CLI level (no
        # session/request_permission round-trips); --model / --reasoning-effort are
        # first-class here, unlike `grok --single` (which exposes no effort flag).
        cmd = [
            "grok",
            "agent",
            "--always-approve",
            "--model",
            model,
            "--reasoning-effort",
            effort,
            "stdio",
        ]
        _log(f"[grok-acp] launching: {' '.join(cmd)}")
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
        )
        self._next_id = 0
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._send_lock = threading.Lock()
        self._agent_text: list[str] = []
        # Token-delta buffers coalesced into one event per contiguous run. _out_lock
        # serializes _emit and buffer access across the reader thread (streamed
        # updates) and the main thread (the trailing flush + result line).
        self._out_lock = threading.Lock()
        self._msg_buf: list[str] = []
        self._thought_buf: list[str] = []
        threading.Thread(target=self._read_loop, daemon=True).start()

    # --- JSON-RPC plumbing ------------------------------------------------
    def _send(self, obj: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        with self._send_lock:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()

    def _request(
        self, method: str, params: dict[str, Any], timeout: float = _HANDSHAKE_TIMEOUT
    ) -> dict[str, Any]:
        self._next_id += 1
        rid = self._next_id
        q: queue.Queue[dict[str, Any]] = queue.Queue()
        self._pending[rid] = q
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return q.get(timeout=timeout)

    def _emit(self, obj: dict[str, Any]) -> None:
        """Write one transcript line to our own stdout (the captured stream)."""
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Never let a handler bug kill the reader: a dead reader stops draining
            # grok's stdout, so the session/prompt response is never seen and the run
            # hangs until timeout. Log and skip the offending event instead.
            try:
                self._dispatch(msg)
            except Exception as exc:  # noqa: BLE001
                _log(f"[grok-acp] dispatch error ({exc!r}) on: {line[:200]}")

    def _dispatch(self, msg: dict[str, Any]) -> None:
        # Response to one of our requests.
        if "id" in msg and "method" not in msg:
            q = self._pending.pop(msg["id"], None)
            if q is not None:
                q.put(msg.get("result", {"__error__": msg.get("error")}))
            return
        method = msg.get("method")
        # Server -> client request: must be answered or the agent stalls.
        if "id" in msg:
            self._handle_server_request(msg["id"], method, msg.get("params") or {})
            return
        # Notification.
        if method == "session/update":
            self._handle_update((msg.get("params") or {}).get("update") or {})

    def _handle_update(self, update: dict[str, Any]) -> None:
        kind = update.get("sessionUpdate")
        # `content` is a {type,text} dict only on the message/thought chunks; on other
        # updates (e.g. tool_call_update) it can be a list, so read text defensively.
        content = update.get("content")
        text = content.get("text") or "" if isinstance(content, dict) else ""
        with self._out_lock:
            # Coalesce contiguous message/thought deltas. Flush-on-transition: the
            # other buffer is flushed the moment the stream switches type, so at most
            # one buffer holds data at a time and arrival order is preserved.
            if kind == "agent_message_chunk":
                if self._thought_buf:
                    self._flush_locked()
                self._msg_buf.append(text)
                return
            if kind == "agent_thought_chunk":
                if self._msg_buf:
                    self._flush_locked()
                self._thought_buf.append(text)
                return
            self._flush_locked()
            self._emit(update)

    def _flush_locked(self) -> None:
        """Emit the buffered thought/message run as one event. Holds ``_out_lock``.

        At most one buffer is non-empty (flush-on-transition), so order between the two
        never matters here.
        """
        if self._thought_buf:
            self._emit(
                {
                    "sessionUpdate": "agent_thought",
                    "content": {"type": "text", "text": "".join(self._thought_buf)},
                }
            )
            self._thought_buf.clear()
        if self._msg_buf:
            text = "".join(self._msg_buf)
            self._agent_text.append(text)
            self._emit(
                {
                    "sessionUpdate": "agent_message",
                    "content": {"type": "text", "text": text},
                }
            )
            self._msg_buf.clear()

    def _handle_server_request(
        self, rid: object, method: str | None, params: dict[str, Any]
    ) -> None:
        # --always-approve should suppress permission prompts; answer defensively
        # anyway so a stray request never deadlocks the run.
        if method == "session/request_permission":
            options = params.get("options") or []
            allow = next(
                (o for o in options if "allow" in (o.get("kind") or "")),
                options[0] if options else None,
            )
            oid = allow.get("optionId") if allow else None
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"outcome": {"outcome": "selected", "optionId": oid}},
                }
            )
        else:
            self._send({"jsonrpc": "2.0", "id": rid, "result": {}})

    # --- run --------------------------------------------------------------
    def run(self, prompt: str, cwd: str) -> int:
        init = self._request(
            "initialize",
            {
                "protocolVersion": 1,
                # fs/terminal off: grok runs its own tools in the sandbox rather
                # than delegating file/terminal ops back to this driver.
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            },
        )
        methods = {
            m.get("id") or m.get("methodId") for m in init.get("authMethods") or []
        }
        # OAuth login (mounted auth.json) is "cached_token"; XAI_API_KEY is
        # "xai.api_key". Prefer the key only when it's actually set.
        method_id: str | None
        if os.environ.get("XAI_API_KEY") and "xai.api_key" in methods:
            method_id = "xai.api_key"
        elif "cached_token" in methods:
            method_id = "cached_token"
        else:
            method_id = next(iter(m for m in methods if m), None)
        self._request(
            "authenticate", {"methodId": method_id, "_meta": {"headless": True}}
        )

        sess = self._request("session/new", {"cwd": cwd, "mcpServers": []})
        session_id = sess.get("sessionId")
        if not session_id:
            _log(f"[grok-acp] session/new failed: {json.dumps(sess)}")
            return 1

        # session/prompt is the whole proof attempt, so bound its wait by the run's
        # wall-clock budget (+ slack) rather than the short handshake cap.
        budget = int(os.environ.get("OPEN_ATP_TIMEOUT_S") or _DEFAULT_TIMEOUT)
        try:
            resp = self._request(
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
                timeout=budget + _PROMPT_SLACK,
            )
        except queue.Empty:
            _log(f"[grok-acp] session/prompt timed out after {budget}s")
            with self._out_lock:
                self._flush_locked()
                self._emit(
                    {
                        "sessionUpdate": "result",
                        "stopReason": "timeout",
                        "input_tokens": None,
                        "output_tokens": None,
                        "total_tokens": None,
                        "text": "".join(self._agent_text),
                    }
                )
            return 1
        meta = resp.get("_meta") or {}
        with self._out_lock:
            # Flush any trailing message/thought run the turn ended on, then close with
            # the result line. The token counts here are the final assistant turn only,
            # not the whole loop; raw_meta preserves every field xAI ships (e.g.
            # cachedReadTokens, reasoningTokens) for the harness's cost estimate.
            self._flush_locked()
            self._emit(
                {
                    "sessionUpdate": "result",
                    "stopReason": resp.get("stopReason"),
                    "input_tokens": meta.get("inputTokens"),
                    "output_tokens": meta.get("outputTokens"),
                    "total_tokens": meta.get("totalTokens"),
                    "raw_meta": meta,
                    "text": "".join(self._agent_text),
                }
            )
        return 0

    def close(self) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        self.proc.terminate()


def main() -> int:
    prompt = os.environ.get("PROMPT", "")
    driver = ACPDriver()
    try:
        return driver.run(prompt, os.getcwd())
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
