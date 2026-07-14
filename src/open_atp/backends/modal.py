"""Modal Sandbox backend.

Runs an arbitrary command over a workdir against the
:meth:`ComputeBackend.start` contract.

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
* Cap generation with coreutils ``timeout`` *below* the Sandbox's own lifetime (which
  carries :data:`SYNC_HEADROOM_S` of slack), so a timed-out agent is killed while the
  Sandbox is still alive to pull the partial workdir back -- rather than Modal
  terminating the whole Sandbox mid-run and losing every edit.
* Never leak a Sandbox: terminate on a failed start and after every run.
"""

from __future__ import annotations

import contextvars
import io
import logging
import shlex
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from open_atp.backends.base import (
    CommandHandle,
    CommandResult,
    ComputeBackend,
    ComputeError,
    ComputeSession,
    ExecTimeout,
    SandboxUnreachable,
    wrap_command,
)
from open_atp.images import DEFAULT_IMAGE, Image

if TYPE_CHECKING:
    import modal
    from modal.container_process import ContainerProcess

log = logging.getLogger("open_atp")

#: Working directory inside the Sandbox (same convention as Docker's bind mount).
REMOTE_WD = "/workspace/wd"
#: Image-baked warm Mathlib olean cache to symlink the workdir's ``.lake`` to.
BAKED_LAKE = "/workspace/.lake"

#: Extra Sandbox lifetime (seconds) reserved purely for the final tar pull. The
#: caller's ``timeout_s`` already budgets the *work* (generation + the in-session
#: verify); the Sandbox is created with ``timeout_s + SYNC_HEADROOM_S`` so that after
#: the last command hits its coreutils ``timeout``, the Sandbox is still alive long
#: enough to tar the partial workdir back out. Without it, the Sandbox timeout and the
#: work budget coincide and the pull races a Sandbox Modal has already terminated --
#: dropping every edit the agent made. Kept above the coreutils ``--kill-after=30``
#: grace (2x it): a command that runs to budget and ignores SIGTERM isn't dead until
#: ``budget + 30``, so anything <= 30 would leave no window to pull its partial workdir.
SYNC_HEADROOM_S = 60

#: Seconds a command's *client* deadline sits above its in-sandbox coreutils cap. The
#: coreutils ``timeout`` (which does the clean group-kill + leaves the Sandbox alive
#: for the partial pull) should always fire first; this margin is the backstop reached
#: only if the worker stops talking. Covers the ``--kill-after=30`` grace + RPC slack.
EXEC_DEADLINE_MARGIN_S = 60
#: Client wall-clock ceiling for the quick provisioning/teardown execs -- the push
#: untar, the pull tar -- which are size-independent shell one-liners, not the build
#: itself. Generous -- it only bounds a genuine stall, never legitimate work.
INFRA_EXEC_TIMEOUT_S = 60
#: Client wall-clock ceiling for the warm ``lake build``, which -- unlike the other
#: infra execs -- scales with project size, so it keeps a much wider margin.
WARM_BUILD_TIMEOUT_S = 600
#: Client wall-clock ceiling for opaque filesystem transfers (``copy_to_local`` /
#: ``copy_from_local``), which expose no deadline knob of their own.
TRANSFER_TIMEOUT_S = 60
#: Client wall-clock ceiling for ``Sandbox.terminate`` (best effort).
TERMINATE_TIMEOUT_S = 30
#: Grace window for the best-effort stdout drain *after* a process has exited: enough
#: to collect buffered output, short enough that an orphan-held pipe can't stall us.
DRAIN_GRACE_S = 15
#: How often :func:`_run_bounded` re-checks Sandbox liveness while blocked.
LIVENESS_POLL_INTERVAL_S = 5


def _require_modal() -> None:
    """Validate that ``modal`` is importable, with an actionable error if not."""
    try:
        import modal  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "the modal compute backend requires the `modal` package; "
            "install it with `pip install open-atp` (modal is a core dependency)"
        ) from exc


