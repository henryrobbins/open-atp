"""Thread-local stdout capture tests.

Verifies :func:`~open_atp._capture.capture_stdout` routes each thread's ``print`` to
its own file with no cross-contamination, and leaves an uncaptured thread's stdout
alone.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from open_atp._capture import capture_stdout


def test_capture_writes_this_threads_stdout(tmp_path: Path) -> None:
    out = tmp_path / "stdout.txt"
    with capture_stdout(out):
        print("captured line")
    assert out.read_text().strip() == "captured line"


def test_capture_is_thread_local(tmp_path: Path) -> None:
    start = threading.Barrier(2)

    def worker(name: str) -> None:
        with capture_stdout(tmp_path / f"{name}.txt"):
            start.wait()  # both threads capture concurrently before either prints
            print(f"hello from {name}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert (tmp_path / "a.txt").read_text().strip() == "hello from a"
    assert (tmp_path / "b.txt").read_text().strip() == "hello from b"


def test_uncaptured_thread_stdout_unaffected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "stdout.txt"
    with capture_stdout(out):
        print("inside")
    print("outside")  # no sink for this thread now -> normal stdout

    assert out.read_text().strip() == "inside"
    assert "outside" in capsys.readouterr().out
