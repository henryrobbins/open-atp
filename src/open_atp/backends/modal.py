"""Modal Sandbox backend.

Runs an arbitrary command over a workdir against the
:meth:`ComputeBackend.session` contract.

The one idea driving the design: Docker **bind-mounts** the workdir (edits land on
the host with no copy step), but Modal Sandboxes have an **isolated filesystem**, so
the backend must

1. **push** the populated workdir (and any credential mounts) into the Sandbox
   before running, and
2. **pull** it back out afterwards, so completed proofs land on the host.

Everything else is a variation of that isolation. Notable details:

* Run as root with ``IS_SANDBOX=1`` so Claude Code's ``bypassPermissions`` works
  (Modal ignores the image ``USER`` and runs everything as root).
* Warm the Lean cache (and set up the ``.lake`` symlink) before timing real work, to
  dodge Modal's lazy layer paging.
* Redirect stdin to ``/dev/null`` to avoid Modal's stdin-open deadlock; capture
  stderr to a file pulled back with the workdir.
* Pin ``LEAN_NUM_THREADS`` to the allocated CPU count so Lean doesn't oversubscribe.
"""

from __future__ import annotations

import contextvars
import io
import logging
import queue
import shlex
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

import modal

from open_atp.backends.base import (
    BAKED_LAKE,
    WORKDIR_MOUNT,
    CommandHandle,
    CommandResult,
    ComputeBackend,
    ComputeError,
    ComputeSession,
    ExecTimeout,
    SandboxUnreachable,
    TransferError,
    wrap_command,
)
from open_atp.images import DEFAULT_IMAGE, Image

if TYPE_CHECKING:
    from modal.container_process import ContainerProcess

log = logging.getLogger("open_atp")

#: Time to wait before sending SIGKILL to coreutils ``timeout``-capped commands.
TIMEOUT_KILL_AFTER_S = 30
#: Margin added to ``sb.exec`` client timeout to ensure the command is killed by
#: coreutils ``timeout`` before the client deadline lapses.
EXEC_DEADLINE_MARGIN_S = 30
#: Wall-clock timeout for Modal Sandbox file transfer to complete.
TRANSFER_TIMEOUT_S = 60
#: Wall-clock timeout for running ``tar`` inside the Sandbox to push/pull a directory.
TAR_TIMEOUT_S = 30
#: Wall-clock timeout for warming the Lean cache in a Modal Sandbox (``lake build``).
WARM_BUILD_TIMEOUT_S = 300
#: Wall-clock timeout for Modal Sandbox termination.
TERMINATE_TIMEOUT_S = 5
#: Grace window to drain remaining stdout after the process has exited.
DRAIN_GRACE_S = 15
#: Poll interval for Sandbox liveness check while blocked.
LIVENESS_POLL_INTERVAL_S = 5


def _is_dead(sb: modal.Sandbox) -> bool:
    """Return if the Modal Sandbox is dead."""
    return sb.poll() is not None


def _safe_run[T](
    fn: Callable[[], T],
    *,
    timeout_s: float,
    sb: modal.Sandbox,
    label: str,
) -> T:
    """Run function with blocking Modal calls, bounded by liveness and a wall-clock.

    Modal's ``proc.wait``, ``proc.stdout``, ``filesystem.copy_*``, etc... are
    well-behaved for a gracefully reaped Sandbox.

    - ``proc.wait`` returns the kill-signal code (137)
    - ``proc.stdout`` reaches EOF
    - ``filesystem.copy_*`` raises ``NotFoundError``

    However, an unreachable worker (crash / network partition) can stall them
    until the timeout is reached or indefinitely. Notably, ``filesystem.copy_*``
    and ``terminate`` have no client-side deadline knob and can hang indefinitely
    on a dead worker.

    To ensure blocking calls fail quickly on a dead Sandbox and are prevented
    from indefinitely stalling, this wrapper runs the blocking function on
    a daemon thread with a timeout and polls the Sandbox liveness.

    Parameters
    ----------
    fn : Callable[[], T]
        The blocking function to run.
    timeout_s : float
        Wall-clock time budget for ``fn`` to complete, in seconds.
    sb : modal.Sandbox
        The Sandbox to check liveness on while ``fn`` is blocked.
    label : str
        Label for logging the call in case of a stall or timeout.

    Returns
    -------
    T
        The return value of ``fn`` if it completes before the deadline.

    Raises
    ------
    SandboxUnreachable
        If the Sandbox is reaped while ``fn`` is in flight.
    ExecTimeout
        If ``fn`` does not complete before ``timeout_s``.
    """
    result: list[T] = []
    error: list[BaseException] = []
    ctx = contextvars.copy_context()

    def target() -> None:
        try:
            result.append(ctx.run(fn))
        except BaseException as exc:  # re-raised on the caller's thread
            error.append(exc)

    worker = threading.Thread(target=target, daemon=True)
    started = time.monotonic()
    worker.start()
    waited = 0.0
    while True:
        worker.join(LIVENESS_POLL_INTERVAL_S)
        if not worker.is_alive():
            break
        waited += LIVENESS_POLL_INTERVAL_S
        if _is_dead(sb):
            log.warning(
                "modal call aborted",
                extra={
                    "label": label,
                    "reason": "sandbox_dead",
                    "elapsed_s": round(time.monotonic() - started, 1),
                },
            )
            raise SandboxUnreachable("Sandbox terminated while awaiting a Modal call")
        if waited >= timeout_s:
            log.warning(
                "modal call aborted",
                extra={
                    "label": label,
                    "reason": "timeout",
                    "elapsed_s": round(time.monotonic() - started, 1),
                    "timeout_s": timeout_s,
                },
            )
            raise ExecTimeout(f"Modal call exceeded {timeout_s:.0f}s client deadline")
    if error:
        raise error[0]
    return result[0]


