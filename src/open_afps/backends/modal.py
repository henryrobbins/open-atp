"""Modal Sandbox backend.

Ported from milp_flare ``harness/runner/modal.py``, generalised from "launch an
agent" to "run an arbitrary command over a workdir" against our
:meth:`ComputeBackend.start` contract.

The one idea driving the port: Docker **bind-mounts** the workdir (edits land on
the host with no copy step), but Modal Sandboxes have an **isolated filesystem**, so
the backend must

1. **push** the populated workdir (and any credential mounts) into the Sandbox
   before running, and
2. **pull** it back out afterwards, so completed proofs land on the host.

Everything else is a variation of that isolation. Notable details carried over:

* Run as root with ``IS_SANDBOX=1`` so Claude Code's ``bypassPermissions`` works
  (Modal ignores the image ``USER`` and runs everything as root).
* Warm the Lean cache (and set up the ``.lake`` symlink) before timing real work, to
  dodge Modal's lazy layer paging.
* Redirect stdin to ``/dev/null`` to avoid Modal's stdin-open deadlock; capture
  stderr to a file pulled back with the workdir.
* Pin ``LEAN_NUM_THREADS`` to the allocated CPU count so Lean doesn't oversubscribe.
* Never leak a Sandbox: terminate on a failed start and after every run.
"""

from __future__ import annotations

import io
import logging
import tarfile
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from open_afps.backends.base import (
    BackendConfig,
    CommandHandle,
    CommandResult,
    ComputeBackend,
    wrap_command,
)

if TYPE_CHECKING:
    import modal
    from modal.container_process import ContainerProcess

log = logging.getLogger(__name__)

#: Working directory inside the Sandbox (same convention as Docker's bind mount).
REMOTE_WD = "/workspace/wd"
#: Image-baked warm Mathlib olean cache to symlink the workdir's ``.lake`` to.
BAKED_LAKE = "/workspace/.lake"


def _require_modal() -> None:
    """Validate that ``modal`` is importable, with an actionable error if not."""
    try:
        import modal  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "the modal compute backend requires the `modal` package; "
            "install it with `pip install open-afps` (modal is a core dependency)"
        ) from exc


