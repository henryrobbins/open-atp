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

import io
import logging
import shlex
import tarfile
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from open_atp.backends.base import (
    CommandHandle,
    CommandResult,
    ComputeBackend,
    ComputeSession,
    wrap_command,
)
from open_atp.images import DEFAULT_IMAGE, Image

if TYPE_CHECKING:
    import modal
    from modal.container_process import ContainerProcess

log = logging.getLogger(__name__)

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
#: dropping every edit the agent made.
SYNC_HEADROOM_S = 180


def _require_modal() -> None:
    """Validate that ``modal`` is importable, with an actionable error if not."""
    try:
        import modal  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "the modal compute backend requires the `modal` package; "
            "install it with `pip install open-atp` (modal is a core dependency)"
        ) from exc


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
    _stdout_lines: list[str] = field(default_factory=list)
    _buf: str = ""

    def stream(self) -> Iterator[str]:
        """Yield stdout split into lines, re-buffering Modal's arbitrary chunks."""
        # Modal's StreamReader defaults to by_line=False, so iterating proc.stdout
        # yields arbitrary chunks (a chunk may hold several JSON objects, or split
        # one). Re-buffer and split on newlines so we honour the line-delimited
        # JSONL contract the harness parsers rely on (json.loads per line) -- and
        # so a chunked `result` event is never lost (would zero cost/tokens).
        for chunk in self.proc.stdout:
            self._buf += chunk
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._stdout_lines.append(line)
                yield line

    def _flush(self) -> None:
        """Drain any unread chunks, then emit the final newline-less partial line."""
        for chunk in self.proc.stdout:
            self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._stdout_lines.append(line)
        if self._buf:
            self._stdout_lines.append(self._buf)
            self._buf = ""

    def cancel(self) -> None:
        """Pull partial artifacts (best effort), then terminate the Sandbox."""
        # No per-exec kill on a Sandbox, so pull partial artifacts (best effort) and
        # then terminate -- terminating is the authoritative kill and also ensures we
        # never leak compute. Idempotent: safe to call after wait() already tore down.
        _pull_wd(self.sb, self.workdir)
        _terminate(self.sb)

    def wait(self) -> CommandResult:
        """Flush stdout, pull the workdir + stderr back, then terminate the Sandbox."""
        self._flush()
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
        """Flush stdout and read stderr, leaving the Sandbox up for the session."""
        self._flush()
        exit_code = self.proc.wait()
        stderr = _pull_stderr(self.sb)
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

    A failure here silently drops *all* of a run's generation output (edits +
    usage files), so don't collapse every cause into one opaque message: check the
    tar exit code and log the real exception with a traceback. The distinct causes
    (dead sandbox vs. a non-zero ``tar`` on some file the agent created vs. a
    missing tarball) need different fixes.
    """
    try:
        proc = sb.exec(
            "bash",
            "-c",
            f"tar -czf /tmp/out.tar.gz -C {REMOTE_WD} --exclude=./.lake .",
        )
        # tar exits non-zero on e.g. a file that changed/vanished mid-archive; surface
        # it rather than letting the (possibly partial/missing) copy fail opaquely.
        exit_code = proc.wait()
        if exit_code != 0:
            stderr = "".join(proc.stderr).strip() or "(no stderr)"
            log.warning("pull_wd: tar exited %s: %s", exit_code, stderr)
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
            sb.filesystem.copy_to_local("/tmp/out.tar.gz", tmp.name)
            with tarfile.open(tmp.name, mode="r:gz") as tf:
                tf.extractall(wd, filter="data")
    except Exception:
        log.warning("pull_wd: failed to pull artifacts from %s", wd, exc_info=True)


def _pull_stderr(sb: modal.Sandbox) -> str:
    """Read back the captured ``modal_stderr.txt`` (empty if unavailable)."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".txt") as tmp:
            sb.filesystem.copy_to_local(f"{REMOTE_WD}/modal_stderr.txt", tmp.name)
            return Path(tmp.name).read_text(errors="replace")
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
    ) -> None:
        super().__init__(image=image, env=env)
        self.cpu = cpu
        self.memory_mib = memory_mib
        self.app = app

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
        propagating so a failed provision never leaks compute.
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
        sb = modal.Sandbox.create(
            app=app,
            image=image,
            secrets=[secret],
            cpu=cpu,
            memory=self.memory_mib,
            timeout=timeout_s + SYNC_HEADROOM_S,
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
        except BaseException:
            _terminate(sb)
            raise
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
            # Cap the command a headroom below the Sandbox timeout so a timed-out run
            # is killed while the Sandbox is still alive to pull the workdir back.
            proc = sb.exec(
                "bash",
                "-c",
                _exec_payload(self._wrap(command), timeout_s),
                workdir=REMOTE_WD,
            )
        except BaseException:
            # Launch failed after provision; release the Sandbox before propagating.
            _terminate(sb)
            raise
        return ModalCommandHandle(
            proc=proc, sb=sb, workdir=workdir, started_at=started_at
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
        proc = self.sb.exec(
            "bash",
            "-c",
            _exec_payload(self.backend._wrap(command), timeout_s),
            workdir=REMOTE_WD,
            secrets=secrets,
        )
        return ModalSessionHandle(
            proc=proc, sb=self.sb, workdir=self.workdir, started_at=started_at
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