def _safe_stream(stream: Iterable[str], sb: modal.Sandbox) -> Iterator[str]:
    """Yield stream chunks bounded by Sandbox liveness.

    A blocking ``for chunk in stdout`` carries only the exec's own deadline. An
    unreachable worker (crash / network partition) may never deliver EOF, stalling
    the loop all the way to that deadline.

    To quickly fail on a dead Sandbox, we run the blocking iteration on a daemon
    thread and poll the Sandbox liveness while the stream is blocked.

    Parameters
    ----------
    stream : Iterable[str]
        The blocking stream to iterate.
    sb : modal.Sandbox
        The Sandbox to check liveness on while the stream is blocked.

    Yields
    ------
    Iterator[str]
        Each stream chunk as the underlying stream delivers it.

    Raises
    ------
    SandboxUnreachable
        If the Sandbox is reaped prior to the timeout.
    ExecTimeout
        If the Sandbox was reaped due to a timeout.
    """
    relay: queue.Queue[tuple[str, object]] = queue.Queue()
    ctx = contextvars.copy_context()

    def produce() -> None:
        try:
            for chunk in stream:
                relay.put(("chunk", chunk))
            relay.put(("done", None))
        except BaseException as exc:  # surfaced on the consumer thread
            relay.put(("error", exc))

    threading.Thread(target=lambda: ctx.run(produce), daemon=True).start()
    while True:
        try:
            kind, payload = relay.get(timeout=LIVENESS_POLL_INTERVAL_S)
        except queue.Empty:
            if _is_dead(sb):
                raise SandboxUnreachable("stdout stream stalled; Sandbox unreachable")
            continue
        if kind == "chunk":
            yield cast(str, payload)
        elif kind == "done":
            return
        else:
            _raise_stream_error(cast(Exception, payload), sb)


def _raise_stream_error(exc: Exception, sb: modal.Sandbox) -> NoReturn:
    """Translate a stream failure into a typed :class:`ComputeError`."""
    from modal.exception import ExecTimeoutError

    if isinstance(exc, ExecTimeoutError):
        raise ExecTimeout("exec exceeded its client deadline while streaming") from exc
    if _is_dead(sb):
        raise SandboxUnreachable(
            "stdout stream terminated; Sandbox unreachable"
        ) from exc
    raise exc


