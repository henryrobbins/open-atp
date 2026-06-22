"""Modal sandbox backend (skeleton).

Port target: milp_flare ``harness/runner/modal.py``. Notable details to carry over:

* Spin up an idle ``modal.Sandbox`` from the image, then ``exec`` the command.
* Redirect stdin to ``/dev/null`` to avoid Modal's stdin-open deadlock.
* Symlink the image's pre-baked ``/workspace/.lake`` into the workdir and run a
  warm-up ``lake build`` before timing real work, to dodge cold-cache latency.
* Sync the workdir in/out via tar.gz.
* Pin ``LEAN_NUM_THREADS`` to the allocated CPU count.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from open_afps.backends.base import BackendConfig, CommandHandle, ComputeBackend


@dataclass
class ModalConfig(BackendConfig):
    cpu: float = 2.0
    memory_mib: int = 4096


class ModalBackend(ComputeBackend):
    config: ModalConfig

    def __init__(self, config: ModalConfig) -> None:
        super().__init__(config)

    @property
    def name(self) -> str:
        return "modal"

    def start(
        self,
        workdir: Path,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
        timeout_s: int | None = None,
    ) -> CommandHandle:
        # TODO(phase 1): create modal.Sandbox(image=..., cpu=..., memory=...),
        #   sync workdir in, exec command with stdin=/dev/null, stream stdout,
        #   sync workdir back out. Port from milp_flare/runner/modal.py.
        raise NotImplementedError("ModalBackend.start not yet ported")
