"""DockerBackend offline unit tests (no daemon).

Construction is offline, so these need no running Docker; the live parity suite lives
alongside the shared verifier tests under the ``docker`` marker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from open_atp.backends.base import CommandTimeout, ImageUnavailable, ProvisionError
from open_atp.backends.docker import DockerBackend, DockerCommandHandle


def test_wallclock_overhead_is_small_bind_mount_slack() -> None:
    # Bind-mounted: no push/pull, no warm-cache paging -- just container start/kill.
    assert DockerBackend().wallclock_overhead_s == 30


def test_session_without_docker_binary_raises_provision_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``docker`` on PATH is a provision failure, not a leaked FileNotFoundError."""
    monkeypatch.setenv("PATH", "")  # `docker` is now unresolvable
    with pytest.raises(ProvisionError, match="docker"):
        DockerBackend().session(tmp_path, timeout_s=60)


def test_session_with_missing_image_raises_image_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nonzero ``docker image inspect`` fails fast before any ``docker run``."""

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv[:3] == ["docker", "image", "inspect"]  # never reaches `run`
        return subprocess.CompletedProcess(argv, returncode=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ImageUnavailable, match="build-docker-image"):
        DockerBackend().session(tmp_path, timeout_s=60)


def _handle(argv: list[str]) -> DockerCommandHandle:
    """A DockerCommandHandle over a real local subprocess -- no daemon needed.

    The handle's timeout classification is exercised directly here rather than through
    ``prove``: reproducing a coreutils-``timeout`` budget kill (exit 124) end-to-end
    needs the live container, and the classify-and-raise is the intricate seam.
    """
    popen = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return DockerCommandHandle(
        popen=popen, container="test", started_at=0.0, budget_s=60, deadline_s=30.0
    )


def test_wait_raises_command_timeout_on_budget_kill_exit_code() -> None:
    """The coreutils-``timeout`` exit code (124) surfaces as CommandTimeout."""
    handle = _handle(["bash", "-c", "exit 124"])
    with pytest.raises(CommandTimeout, match="budget") as exc:
        handle.wait()
    # The partial result rides along so a salvaged candidate can still be verified.
    assert exc.value.result is not None and exc.value.result.exit_code == 124


def test_wait_returns_result_on_clean_exit() -> None:
    """A command that exits cleanly returns its captured result, no raise."""
    result = _handle(["bash", "-c", "echo hi"]).wait()
    assert result.exit_code == 0 and result.stdout.strip() == "hi"