def _tar_dir(src: Path) -> bytes:
    """Tar the contents of ``src`` into an in-memory gzip blob."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(str(src), arcname=".")
    return buf.getvalue()


def _sb_exec_args(command: str, timeout_s: int, *, capture_io: bool) -> list[str]:
    """Build the args for ``sb.exec`` to run ``command`` in a Modal Sandbox."""
    # Modal's `sb.exec` timeout does not kill the process group, so we
    # wrap commands in coreutils `timeout` to ensure the group is killed.
    timeout = f"timeout --kill-after={TIMEOUT_KILL_AFTER_S} {timeout_s}"
    payload = f"{timeout} bash -c {shlex.quote(command)}"
    if capture_io:
        # Modal leaves stdin open with no EOF. If the agent CLI attempts to read
        # from stdin, it hangs forever. Redirect stdin to /dev/null to avoid this.
        # Write stderr to a file in the workdir so that it is accessible on sync.
        payload += f" < /dev/null 2> {WORKDIR_MOUNT}/modal_stderr.txt"
    return ["bash", "-c", payload]


def _sb_exec_deadline(timeout_s: int) -> int:
    """The Modal ``sb.exec`` client deadline for a given ``timeout_s`` budget."""
    return timeout_s + TIMEOUT_KILL_AFTER_S + EXEC_DEADLINE_MARGIN_S


def _sb_exec(
    sb: modal.Sandbox,
    command: str,
    timeout_s: int,
    *,
    workdir: str | None = None,
    secrets: Sequence[modal.Secret] | None = None,
    capture_io: bool = False,
) -> ContainerProcess[str]:
    """Run ``command`` in ``sb`` with the standard timeout wrapping and deadline.

    `capture_io` redirects stdin to /dev/null and captures stderr to a file in
    the workdir so that it is accessible on sync. It is true when executing
    external commands in a provisioned Sandbox.
    """
    return sb.exec(
        *_sb_exec_args(command, timeout_s, capture_io=capture_io),
        workdir=workdir,
        secrets=secrets,
        timeout=_sb_exec_deadline(timeout_s),
    )


def _modal_image_name(image: str) -> str:
    """Map an image ref to a Modal named-image lookup."""
    # Modal published images are looked up by bare name; strip a trailing tag
    return image.rsplit(":", 1)[0]


@dataclass
class ModalCommandHandle(CommandHandle):
    """A command exec'd into a live :class:`ModalSession`.

    :meth:`wait` does NOT ``_pull_wd``/``_terminate``: the session owns teardown
    (:meth:`ModalSession.close`), so a finished exec leaves the Sandbox up for the
    next command (and close, guaranteed by the session context manager, is what
    reclaims it).
    """

    proc: ContainerProcess[str]
    sb: modal.Sandbox
    started_at: float
    #: Client wall-clock (seconds) the process wait is bounded by.
    deadline_s: float
    _stdout_lines: list[str] = field(default_factory=list)
    _buf: str = ""

    def stream(self) -> Iterator[str]:
        """Yield stdout split into lines, re-buffering Modal's arbitrary chunks."""
        for chunk in _safe_stream(self.proc.stdout, self.sb):
            self._buf += chunk
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._stdout_lines.append(line)
                yield line

    def _drain_remaining(self) -> None:
        """Drain remaining stdout after the process has exited.

        ``sb.exec`` timeout kills only the direct process and child/grandchild
        processes may survive as orphans keeping the stdout pipe open and
        preventing EOF from ever arriving. After a short timeout, we take
        whatever is buffered and stop rather than block indefinitely.

        See https://github.com/modal-labs/modal-client/issues/3992.
        """

        def pull() -> None:
            for chunk in self.proc.stdout:
                self._buf += chunk

        try:
            _safe_run(pull, timeout_s=DRAIN_GRACE_S, sb=self.sb, label="drain")
        except ComputeError:
            log.warning("drain: stdout did not close after exit; using partial output")
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._stdout_lines.append(line)
        if self._buf:
            self._stdout_lines.append(self._buf)
            self._buf = ""

    def _collect_result(self) -> CommandResult:
        """Wait for the process to exit and collect stdout/stderr to compose result."""
        exit_code = _safe_run(
            self.proc.wait, timeout_s=self.deadline_s, sb=self.sb, label="proc_wait"
        )
        self._drain_remaining()
        stderr = _pull_stderr(self.sb)
        return CommandResult(
            exit_code=exit_code,
            stdout="\n".join(self._stdout_lines),
            stderr=stderr,
            duration_s=time.time() - self.started_at,
        )

    def wait(self) -> CommandResult:
        """Collect the result, leaving the Sandbox up for the session."""
        return self._collect_result()


def _terminate(sb: modal.Sandbox) -> None:
    """Tear down the Sandbox"""
    try:
        _safe_run(sb.terminate, timeout_s=TERMINATE_TIMEOUT_S, sb=sb, label="terminate")
    except Exception:
        log.warning("terminate: bounded teardown failed")


