"""Shared test config: load ``.env`` so opt-in tests can read their credentials.

The live Aristotle test is marked ``aristotle_api`` and excluded by default via
``addopts`` in ``pyproject.toml``; run it explicitly with ``-m aristotle_api``.
Its ``ARISTOTLE_API_KEY`` is read from a ``.env`` file at the repo root.

Also exposes :func:`fake_session_backend`: a backend whose live session is an
in-process no-op, so the no-Docker unit tests can drive a prover's ``_generate``
(which always opens a session and verifies in it) without spinning up a container.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from open_atp.backends.base import CommandHandle, CommandResult, ComputeSession
from open_atp.backends.docker import DockerBackend
from open_atp.images import DEFAULT_IMAGE

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def _load_dotenv(path: Path) -> None:
    """Minimal ``.env`` reader: ``KEY=VALUE`` lines, without overriding real env."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv(_ENV_FILE)


# The no-Docker prover/harness unit tests stub the agent run but still build the
# harness auth, which only checks each provider key is *present* (the value is
# never used off the live path). Seed placeholders so the suite is self-contained
# without a local ``.env`` (e.g. on CI). ``setdefault`` keeps real creds -- needed
# by the opt-in live tests -- authoritative when they are set.
for _placeholder_env in (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "MISTRAL_API_KEY",
):
    os.environ.setdefault(_placeholder_env, "test-placeholder")


class _FakeHandle(CommandHandle):
    """A finished, output-less command -- enough for an in-session verify exec."""

    def stream(self) -> Iterator[str]:
        return iter(())

    def cancel(self) -> None:
        pass

    def wait(self) -> CommandResult:
        return CommandResult(exit_code=0, stdout="", stderr="", duration_s=0.0)


class _FakeSession(ComputeSession):
    """An in-process session: every exec returns an empty result, syncs are no-ops."""

    def exec(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: int | None = None,
    ) -> CommandHandle:
        return _FakeHandle()

    def sync_out(self) -> None:
        pass

    def sync_in(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeSessionBackend(DockerBackend):
    """A :class:`DockerBackend` whose :meth:`session` is an in-process no-op.

    Lets the no-Docker unit tests exercise ``_generate`` -- which always opens a
    session and verifies in it -- without a live sandbox: the agent run is stubbed,
    and the in-session verify execs into the fake (returning an empty compile result).
    """

    def session(
        self,
        workdir: Path,
        *,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
        timeout_s: int | None = None,
    ) -> ComputeSession:
        return _FakeSession()


@pytest.fixture
def fake_session_backend() -> FakeSessionBackend:
    """A backend with an in-process session for the no-Docker prover unit tests."""
    return FakeSessionBackend(image=DEFAULT_IMAGE)
