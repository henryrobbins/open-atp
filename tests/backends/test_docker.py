"""DockerBackend offline unit tests (no daemon).

Construction is offline, so these need no running Docker; the live parity suite lives
alongside the shared verifier tests under the ``docker`` marker.
"""

from __future__ import annotations

from open_atp.backends.docker import DockerBackend


def test_wallclock_overhead_is_small_bind_mount_slack() -> None:
    # Bind-mounted: no push/pull, no warm-cache paging -- just container start/kill.
    assert DockerBackend().wallclock_overhead_s == 30