def _push_dir(sb: modal.Sandbox, src: Path, dest: str) -> None:
    """Tar ``src`` and extract it into ``dest`` inside the Sandbox."""
    log.debug("pushing dir into sandbox", extra={"src": str(src), "dest": dest})

    def do_push() -> None:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
            tmp.write(_tar_dir(src))
            tmp.flush()
            sb.filesystem.copy_from_local(tmp.name, "/tmp/push.tar.gz")
        _sb_exec(
            sb,
            f"mkdir -p {dest} && tar -xzf /tmp/push.tar.gz -C {dest}",
            TAR_TIMEOUT_S,
        ).wait()

    _safe_run(
        do_push,
        timeout_s=_sb_exec_deadline(TRANSFER_TIMEOUT_S),
        sb=sb,
        label="push_dir",
    )


def _pull_wd(sb: modal.Sandbox, wd: Path) -> None:
    """Tar the Sandbox workdir and extract it over the host ``wd``.

    Raises :class:`~open_atp.backends.base.TransferError` when the workdir cannot be
    retrieved (the Sandbox is already gone, or the tar copy/extract fails), so a
    result-bearing pull is a loud failure rather than a silent degrade into an empty
    verify. Teardown callers that only pull best-effort must swallow it.
    """
    if _is_dead(sb):
        raise TransferError(
            "pull_wd: Sandbox terminated before the workdir could be pulled"
        )

    def do_pull() -> None:
        # Exclude the image-baked .lake symlink target
        proc = _sb_exec(
            sb,
            f"tar -czf /tmp/out.tar.gz -C {WORKDIR_MOUNT} --exclude=./.lake .",
            TAR_TIMEOUT_S,
        )
        # tar exits non-zero on e.g. a file that changed/vanished mid-archive; surface
        # it rather than letting the (possibly partial/missing) copy fail opaquely.
        exit_code = proc.wait()
        if exit_code != 0:
            stderr = "".join(proc.stderr).strip() or "(no stderr)"
            log.warning(
                "pull_wd: tar exited nonzero",
                extra={"exit_code": exit_code, "stderr": stderr},
            )

        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
            sb.filesystem.copy_to_local("/tmp/out.tar.gz", tmp.name)
            with tarfile.open(tmp.name, mode="r:gz") as tf:
                tf.extractall(wd, filter="data")

    try:
        _safe_run(
            do_pull,
            timeout_s=_sb_exec_deadline(TRANSFER_TIMEOUT_S),
            sb=sb,
            label="pull_wd",
        )
    except ComputeError:
        # A stall / mid-pull Sandbox loss is already a typed ComputeError; let it ride.
        raise
    except Exception as exc:
        # The copy_to_local / tar extract failed: retitle it as a transfer failure so
        # the run records ``TransferError`` rather than a leaked Modal/tar class.
        raise TransferError(f"pull_wd: {exc}") from exc


def _pull_stderr(sb: modal.Sandbox) -> str:
    """Read back the captured ``modal_stderr.txt``."""
    if _is_dead(sb):
        log.warning("pull_stderr: Sandbox already terminated; skipping pull")
        return ""

    def read() -> str:
        with tempfile.NamedTemporaryFile(suffix=".txt") as tmp:
            sb.filesystem.copy_to_local(f"{WORKDIR_MOUNT}/modal_stderr.txt", tmp.name)
            return Path(tmp.name).read_text(errors="replace")

    return _safe_run(read, timeout_s=TRANSFER_TIMEOUT_S, sb=sb, label="pull_stderr")


