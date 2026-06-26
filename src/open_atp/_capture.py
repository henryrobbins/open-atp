"""Thread-safe per-thread ``stdout`` capture.

Some provers drive in-process libraries that print progress straight to stdout (e.g.
``aristotlelib``'s progress display, via plain :func:`print`). Under the concurrent
benchmark sweep a plain :func:`contextlib.redirect_stdout` is unsafe: it swaps the
process-global ``sys.stdout``, so concurrent runs clobber each other and corrupt the
restore. Instead this installs a single dispatcher on ``sys.stdout`` that routes each
thread's writes to its own sink, so one run's output lands in its own file without
touching another's.

Only code that resolves ``sys.stdout`` dynamically (``print`` and the like) is
captured; output from subprocesses -- which inherit the real file descriptor -- is not.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO, cast


class _ThreadStdoutDispatcher:
    """A ``sys.stdout`` stand-in routing each thread's writes to its own sink."""

    def __init__(self, base: TextIO) -> None:
        self._base = base
        self._local = threading.local()

    def _target(self) -> TextIO:
        return getattr(self._local, "sink", None) or self._base

    def set_sink(self, sink: TextIO | None) -> None:
        self._local.sink = sink

    def write(self, data: str) -> int:
        return self._target().write(data)

    def flush(self) -> None:
        self._target().flush()

    def __getattr__(self, name: str) -> object:
        # Delegate everything else (isatty, fileno, encoding, ...) to the real stream.
        return getattr(self._base, name)


_install_lock = threading.Lock()


def _dispatcher() -> _ThreadStdoutDispatcher:
    """Return the installed dispatcher, installing it on ``sys.stdout`` if needed."""
    with _install_lock:
        current = sys.stdout
        if isinstance(current, _ThreadStdoutDispatcher):
            return current
        dispatcher = _ThreadStdoutDispatcher(current)
        sys.stdout = cast("TextIO", dispatcher)
        return dispatcher


@contextmanager
def capture_stdout(path: Path) -> Iterator[None]:
    """Redirect *this thread's* ``sys.stdout`` to ``path`` for the block.

    Other threads' stdout is unaffected. Subprocess output (inheriting the real file
    descriptor) is not captured.
    """
    dispatcher = _dispatcher()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as sink:
        dispatcher.set_sink(sink)
        try:
            yield
        finally:
            dispatcher.set_sink(None)