def _run_bounded[T](
    fn: Callable[[], T],
    *,
    timeout_s: float | None = None,
    sb: modal.Sandbox | None = None,
    label: str,
) -> T:
    """Run blocking ``fn()`` in a thread, bounded by liveness and a wall-clock.

    Modal's blocking calls (stream reads, ``wait``, filesystem transfers, ``create``)
    have no client deadline, so a dead/unreachable worker hangs them forever. This
    wrapper runs ``fn`` on a daemon thread and, while it is in flight:

    * raises :class:`~open_atp.backends.base.SandboxUnreachable` as soon as ``sb``
      (when given) is reaped -- via ``sb.poll()``, a control-plane
      ``SandboxWait(timeout=0)`` that does **not** traverse the worker command-router
      path, so it still answers when the *worker* is the thing that has gone
      unreachable -- the fast path, seen via the control plane, not the stuck worker;
    * raises :class:`~open_atp.backends.base.ExecTimeout` at ``timeout_s`` (when given)
      as the backstop.

    ``fn`` runs inside the calling thread's ``contextvars`` context (a plain
    ``threading.Thread`` does not copy it by default), so structured-logging fields
    bound via ``structlog.contextvars`` (prover, run id, task) still attach to any
    logging ``fn`` itself does. ``fn``'s own return value is returned and its own
    exception re-raised. On abort the daemon thread is abandoned (it holds a doomed
    Sandbox that Modal will GC); the caller is never blocked on it.

    Every abort emits a ``warning`` event tagged with ``label``, the observed
    ``elapsed_s``, and the ``timeout_s`` ceiling it ran under -- so ``elapsed_s`` read
    against the ceiling shows real per-call latency and headroom (control-plane RPCs,
    transfers, exec waits) when right-sizing the wall-clock constants above. Phases
    *inside* a bounded call (transfer vs. untar) are timed separately by :func:`_timed`.
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
        if sb is not None and sb.poll() is not None:
            log.warning(
                "modal call aborted",
                extra={
                    "label": label,
                    "reason": "sandbox_dead",
                    "elapsed_s": round(time.monotonic() - started, 1),
                },
            )
            raise SandboxUnreachable("Sandbox terminated while awaiting a Modal call")
        if timeout_s is not None and waited >= timeout_s:
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
        log.debug(
            "modal call raised",
            extra={
                "label": label,
                "elapsed_s": round(time.monotonic() - started, 2),
                "timeout_s": timeout_s,
                "error": type(error[0]).__name__,
            },
        )
        raise error[0]
    return result[0]


def _timed[T](label: str, fn: Callable[[], T]) -> T:
    """Time one *phase* inside an already-bounded call; emit a debug event.

    A lighter companion to :func:`_run_bounded` (no thread, no liveness -- the enclosing
    ``_run_bounded`` still supplies the safety bound). It splits an opaque combined
    bound into representative per-phase latencies -- the filesystem transfer vs. the
    untar/tar exec inside a push/pull -- so :data:`TRANSFER_TIMEOUT_S` and
    :data:`INFRA_EXEC_TIMEOUT_S` can each be sized from real numbers on a full-size
    workdir rather than one merged total (or only the tiny ``pull_stderr`` transfer).
    """
    started = time.monotonic()
    try:
        return fn()
    finally:
        log.debug(
            "modal phase",
            extra={"label": label, "elapsed_s": round(time.monotonic() - started, 2)},
        )


def _raise_stream_error(exc: Exception, sb: modal.Sandbox) -> NoReturn:
    """Translate a stdout-stream failure into a typed :class:`ComputeError`.

    Modal raises ``ExecTimeoutError`` when the stream's own deadline lapses; a reaped
    or unreachable Sandbox drops the stream with a variety of errors, so rather than
    match each one we consult the control plane (``sb.poll()``). Map both to our
    vocabulary so the caller can classify the run; re-raise anything else.
    """
    from modal.exception import ExecTimeoutError

    if isinstance(exc, ExecTimeoutError):
        raise ExecTimeout(
            "exec exceeded its client deadline while streaming stdout"
        ) from exc
    if sb.poll() is not None:
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


def _exec_payload(wrapped: str, timeout_s: int | None) -> str:
    """Build the ``bash -c`` argument for a Sandbox exec: cap, stdin, stderr.

    ``timeout_s`` (> 0) caps ``wrapped`` with coreutils ``timeout``, which -- run
    without ``--foreground`` -- signals the command's whole process group, so the
    agent *and* its children are killed when the budget expires (in time for the
    Sandbox's :data:`SYNC_HEADROOM_S` pull window). Each command the caller runs
    (generation, then the in-session verify) passes its own slice of the budget.
    ``None`` leaves it uncapped, for a caller that manages its own timeout.

    Stdin is ``/dev/null`` (Modal leaves it an open pipe with no EOF, hanging agent
    CLIs) and stderr is captured to a file pulled back with the workdir.
    """
    if timeout_s is not None and timeout_s > 0:
        body = f"timeout --kill-after=30 {timeout_s} bash -c {shlex.quote(wrapped)}"
    else:
        body = f"{{ {wrapped} ; }}"
    return f"{body} < /dev/null 2> {REMOTE_WD}/modal_stderr.txt"


def _exec_deadline(timeout_s: int | None) -> int | None:
    """The Modal ``exec_deadline`` (seconds) backstopping a command's client wait.

    Sits above the client wall-clock (which itself sits above the in-sandbox coreutils
    cap) so :func:`_run_bounded` fails with a clean typed error first; this is only the
    last resort, so an abandoned ``wait`` thread can't outlive the exec forever.
    ``None`` (an uncapped command) leaves the exec uncapped too.
    """
    if timeout_s is None:
        return None
    return timeout_s + 2 * EXEC_DEADLINE_MARGIN_S


def _modal_image_name(image: str) -> str:
    """Map an image ref to a Modal named-image lookup.

    The platform shares one ``image`` string across backends and it carries a
    Docker-style ``:tag`` (e.g. ``open-atp:latest``). Modal published images are
    looked up by bare name (``modal.Image.from_name`` takes no tag and Modal names
    can't contain ``:``), so strip a trailing tag.
    """
    return image.rsplit(":", 1)[0]


@dataclass
class ModalCommandHandle(CommandHandle):
    """Live handle for a command running in a Modal Sandbox."""

    proc: ContainerProcess[str]
    sb: modal.Sandbox
    workdir: Path
    started_at: float
    #: Client wall-clock the process wait is bounded by (``None`` = liveness-only).
    deadline_s: float | None = None
    _stdout_lines: list[str] = field(default_factory=list)
    _buf: str = ""

    def stream(self) -> Iterator[str]:
        """Yield stdout split into lines, re-buffering Modal's arbitrary chunks."""
        # Modal's StreamReader defaults to by_line=False, so iterating proc.stdout
        # yields arbitrary chunks (a chunk may hold several JSON objects, or split
        # one). Re-buffer and split on newlines so we honour the line-delimited
        # JSONL contract the harness parsers rely on (json.loads per line) -- and
        # so a chunked `result` event is never lost (would zero cost/tokens).
        try:
            for chunk in self.proc.stdout:
                self._buf += chunk
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    self._stdout_lines.append(line)
                    yield line
        except Exception as exc:
            _raise_stream_error(exc, self.sb)

    def _drain_remaining(self) -> None:
        """Best-effort: collect buffered stdout after the process has exited.

        Bounded by :data:`DRAIN_GRACE_S`: once the process is gone, any remaining real
        output is already buffered and arrives fast, but an orphaned child holding the
        stdout pipe open means EOF may never come -- so we take what's there and stop
        rather than block (the case-1 hang).
        """

        def pull() -> None:
            for chunk in self.proc.stdout:
                self._buf += chunk

        try:
            _run_bounded(pull, timeout_s=DRAIN_GRACE_S, sb=self.sb, label="drain")
        except ComputeError:
            log.warning("drain: stdout did not close after exit; using partial output")
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._stdout_lines.append(line)
        if self._buf:
            self._stdout_lines.append(self._buf)
            self._buf = ""

    def _collect_result(self) -> CommandResult:
        """Wait for the process to exit (bounded), then drain stdout + stderr.

        Waiting on the *process* first (not stdout EOF) is the fast-fail: ``proc.wait``
        returns when the command exits -- independent of whether an orphaned child is
        still holding the stdout pipe open -- so a budget-killed command returns at its
        budget instead of blocking on an EOF that never arrives.
        """
        exit_code = _run_bounded(
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

    def cancel(self) -> None:
        """Pull partial artifacts (best effort), then terminate the Sandbox."""
        # No per-exec kill on a Sandbox, so pull partial artifacts (best effort) and
        # then terminate -- terminating is the authoritative kill and also ensures we
        # never leak compute. Idempotent: safe to call after wait() already tore down.
        _pull_wd(self.sb, self.workdir)
        _terminate(self.sb)

    def wait(self) -> CommandResult:
        """Collect the result, pull the workdir back, then terminate the Sandbox."""
        try:
            return self._collect_result()
        finally:
            _pull_wd(self.sb, self.workdir)
            _terminate(self.sb)


@dataclass
class ModalSessionHandle(ModalCommandHandle):
    """A command exec'd into a live :class:`ModalSession`.

    Same stdout/stderr draining as the one-shot handle, but :meth:`wait` does NOT
    ``_pull_wd``/``_terminate`` and :meth:`cancel` is a no-op: the session owns
    teardown (:meth:`ModalSession.close`), so a finished exec must leave the Sandbox
    up for the next command (and must not leave it leaked either -- that is close's
    job, guaranteed by the session context manager).
    """

    def cancel(self) -> None:
        """No-op: the owning session, not the handle, terminates the Sandbox."""

    def wait(self) -> CommandResult:
        """Collect the result, leaving the Sandbox up for the session."""
        return self._collect_result()


def _terminate(sb: modal.Sandbox) -> None:
    """Tear down the Sandbox; idempotent (safe if it is already gone)."""
    try:
        _run_bounded(
            lambda: sb.terminate(), timeout_s=TERMINATE_TIMEOUT_S, label="terminate"
        )
    except Exception:
        log.debug("terminate: Sandbox already gone or unreachable")


def _push_dir(sb: modal.Sandbox, src: Path, dest: str) -> None:
    """Tar ``src`` and extract it into ``dest`` inside the Sandbox.

    Bounded by liveness + a wall-clock: an unreachable worker would otherwise hang the
    copy or the untar exec indefinitely (neither the transfer nor a plain ``.wait()``
    has a client deadline of its own). A failure here aborts provisioning.
    """
    log.debug("pushing dir into sandbox", extra={"src": str(src), "dest": dest})

    def do_push() -> None:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
            tmp.write(_tar_dir(src))
            tmp.flush()
            _timed(
                "push_copy",
                lambda: sb.filesystem.copy_from_local(tmp.name, "/tmp/push.tar.gz"),
            )
        _timed(
            "push_untar",
            lambda: sb.exec(
                "bash",
                "-c",
                f"mkdir -p {dest} && tar -xzf /tmp/push.tar.gz -C {dest}",
                timeout=INFRA_EXEC_TIMEOUT_S,
            ).wait(),
        )

    _run_bounded(
        do_push,
        timeout_s=INFRA_EXEC_TIMEOUT_S + TRANSFER_TIMEOUT_S,
        sb=sb,
        label="push_dir",
    )


def _pull_wd(sb: modal.Sandbox, wd: Path) -> None:
    """Tar the Sandbox workdir back out and extract it over the host ``wd``.

    Excludes the huge image-baked ``.lake`` symlink target. For the verify path no
    files change so this is just the stderr/log; for generation it is how edits
    return -- one code path that always pulls (cheap for small projects).

    Best effort: a failure here must not mask the run's real outcome, so it is caught
    and logged, never raised. It first gates on ``sb.poll()`` (a reaped Sandbox can
    never be pulled -- fail instantly rather than stall), then bounds the tar +
    transfer. Distinct causes (dead sandbox vs. a non-zero ``tar`` on some file the
    agent created vs. a missing tarball) get distinct messages so they get distinct
    fixes.
    """
    try:
        dead = sb.poll() is not None
    except Exception:
        log.warning(
            "pull_wd: failed to pull artifacts", extra={"wd": str(wd)}, exc_info=True
        )
        return
    if dead:
        log.warning(
            "pull_wd: Sandbox already terminated; skipping pull",
            extra={"wd": str(wd)},
        )
        return

    def do_pull() -> None:
        proc = sb.exec(
            "bash",
            "-c",
            f"tar -czf /tmp/out.tar.gz -C {REMOTE_WD} --exclude=./.lake .",
            timeout=INFRA_EXEC_TIMEOUT_S,
        )
        # tar exits non-zero on e.g. a file that changed/vanished mid-archive; surface
        # it rather than letting the (possibly partial/missing) copy fail opaquely.
        exit_code = _timed("pull_tar", proc.wait)
        if exit_code != 0:
            stderr = "".join(proc.stderr).strip() or "(no stderr)"
            log.warning(
                "pull_wd: tar exited nonzero",
                extra={"exit_code": exit_code, "stderr": stderr},
            )
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
            _timed(
                "pull_copy",
                lambda: sb.filesystem.copy_to_local("/tmp/out.tar.gz", tmp.name),
            )
            with tarfile.open(tmp.name, mode="r:gz") as tf:
                tf.extractall(wd, filter="data")

    try:
        _run_bounded(
            do_pull,
            timeout_s=INFRA_EXEC_TIMEOUT_S + TRANSFER_TIMEOUT_S,
            sb=sb,
            label="pull_wd",
        )
    except Exception:
        log.warning(
            "pull_wd: failed to pull artifacts", extra={"wd": str(wd)}, exc_info=True
        )


def _pull_stderr(sb: modal.Sandbox) -> str:
    """Read back the captured ``modal_stderr.txt`` (empty if unavailable).

    Best effort and liveness-gated: a dead or unreachable Sandbox yields ``""`` rather
    than hanging the caller on a transfer that will never complete.
    """
    try:
        if sb.poll() is not None:
            return ""
    except Exception:
        return ""

    def read() -> str:
        with tempfile.NamedTemporaryFile(suffix=".txt") as tmp:
            sb.filesystem.copy_to_local(f"{REMOTE_WD}/modal_stderr.txt", tmp.name)
            return Path(tmp.name).read_text(errors="replace")

    try:
        return _run_bounded(
            read, timeout_s=TRANSFER_TIMEOUT_S, sb=sb, label="pull_stderr"
        )
    except Exception:
        return ""


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

    def _wrap(self, command: str) -> str:
        """cd into the workdir and wire the warm Mathlib cache before ``command``."""
        return wrap_command(REMOTE_WD, BAKED_LAKE, command)

    def _provision(
        self,
        workdir: Path,
        timeout_s: int,
        env: Mapping[str, str] | None,
        mounts: Sequence[tuple[str, str]] | None,
    ) -> modal.Sandbox:
        """Create an idle Sandbox, push the workdir + mounts, warm the Lean cache.

        Shared by :meth:`start` (one command then teardown) and :meth:`session` (kept
        alive for many commands). On any failure the Sandbox is terminated before
        propagating so a failed provision never leaks compute. The push, warm build,
        and pull steps that follow are each bounded (liveness + wall-clock); the
        pre-Sandbox ``App.lookup``/``Sandbox.create`` RPCs are not -- no Sandbox
        exists yet to poll for liveness, and no failures have been observed in
        practice.
        """
        import modal

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
            timeout=timeout_s + SYNC_HEADROOM_S,
            region=self.region,
        )
        try:
            # Push the workdir, then each extra (host, container) mount -- replaces
            # Docker's bind mounts (workdir + credential dirs).
            _push_dir(sb, workdir, REMOTE_WD)
            for host, dest in mounts or ():
                _push_dir(sb, Path(host), dest)

            # Warm the Lean cache (and wire the .lake symlink) before timing real
            # work: Modal pages image layers in lazily, so the first build is slow.
            log.debug("warming lean cache", extra={"workdir": REMOTE_WD})
            _run_bounded(
                lambda: sb.exec(
                    "bash",
                    "-c",
                    f"ln -sfn {BAKED_LAKE} {REMOTE_WD}/.lake && lake build",
                    workdir=REMOTE_WD,
                    timeout=WARM_BUILD_TIMEOUT_S,
                ).wait(),
                timeout_s=WARM_BUILD_TIMEOUT_S + EXEC_DEADLINE_MARGIN_S,
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

    def start(
        self,
        workdir: Path,
        command: str,
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        mounts: Sequence[tuple[str, str]] | None = None,
    ) -> CommandHandle:
        """Provision a Sandbox, push ``workdir`` in, and launch ``command`` in it.

        The handle pulls the workdir back out and terminates the Sandbox on
        :meth:`ModalCommandHandle.wait`/``cancel``.

        Parameters
        ----------
        workdir : pathlib.Path
            Host directory pushed into the Sandbox before launch and pulled back out on
            completion (so completed proofs land on the host).
        command : str
            The shell command to run inside the Sandbox.
        timeout_s : int
            Wall-clock cap for the command, in seconds.
        env : Mapping[str, str], optional
            Per-call environment variables, merged over the backend's :attr:`env`.
            Default empty.
        mounts : Sequence[tuple[str, str]], optional
            Extra ``(host_path, container_path)`` dirs pushed into the Sandbox (e.g.
            agent credential dirs). Default empty.

        Returns
        -------
        CommandHandle
            A live :class:`ModalCommandHandle` to stream or :meth:`~CommandHandle.wait`
            on.
        """
        _require_modal()
        sb = self._provision(
            workdir=workdir, timeout_s=timeout_s, env=env, mounts=mounts
        )
        try:
            started_at = time.time()
            log.debug(
                "modal exec",
                extra={
                    "command": command,
                    "workdir": REMOTE_WD,
                    "timeout_s": timeout_s,
                },
            )
            # Cap the command a headroom below the Sandbox timeout so a timed-out run
            # is killed while the Sandbox is still alive to pull the workdir back. The
            # exec also carries a client exec_deadline as a last-resort backstop.
            proc = sb.exec(
                "bash",
                "-c",
                _exec_payload(self._wrap(command), timeout_s),
                workdir=REMOTE_WD,
                timeout=_exec_deadline(timeout_s),
            )
        except BaseException:
            # Launch failed after provision; release the Sandbox before propagating.
            _terminate(sb)
            raise
        return ModalCommandHandle(
            proc=proc,
            sb=sb,
            workdir=workdir,
            started_at=started_at,
            deadline_s=timeout_s + EXEC_DEADLINE_MARGIN_S,
        )

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
        _require_modal()
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
        env: Mapping[str, str] | None = None,
        timeout_s: int | None = None,
    ) -> CommandHandle:
        """Exec ``command`` in the live Sandbox; close() owns teardown.

        Parameters
        ----------
        command : str
            The shell command to run in the live Sandbox.
        env : Mapping[str, str], optional
            Per-command environment variables, forwarded as a one-off Modal secret
            (usually empty -- credentials pin at session creation). Default empty.
        timeout_s : int, optional
            Wall-clock cap for this command, uncapped by default.

        Returns
        -------
        CommandHandle
            A live :class:`ModalSessionHandle` whose ``wait`` leaves the Sandbox up for
            the next command.
        """
        import modal

        # Per-command env is rare (the agent's creds are pinned at session create), so
        # this is usually an empty secret list.
        secrets = [modal.Secret.from_dict(dict(env))] if env else []
        started_at = time.time()
        log.debug(
            "modal exec (session)",
            extra={
                "command": command,
                "workdir": REMOTE_WD,
                "env_keys": sorted(env or {}),
                "timeout_s": timeout_s,
            },
        )
        proc = self.sb.exec(
            "bash",
            "-c",
            _exec_payload(self.backend._wrap(command), timeout_s),
            workdir=REMOTE_WD,
            secrets=secrets,
            timeout=_exec_deadline(timeout_s),
        )
        return ModalSessionHandle(
            proc=proc,
            sb=self.sb,
            workdir=self.workdir,
            started_at=started_at,
            deadline_s=(
                None if timeout_s is None else timeout_s + EXEC_DEADLINE_MARGIN_S
            ),
        )

    def sync_out(self) -> None:
        """Tar the Sandbox workdir back out over the host ``workdir``."""
        _pull_wd(self.sb, self.workdir)

    def sync_in(self) -> None:
        """Tar the host ``workdir`` up into the Sandbox."""
        _push_dir(self.sb, self.workdir, REMOTE_WD)

    def close(self) -> None:
        """Pull final artifacts, then terminate the Sandbox. Idempotent."""
        _pull_wd(self.sb, self.workdir)
        _terminate(self.sb)
