"""DockerBackend offline unit tests (no daemon).

Construction is offline, so these need no running Docker; the live parity suite lives
alongside the shared verifier tests under the ``docker`` marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from open_atp.backends.base import ProvisionError
from open_atp.backends.docker import DockerBackend


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