class ModalBackend(ComputeBackend):
    """Run sandboxes as Modal Sandboxes, pushing the workdir up and pulling it back.

    Unlike the bind-mounted Docker backend, the workdir is synced into the remote
    Sandbox and synced back out on completion. Construction is offline -- it just
    records its knobs; Modal is only contacted when a command runs.

    Parameters
    ----------
    image : Image
        The sandbox image carrying Lean + Mathlib -- its tag plus the toolchain and
        Mathlib revision the verifier checks projects against. Default
        :data:`~open_atp.images.DEFAULT_IMAGE`. A mapping is coerced to an
        :class:`~open_atp.images.Image` (so a parsed config's nested ``image:`` block
        works).
    env : Mapping[str, str], optional
        Environment variables baked into every command run in the sandbox. Default
        empty.
    cpu : float
        CPU cores requested for the Modal Sandbox. Default ``2.0``.
    memory_mib : int
        Memory (MiB) requested for the Modal Sandbox. Default ``4096``.
    app : str
        Modal app the Sandbox is associated with (also the publish target of
        ``open-atp build-modal-image``). Default ``open-atp``.
    region : str or Sequence[str], optional
        Region(s) to schedule sandboxes in (see Modal region selection docs).
        Default ``"us"``; ``None`` lets Modal choose freely.

    Examples
    --------

    >>> from open_atp.backends.modal import ModalBackend
    >>> from open_atp.images import DEFAULT_IMAGE
    >>> backend = ModalBackend(image=DEFAULT_IMAGE, cpu=4.0)
    >>> backend.name
    'modal'
    >>> backend.cpu
    4.0
    >>> backend.app
    'open-atp'
    >>> backend.region
    'us'
    """

    # Modal ignores the image USER and runs everything as root; credential mounts
    # land under root's HOME.
    container_home = "/root"

    def __init__(
        self,
        *,
        image: Image | Mapping[str, object] = DEFAULT_IMAGE,
        env: Mapping[str, str] | None = None,
        cpu: float = 2.0,
        memory_mib: int = 4096,
        app: str = "open-atp",
        region: str | Sequence[str] | None = "us",
    ) -> None:
        super().__init__(image=image, env=env)
        self.cpu = cpu
        self.memory_mib = memory_mib
        self.app = app
        self.region = region

    @property
    def name(self) -> str:
        """Short identifier for the backend: ``"modal"``."""
        return "modal"

    @property
    def wallclock_overhead_s(self) -> int:
        """Modal Sandbox lifecycle overhead: file sync + warm build + teardown."""
        # This is a *rough* high water estimate of the wall-clock overhead to
        # prevent a Modal Sandbox from being reaped prematurely.
        return (
            TRANSFER_TIMEOUT_S  # push workdir (and mounts)
            + WARM_BUILD_TIMEOUT_S  # warm the Lean cache
            + TRANSFER_TIMEOUT_S  # pull workdir back out
            + 3 * TIMEOUT_KILL_AFTER_S  # buffer for coreutils timeout
            + 3 * EXEC_DEADLINE_MARGIN_S  # buffer for sb.exec client deadlines
        )

    def _provision(
        self,
        workdir: Path,
        timeout_s: int,
        env: Mapping[str, str] | None,
        mounts: Sequence[tuple[str, str]] | None,
    ) -> modal.Sandbox:
        """Create an idle Sandbox, push the workdir + mounts, warm the Lean cache.

        Shared by :meth:`run` (one command then teardown) and :meth:`session` (kept
        alive for many commands). On any failure the Sandbox is terminated before
        propagating so a failed provision never leaks compute. The push, warm build,
        and pull steps that follow are each bounded (liveness + wall-clock); the
        pre-Sandbox ``App.lookup``/``Sandbox.create`` RPCs are not -- no Sandbox
        exists yet to poll for liveness, and no failures have been observed in
        practice.
        """
        app = modal.App.lookup(self.app, create_if_missing=True)
        image = modal.Image.from_name(_modal_image_name(self.image.name))

        cpu = self.cpu
        secret_dict: dict[str, str | None] = {
            **self.env,
            **(env or {}),
            # Lets Claude Code's bypassPermissions run as root (Modal runs as root).
            "IS_SANDBOX": "1",
            # Pin Lean's thread pool to the reserved cores so it can't oversubscribe.
            "LEAN_NUM_THREADS": str(max(1, int(cpu))),
        }
        secret = modal.Secret.from_dict(secret_dict)

        # Idle Sandbox with no main process: we must push the workdir before exec.
        # Give the Sandbox SYNC_HEADROOM_S beyond the work budget: commands are
        # coreutils-capped at the budget (see _exec_payload), so the Sandbox outlives
        # a timed-out command and the partial workdir can still be pulled back.
        log.debug(
            "provisioning modal sandbox",
            extra={
                "image": self.image.name,
                "cpu": cpu,
                "memory_mib": self.memory_mib,
                "timeout_s": timeout_s,
                "region": self.region,
            },
        )
        started_at = time.time()
        sb = modal.Sandbox.create(
            app=app,
            image=image,
            secrets=[secret],
            cpu=cpu,
            memory=self.memory_mib,
            timeout=timeout_s + self.wallclock_overhead_s,
            region=self.region,
        )
        try:
            # Push the workdir, then each extra (host, container) mount -- replaces
            # Docker's bind mounts (workdir + credential dirs).
            _push_dir(sb, workdir, WORKDIR_MOUNT)
            for host, dest in mounts or ():
                _push_dir(sb, Path(host), dest)

            # Warm the Lean cache (and wire the .lake symlink) before timing real
            # work: Modal pages image layers in lazily, so the first build is slow.
            log.debug("warming lean cache", extra={"workdir": WORKDIR_MOUNT})
            _safe_run(
                lambda: _sb_exec(
                    sb,
                    f"ln -sfn {BAKED_LAKE} {WORKDIR_MOUNT}/.lake && lake build",
                    WARM_BUILD_TIMEOUT_S,
                    workdir=WORKDIR_MOUNT,
                ).wait(),
                timeout_s=_sb_exec_deadline(WARM_BUILD_TIMEOUT_S),
                sb=sb,
                label="warm_build",
            )
        except BaseException:
            log.error("modal sandbox provisioning failed", exc_info=True)
            _terminate(sb)
            raise
        log.debug(
            "modal sandbox ready",
            extra={"duration_s": round(time.time() - started_at, 1)},
        )
        return sb

    def session(
        self,
        workdir: Path,
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
    ) -> ComputeSession:
        """Provision a Sandbox over ``workdir`` and keep it alive for many execs.

        The Sandbox lives until :meth:`ModalSession.close`; ``env``/``mounts`` pin it
        at creation.

        Parameters
        ----------
        workdir : pathlib.Path
            Host directory pushed into the Sandbox at creation; bridge it back with
            :meth:`ModalSession.sync_out`.
        timeout_s : int
            Wall-clock time for the Modal Sandbox session to live, in seconds.
        env : Mapping[str, str], optional
            Environment variables pinned on the Sandbox at creation, merged over the
            backend's :attr:`env`. Default empty.
        mounts : Sequence[tuple[str, str]], optional
            Extra ``(host_path, container_path)`` dirs pushed in at creation. Default
            empty.

        Returns
        -------
        ComputeSession
            A live :class:`ModalSession` over the workdir.
        """
        sb = self._provision(
            workdir=workdir, timeout_s=timeout_s, env=env, mounts=mounts
        )
        return ModalSession(backend=self, sb=sb, workdir=workdir)


