"""A verification compile killed for exceeding its budget yields no verdict.

No Docker: a fake backend whose exec raises :class:`CommandTimeout` (what the real
backends do on a coreutils-``timeout`` kill) drives ``verify`` through its timeout
branch, which logs and returns ``None``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from open_atp.backends.base import (
    CommandHandle,
    CommandResult,
    ComputeSession,
)
from open_atp.backends.base import (
    CommandTimeout as _CommandTimeout,
)
from open_atp.backends.docker import DockerBackend
from open_atp.images import DEFAULT_IMAGE
from open_atp.lean import LeanProject
from open_atp.verify import Verifier

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"


class _TimeoutHandle(CommandHandle):
    """A command whose ``wait`` always reports a budget kill."""

    def stream(self) -> Iterator[str]:
        return iter(())

    def wait(self) -> CommandResult:
        raise _CommandTimeout(
            "command exceeded its budget",
            result=CommandResult(124, "", "", 0.0),
        )


class _TimeoutSession(ComputeSession):
    def exec(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: int,
    ) -> CommandHandle:
        return _TimeoutHandle()

    def sync_out(self) -> None:
        pass

    def sync_in(self) -> None:
        pass

    def close(self) -> None:
        pass


class _TimeoutBackend(DockerBackend):
    def session(
        self,
        workdir: Path,
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
    ) -> ComputeSession:
        return _TimeoutSession()


def test_verify_returns_none_on_compile_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    verifier = Verifier(_TimeoutBackend(image=DEFAULT_IMAGE))

    with caplog.at_level(logging.WARNING, logger="open_atp"):
        report = verifier.verify(LeanProject(FIXTURE))

    assert report is None
    assert any("budget" in r.message for r in caplog.records)


def test_verify_in_session_returns_none_on_compile_timeout() -> None:
    verifier = Verifier(_TimeoutBackend(image=DEFAULT_IMAGE))
    with _TimeoutSession() as session:
        assert verifier.verify(LeanProject(FIXTURE), session=session) is None
