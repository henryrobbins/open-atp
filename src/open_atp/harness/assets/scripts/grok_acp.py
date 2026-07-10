#!/usr/bin/env python3
"""Drive ``grok agent stdio`` over ACP and re-emit its event stream as JSONL.

``grok --single --output-format json`` prints only a single terminal object, so a
run's tool calls, reasoning, and progress never reach ``stdout.txt``. ACP (xAI's
Agent Client Protocol, JSON-RPC 2.0 over stdin/stdout) surfaces all of it as
``session/update`` notifications plus a final ``session/prompt`` response carrying
token usage -- https://docs.x.ai/build/cli/headless-scripting#acp

This driver speaks that protocol to a child ``grok agent stdio`` and writes one JSON
object per line to *its* stdout, which the backend captures as the run's transcript:

* every ``session/update``'s inner ``update`` dict, verbatim (``sessionUpdate`` is the
  type discriminator: ``tool_call`` / ``tool_call_update`` / ``agent_message_chunk`` /
  ``agent_thought_chunk`` / ...); and
* one final ``{"sessionUpdate": "result", ...}`` line with ``stopReason`` and the
  normalized token counts pulled from the response ``_meta``.

Config comes from the environment the launch script exports: ``PROMPT`` (the task),
``GROK_MODEL`` / ``GROK_EFFORT`` (agent flags), and ``GROK_HOME`` (mounted OAuth).
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
        threading.Thread(target=self._read_loop, daemon=True).start()

    # --- JSON-RPC plumbing ------------------------------------------------
    def _send(self, obj: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        with self._send_lock:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()

    def _request(
        self, method: str, params: dict[str, Any], timeout: float = 1800
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
            self._dispatch(msg)

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
        self._emit(update)
        if update.get("sessionUpdate") == "agent_message_chunk":
            text = ((update.get("content") or {}).get("text")) or ""
            if text:
                self._agent_text.append(text)

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

        resp = self._request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
        )
        meta = resp.get("_meta") or {}
        self._emit(
            {
                "sessionUpdate": "result",
                "stopReason": resp.get("stopReason"),
                "input_tokens": meta.get("inputTokens"),
                "output_tokens": meta.get("outputTokens"),
                "total_tokens": meta.get("totalTokens"),
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