def _tar_dir(src: Path) -> bytes:
    """Tar the contents of ``src`` into an in-memory gzip blob."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(str(src), arcname=".")
    return buf.getvalue()


def _modal_image_name(image: str) -> str:
    """Map an image ref to a Modal named-image lookup.

    The platform shares one ``image`` string across backends and it carries a
    Docker-style ``:tag`` (e.g. ``open-afps:latest``). Modal published images are
    looked up by bare name (``modal.Image.from_name`` takes no tag and Modal names
    can't contain ``:``), so strip a trailing tag.
    """
    return image.rsplit(":", 1)[0]


@dataclass
class ModalConfig(BackendConfig):
    cpu: float = 2.0
    memory_mib: int = 4096
    #: Modal app the Sandbox is associated with (also the publish target of
    #: ``open-afps build-modal-image``).
    app: str = "open-afps"


@dataclass
class ModalCommandHandle(CommandHandle):
    """Live handle for a command running in a Modal Sandbox."""

    proc: ContainerProcess[str]
    sb: modal.Sandbox
    workdir: Path
    started_at: float
    _stdout_lines: list[str] = field(default_factory=list)

    def stream(self) -> Iterator[str]:
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self._stdout_lines.append(line)
            yield line

    def cancel(self) -> None:
        # No per-exec kill on a Sandbox, so pull partial artifacts (best effort) and
        # then terminate -- terminating is the authoritative kill and also ensures we
        # never leak compute. Idempotent: safe to call after wait() already tore down.
        _pull_wd(self.sb, self.workdir)
        _terminate(self.sb)

    def wait(self) -> CommandResult:
        # Draining stdout (above) usually fills _stdout_lines; wait() returns the
        # exit code and releases the process.
        for line in self.proc.stdout:
            self._stdout_lines.append(line.rstrip("\n"))
        exit_code = self.proc.wait()
        stderr = _pull_stderr(self.sb)
        _pull_wd(self.sb, self.workdir)
        _terminate(self.sb)
        return CommandResult(
            exit_code=exit_code,
            stdout="\n".join(self._stdout_lines),
            stderr=stderr,
            duration_s=time.time() - self.started_at,
        )


def _terminate(sb: modal.Sandbox) -> None:
    """Tear down the Sandbox; idempotent (safe if it is already gone)."""
    try:
        sb.terminate()
    except Exception:
        log.debug("terminate: Sandbox already gone")


def _push_dir(sb: modal.Sandbox, src: Path, dest: str) -> None:
    """Tar ``src`` and extract it into ``dest`` inside the Sandbox."""
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        tmp.write(_tar_dir(src))
        tmp.flush()
        sb.filesystem.copy_from_local(tmp.name, "/tmp/push.tar.gz")
    sb.exec(
        "bash",
        "-c",
        f"mkdir -p {dest} && tar -xzf /tmp/push.tar.gz -C {dest}",
    ).wait()


def _pull_wd(sb: modal.Sandbox, wd: Path) -> None:
    """Tar the Sandbox workdir back out and extract it over the host ``wd``.

    Excludes the huge image-baked ``.lake`` symlink target. For the verify path no
    files change so this is just the stderr/log; for generation it is how edits
    return -- one code path that always pulls (cheap for small projects).
    """
    try:
        sb.exec(
            "bash",
            "-c",
            f"tar -czf /tmp/out.tar.gz -C {REMOTE_WD} --exclude=./.lake .",
        ).wait()
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
            sb.filesystem.copy_to_local("/tmp/out.tar.gz", tmp.name)
            with tarfile.open(tmp.name, mode="r:gz") as tf:
                tf.extractall(wd)
    except Exception:
        log.warning("pull_wd: failed to pull artifacts (Sandbox unavailable)")


def _pull_stderr(sb: modal.Sandbox) -> str:
    """Read back the captured ``modal_stderr.txt`` (empty if unavailable)."""
    try:
        with sb.open(f"{REMOTE_WD}/modal_stderr.txt", "r") as f:
            return str(f.read())
    except Exception:
        return ""


class ModalBackend(ComputeBackend):
    config: ModalConfig

    # Modal ignores the image USER and runs everything as root; credential mounts
    # land under root's HOME.
    container_home = "/root"

    def __init__(self, config: ModalConfig) -> None:
        super().__init__(config)

    @property
    def name(self) -> str:
        return "modal"

    def _wrap(self, command: str) -> str:
        return wrap_command(REMOTE_WD, BAKED_LAKE, command)

    def start(
        self,
        workdir: Path,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
        timeout_s: int | None = None,
    ) -> CommandHandle:
        _require_modal()
        import modal

        app = modal.App.lookup(self.config.app, create_if_missing=True)
        image = modal.Image.from_name(_modal_image_name(self.config.image))

        cpu = self.config.cpu
        secret_dict: dict[str, str | None] = {
            **self.config.env,
            **(env or {}),
            # Lets Claude Code's bypassPermissions run as root (Modal runs as root).
            "IS_SANDBOX": "1",
            # Pin Lean's thread pool to the reserved cores so it can't oversubscribe.
            "LEAN_NUM_THREADS": str(max(1, int(cpu))),
        }
        secret = modal.Secret.from_dict(secret_dict)

        # Idle Sandbox with no main process: we must push the workdir before exec.
        sb = modal.Sandbox.create(
            app=app,
            image=image,
            secrets=[secret],
            cpu=cpu,
            memory=self.config.memory_mib,
            timeout=timeout_s or self.config.timeout_s,
        )
        try:
            # Push the workdir, then each extra (host, container) mount -- replaces
            # Docker's bind mounts (workdir + credential dirs).
            _push_dir(sb, workdir, REMOTE_WD)
            for host, dest in mounts or ():
                _push_dir(sb, Path(host), dest)

            # Warm the Lean cache (and wire the .lake symlink) before timing real
            # work: Modal pages image layers in lazily, so the first build is slow.
            sb.exec(
                "bash",
                "-c",
                f"ln -sfn {BAKED_LAKE} {REMOTE_WD}/.lake && lake build",
                workdir=REMOTE_WD,
            ).wait()

            started_at = time.time()
            # Redirect stdin from /dev/null (Modal leaves it an open pipe with no EOF,
            # so agent CLIs hang) and capture stderr to a file pulled back on wait().
            proc = sb.exec(
                "bash",
                "-c",
                f"{{ {self._wrap(command)} ; }} "
                f"< /dev/null 2> {REMOTE_WD}/modal_stderr.txt",
                workdir=REMOTE_WD,
            )
        except BaseException:
            # Provisioning failed after create; release the Sandbox before
            # propagating so a failed start never leaks compute.
            _terminate(sb)
            raise
        return ModalCommandHandle(
            proc=proc, sb=sb, workdir=workdir, started_at=started_at
        )
