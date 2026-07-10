"""Unit tests for the staged Grok ACP driver (``grok_acp.py``).

The driver is a standalone script, not an importable package module, so it is loaded
by path. These tests exercise the event-stream handling in isolation -- no ``grok``
subprocess -- by building an ``ACPDriver`` with ``object.__new__`` and wiring only the
attributes the handlers touch, then capturing what it would write to stdout via a
stubbed ``_emit``.

Regression focus: coalescing must collapse contiguous message/thought deltas while
preserving order, and a malformed event must never kill the reader loop (a dead reader
stops draining grok's stdout, hanging the whole run until timeout -- the stall that
sank the earlier coalescing attempts).
"""

from __future__ import annotations

import importlib.util
import threading
from types import ModuleType, SimpleNamespace
from typing import Any

from open_atp.harness._paths import _SCRIPTS


def _load_driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("grok_acp", _SCRIPTS / "grok_acp.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _driver() -> tuple[Any, list[dict[str, Any]]]:
    """An ACPDriver with capture wiring but no launched subprocess."""
    mod = _load_driver()
    d = object.__new__(mod.ACPDriver)
    d._out_lock = threading.Lock()
    d._msg_buf, d._thought_buf, d._agent_text = [], [], []
    emitted: list[dict[str, Any]] = []
    d._emit = emitted.append  # type: ignore[method-assign]
    return d, emitted


def _chunk(kind: str, text: str) -> dict[str, Any]:
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}}


def test_coalesces_contiguous_runs_and_preserves_order() -> None:
    d, emitted = _driver()
    # thought run, message run, a non-delta event, then message->thought (direct switch)
    d._handle_update(_chunk("agent_thought_chunk", "Let "))
    d._handle_update(_chunk("agent_thought_chunk", "me."))
    d._handle_update(_chunk("agent_message_chunk", "I'll "))
    d._handle_update(_chunk("agent_message_chunk", "do it."))
    d._handle_update({"sessionUpdate": "available_commands_update", "cmds": []})
    d._handle_update(_chunk("agent_message_chunk", "done"))
    d._handle_update(_chunk("agent_thought_chunk", "next"))
    d._flush_locked()  # trailing flush, as run() does

    assert [e["sessionUpdate"] for e in emitted] == [
        "agent_thought",
        "agent_message",
        "available_commands_update",
        "agent_message",
        "agent_thought",
    ]
    # contiguous deltas collapse to one line each, in arrival order
    assert emitted[0]["content"]["text"] == "Let me."
    assert emitted[1]["content"]["text"] == "I'll do it."
    assert emitted[3]["content"]["text"] == "done"
    assert emitted[4]["content"]["text"] == "next"


def test_list_typed_content_does_not_crash() -> None:
    """``tool_call_update.content`` is a list, not a dict -- reading it must not throw.

    This is the exact bug that killed the reader thread and hung earlier coalescing
    attempts.
    """
    d, emitted = _driver()
    d._handle_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c1",
            "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}],
        }
    )
    # passed through verbatim, no exception raised
    assert emitted[-1]["sessionUpdate"] == "tool_call_update"


def test_read_loop_survives_a_throwing_dispatch() -> None:
    """A handler that raises is logged and skipped; the reader keeps draining stdout.

    Drives the real ``_read_loop`` over a stubbed grok stdout so the loop's own
    try/except is what protects it -- not test scaffolding.
    """
    mod = _load_driver()
    d = object.__new__(mod.ACPDriver)
    d.proc = SimpleNamespace(stdout=iter(['{"n": 1}\n', '{"n": 2}\n', '{"n": 3}\n']))
    dispatched: list[int] = []

    def flaky(msg: dict[str, Any]) -> None:
        dispatched.append(msg["n"])
        if msg["n"] == 1:
            raise RuntimeError("handler blew up")

    d._dispatch = flaky  # type: ignore[method-assign]
    d._read_loop()  # returns when the stubbed stdout iterator is exhausted

    assert dispatched == [1, 2, 3]  # the throw on msg 1 did not stop msgs 2 and 3