@dataclass
class ModalSession(ComputeSession):
    """A live Modal Sandbox: exec many commands over the pushed workdir, terminate once.

    The filesystem is isolated, so :meth:`sync_out` (tar pull) and :meth:`sync_in`
    (tar push) bridge host<->Sandbox when a caller needs the host workdir current
    between commands (e.g. a host-side diff, or Numina's statement tracker).
    """

    backend: ModalBackend
    sb: modal.Sandbox
    workdir: Path

    def exec(
        self,
        command: str,
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandHandle:
        """Exec ``command`` in the live Sandbox; close() owns teardown.

        Parameters
        ----------
        command : str
            The shell command to run in the live Sandbox.
        timeout_s : int
            Wall-clock cap for this command, in seconds.
        env : Mapping[str, str], optional
            Per-command environment variables, forwarded as a one-off Modal secret
            (usually empty -- credentials pin at session creation). Default empty.

        Returns
        -------
        CommandHandle
            A live :class:`ModalCommandHandle` whose ``wait`` leaves the Sandbox up for
            the next command.
        """
        # Per-command env is rare (the agent's creds are pinned at session create), so
        # this is usually an empty secret list.
        secrets = [modal.Secret.from_dict(dict(env))] if env else []
        started_at = time.time()
        log.debug(
            "modal exec (session)",
            extra={
                "command": command,
                "workdir": WORKDIR_MOUNT,
                "env_keys": sorted(env or {}),
                "timeout_s": timeout_s,
            },
        )
        proc = _sb_exec(
            self.sb,
            wrap_command(command),
            timeout_s,
            workdir=WORKDIR_MOUNT,
            secrets=secrets,
            capture_io=True,
        )
        return ModalCommandHandle(
            proc=proc,
            sb=self.sb,
            started_at=started_at,
            deadline_s=_sb_exec_deadline(timeout_s),
        )

    def sync_out(self) -> None:
        """Tar the Sandbox workdir back out over the host ``workdir``."""
        _pull_wd(self.sb, self.workdir)

    def sync_in(self) -> None:
        """Tar the host ``workdir`` up into the Sandbox."""
        _push_dir(self.sb, self.workdir, WORKDIR_MOUNT)

    def close(self) -> None:
        """Pull final artifacts best-effort, then terminate the Sandbox. Idempotent.

        The result-bearing pull is :meth:`sync_out` (which the provers call
        explicitly, and which raises on failure); this teardown pull is a redundant
        backstop and also runs after a run has already failed, so a failure here is
        logged and swallowed rather than masking the real outcome. Termination always
        runs.
        """
        try:
            _pull_wd(self.sb, self.workdir)
        except ComputeError:
            log.warning("close: final workdir pull failed; terminating anyway")
        finally:
            _terminate(self.sb)
