"""AristotleProver: a wrapper around Harmonic's Aristotle API.

No agentic sandbox is needed for generation -- we hand the lake project to the
hosted Aristotle agent via ``aristotlelib`` (submit -> wait -> download), unpack the
returned archive over the workdir, and let the shared verifier do the final check in
our own Docker sandbox. This is the platform's simplest end-to-end slice.

The remote interaction is isolated in :meth:`AristotleProver._submit_and_download`
so tests can stand in a fake result without touching the network or an API key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tarfile
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from open_atp._capture import capture_stdout
from open_atp.backends.base import ComputeBackend
from open_atp.lean import ProofTask
from open_atp.provers.base import (
    AutomatedProver,
    ProofResult,
    compose_prompt,
)

if TYPE_CHECKING:
    from aristotlelib import AgentTask, Project

log = logging.getLogger(__name__)

_T = TypeVar("_T")

PROVER_PROMPT = (
    "Complete every `sorry` in this Lean project. Make the project compile and be "
    "sorry-free without introducing new axioms; do not weaken or delete the stated "
    "theorems."
)
# END PROVER_PROMPT (docs literalinclude end marker -- keep adjacent)

# Directories never worth shipping to Aristotle / copying into the workdir.
_IGNORE = shutil.ignore_patterns(".lake", ".git", "*.tar.gz")


class _AristotleNoiseFilter(logging.Filter):
    """Drop the two expected-noise records aristotlelib logs on our headless path.

    We deliberately upload without ``.lake`` (the sandbox already has the warm
    Mathlib cache) and resume across dropped event streams ourselves, so
    aristotlelib's ".lake folder" warning and its "Connection to server was
    interrupted" error are both expected and redundant -- the full run record still
    syncs to the logs dir regardless.
    """

    _DROP = ("no .lake folder", "Connection to server was interrupted")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(needle in message for needle in self._DROP)


def _quiet_aristotle_logger() -> None:
    """Install :class:`_AristotleNoiseFilter` on the ``aristotle`` logger, once."""
    logger = logging.getLogger("aristotle")
    if not any(isinstance(f, _AristotleNoiseFilter) for f in logger.filters):
        logger.addFilter(_AristotleNoiseFilter())


def _is_transient(exc: BaseException) -> bool:
    """True for errors worth retrying: a dropped connection, timeout, or 5xx.

    aristotlelib turns httpx transport failures during a plain request into an
    ``AristotleAPIError`` with no status code (its ``RequestError`` wrapper) and
    leaves HTTP status errors with their code; a streamed run instead surfaces the
    raw ``httpx`` error. We treat transport-level failures and server-side 5xx as
    transient, but let real 4xx (bad key, missing project) fail fast.
    """
    import httpx
    from aristotlelib.api_request import AristotleAPIError

    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, AristotleAPIError):
        return exc.status_code is None or exc.status_code >= 500
    return False


class AristotleProver(AutomatedProver):
    """Prove by handing the whole project to Harmonic's hosted Aristotle agent.

    Generation happens over the network (submit the lake project, wait, download the
    result archive, unpack it over the workdir); the shared
    :class:`~open_atp.verify.Verifier` then runs the same local compile/sorry/axiom
    check. Generation is network-only, so the backend is used solely for that final
    check -- unlike the agentic provers, there is no live session to reuse.

    Parameters
    ----------
    backend : ComputeBackend
        The sandbox used only for the final verify; Aristotle generates over the
        network, so there is no live session to reuse.
    api_key : str, optional
        The Harmonic API key. ``None`` (default) reads it from the host
        ``ARISTOTLE_API_KEY`` env var.
    allow_agent_questions : bool
        Whether to let the hosted agent ask clarifying questions. Off by default:
        this is a headless API path and a prompt for stdin would hang the run.
    max_connection_retries : int
        Bounds per-call retries (list/refresh/download) when a connection drops.
        The hosted run lives server-side, so a dropped connection is recoverable:
        re-fetch rather than reporting the run failed. Default ``5``.
    max_resume_attempts : int
        Bounds *consecutive* event-stream reconnects that see no server-side progress
        before we stop waiting. Any progress (``percent_complete`` or
        ``last_updated_at`` advancing) resets it, so a healthy but long run reconnects
        without limit; this only caps a genuinely stuck task. Default ``10``.
    resume_backoff_seconds : float
        Initial sleep between retries/resumes, doubling (capped) between tries.
        Default ``5.0``.
    timeout_s : int
        Hard wall-clock cap on the generation wait, in seconds. When it elapses we
        stop waiting and proceed with whatever Aristotle has produced so far (the run
        keeps going and billing server-side regardless -- this only bounds the
        client). Default ``1800``.

    Attributes
    ----------
    prover_prompt : str
        The prover's own prompt handed to Aristotle, before any user prompt.

    Examples
    --------

    Construct the prover directly (network-only generation, so the backend is just
    the verify backend):

    >>> from open_atp.backends.docker import DockerBackend
    >>> from open_atp.provers.aristotle import AristotleProver
    >>> backend = DockerBackend()
    >>> prover = AristotleProver(backend=backend)
    >>> prover.name
    'aristotle'

    Or build the same prover from the standard catalog by name, taking its
    baked-in defaults (see :func:`~open_atp.config.standard_prover`):

    >>> from open_atp import standard_prover
    >>> prover = standard_prover("aristotle", backend=DockerBackend())
    >>> prover.name
    'aristotle'

    Complete a task's ``sorry``\\s with
    :meth:`~open_atp.provers.base.AutomatedProver.prove`, here on a bundled example
    (this hits the hosted Aristotle API, needing ``ARISTOTLE_API_KEY``, and runs
    Docker for the verify):

    >>> import tempfile
    >>> from open_atp.examples import EXAMPLE, example_task
    >>> task = example_task(EXAMPLE.ABS_MUL_LT)
    >>> result = prover.prove(task, tempfile.mkdtemp())  # doctest: +SKIP
    >>> result.success  # doctest: +SKIP
    True
    """

    name = "aristotle"

    def __init__(
        self,
        *,
        backend: ComputeBackend,
        api_key: str | None = None,
        allow_agent_questions: bool = False,
        max_connection_retries: int = 5,
        max_resume_attempts: int = 10,
        resume_backoff_seconds: float = 5.0,
        timeout_s: int = 1800,
    ) -> None:
        super().__init__(backend=backend, timeout_s=timeout_s)
        self._api_key = api_key
        self.allow_agent_questions = allow_agent_questions
        self.max_connection_retries = max_connection_retries
        self.max_resume_attempts = max_resume_attempts
        self.resume_backoff_seconds = resume_backoff_seconds

    @property
    def prover_prompt(self) -> str:
        """The prover's own prompt handed to Aristotle, before any user prompt."""
        return PROVER_PROMPT

    def _generate(
        self, task: ProofTask, wd: Path, logs_dir: Path, result: ProofResult
    ) -> None:
        # Stage the original project so the workdir is a complete project both for the
        # upload and, after extraction, for verification.
        shutil.copytree(task.project.root, wd, dirs_exist_ok=True, ignore=_IGNORE)

        original = {
            p.relative_to(task.project.root).as_posix(): p.read_text()
            for p in task.project.lean_files()
        }

        prompt = compose_prompt(self.prover_prompt, task.user_prompt)
        # The raw result archive and the full run record both belong with the run's
        # logs, not the proof project. The hosted agent has no live stdout stream, so
        # its record (events, transcript, summary) is downloaded here rather than teed.
        # aristotlelib prints a live progress display to stdout; capture it to
        # ``stdout.txt`` so a concurrent benchmark sweep stays readable.
        result_tar = logs_dir / "aristotle_result.tar.gz"
        with capture_stdout(logs_dir / "stdout.txt"):
            downloaded, metadata = asyncio.run(
                self._submit_and_download(wd, prompt, result_tar, logs_dir)
            )

        if downloaded is not None:
            self._extract_over(downloaded, wd)

        # Report the .lean files Aristotle changed or added.
        completed: dict[str, str] = {}
        for path in sorted(wd.rglob("*.lean")):
            if ".lake" in path.parts:
                continue
            rel = path.relative_to(wd).as_posix()
            content = path.read_text()
            if original.get(rel) != content:
                completed[rel] = content

        # The hosted agent's run summary is its primary human-readable record; surface
        # it beside the event record in the logs dir.
        summary_src = wd / "ARISTOTLE_SUMMARY.md"
        if summary_src.is_file():
            (logs_dir / "summary.md").write_text(summary_src.read_text())

        result.completed_files = completed
        # The Aristotle API does not expose a per-run cost; leave it unset.
        result.cost_usd = None
        result.metadata = metadata

    async def _submit_and_download(
        self, project_dir: Path, prompt: str, dest_tar: Path, logs_dir: Path
    ) -> tuple[Path | None, dict[str, object]]:
        """Submit ``project_dir`` to Aristotle, wait, and download the result archive.

        Also syncs the full run record (task metadata, every event, a readable
        transcript, project metadata) to ``logs_dir`` on the host.

        Returns ``(downloaded_tar_or_None, metadata)``. Isolated for testing.
        """
        import aristotlelib
        from aristotlelib import AgentQuestionsSetting, Project

        # We strip ``.lake`` before upload and resume dropped event streams ourselves;
        # silence aristotlelib's resulting expected-noise records on the console.
        _quiet_aristotle_logger()

        key = self._api_key or os.environ.get("ARISTOTLE_API_KEY")
        if key:
            aristotlelib.set_api_key(key)

        questions = (
            AgentQuestionsSetting.TIMEOUT_15_MIN
            if self.allow_agent_questions
            else AgentQuestionsSetting.DISABLED
        )

        project = await Project.create_from_directory(
            prompt=prompt, project_dir=project_dir, agent_questions_setting=questions
        )
        tasks, _ = await self._with_retry(
            lambda: project.get_tasks(limit=1), "list tasks"
        )
        metadata: dict[str, object] = {"project_id": project.project_id}
        if not tasks:
            metadata["error"] = "Aristotle returned no task to wait on."
            return None, metadata

        agent_task = tasks[0]
        # Resume across dropped connections until the task truly settles server-side,
        # but never past the wall-clock budget: on timeout, stop waiting and fall
        # through to download whatever Aristotle has produced so far.
        try:
            await asyncio.wait_for(
                self._wait_until_terminal(agent_task), timeout=self.timeout_s
            )
        except TimeoutError:
            log.warning(
                "aristotle: generation exceeded timeout_s=%ss; proceeding with "
                "whatever output is available",
                self.timeout_s,
            )
            metadata_timed_out = True
            # wait_for cancelled the wait mid-flight; re-fetch the true task state so
            # the recorded status/summary reflect where the run actually got to.
            await self._with_retry(agent_task.refresh, "refresh task status")
        else:
            metadata_timed_out = False
        await self._with_retry(project.refresh, "refresh project")

        metadata.update(
            task_id=agent_task.agent_task_id,
            task_status=agent_task.status.name,
            percent_complete=agent_task.percent_complete,
            output_summary=agent_task.output_summary,
            timed_out=metadata_timed_out,
        )

        # Sync the full run record to the host before returning. Best-effort: a hiccup
        # syncing logs must not discard an otherwise-good result.
        try:
            await self._sync_run_info(project, agent_task, logs_dir)
        except Exception:  # noqa: BLE001 -- logs are nice-to-have, not the result
            log.warning("aristotle: failed to sync run record", exc_info=True)
        metadata["logs_dir"] = str(logs_dir)

        if not project.has_files:
            metadata["error"] = "Aristotle produced no output files."
            return None, metadata

        await self._with_retry(
            lambda: project.get_files(destination=dest_tar), "download files"
        )
        return dest_tar, metadata

    async def _with_retry(self, op: Callable[[], Awaitable[_T]], what: str) -> _T:
        """Run an awaitable-returning ``op``, retrying transient connection failures.

        Backs off exponentially (capped) and re-raises once the retry budget is spent
        or the error is not transient, so a genuine bad key/4xx still fails fast.
        """
        delay = self.resume_backoff_seconds
        for attempt in range(1, self.max_connection_retries + 1):
            try:
                return await op()
            except Exception as exc:  # noqa: BLE001 -- re-raised unless transient
                if not _is_transient(exc) or attempt == self.max_connection_retries:
                    raise
                log.warning(
                    "aristotle: %s failed (%s); retrying (attempt %d/%d)",
                    what,
                    exc,
                    attempt,
                    self.max_connection_retries,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
        raise AssertionError("unreachable")  # loop either returns or raises

    async def _wait_until_terminal(self, agent_task: AgentTask) -> None:
        """Wait for the task to reach a terminal state, resuming across dropped links.

        aristotlelib's ``wait_for_completion`` swallows a dropped event stream and
        returns with a stale, still-running status while the task keeps going on the
        server. Treat any non-terminal status after it returns as a dropped connection,
        re-fetch the true state, and re-attach to the stream until the task actually
        settles.

        The stream has no client timeout (aristotlelib opens it with ``timeout=None``),
        so drops are server/proxy-side -- and Aristotle can work for many minutes
        without emitting an event, leaving the connection idle and prone to being cut.
        So ``max_resume_attempts`` bounds *consecutive resumes without progress*, not
        the run's lifetime: any forward progress (``percent_complete`` or
        ``last_updated_at`` advancing) resets the budget and the backoff. A healthy but
        long run thus reconnects indefinitely; we only give up once the task is
        genuinely stuck, and proceed with whatever output exists (the run dashboard is
        the source of truth).
        """
        from aristotlelib.agent_task import TaskStatus

        terminal = {
            TaskStatus.COMPLETE,
            TaskStatus.COMPLETE_WITH_ERRORS,
            TaskStatus.OUT_OF_BUDGET,
            TaskStatus.FAILED,
            TaskStatus.CANCELED,
        }

        def progress() -> tuple[object, object]:
            return (agent_task.percent_complete, agent_task.last_updated_at)

        delay = self.resume_backoff_seconds
        last_progress = progress()
        stalls = 0
        while stalls < self.max_resume_attempts:
            try:
                await agent_task.wait_for_completion()
            except Exception as exc:  # noqa: BLE001 -- re-raised unless transient
                if not _is_transient(exc):
                    raise
                log.debug("aristotle: wait interrupted (%s); resuming", exc)
            await self._with_retry(agent_task.refresh, "refresh task status")
            if agent_task.status in terminal:
                return

            if progress() != last_progress:
                # The run advanced server-side, so this drop is not a stall: reset the
                # budget and backoff and keep reconnecting for as long as it progresses.
                last_progress = progress()
                stalls = 0
                delay = self.resume_backoff_seconds
            else:
                stalls += 1
            # Expected: long-lived SSE streams get severed and we re-attach. Debug, not
            # warning, so a normal run with many reconnects stays quiet.
            log.debug(
                "aristotle: connection dropped with task still %s; resuming "
                "(stall %d/%d)",
                agent_task.status.name,
                stalls,
                self.max_resume_attempts,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
        log.warning(
            "aristotle: task %s still %s after %d resumes with no progress; proceeding "
            "with whatever output is available",
            agent_task.agent_task_id,
            agent_task.status.name,
            self.max_resume_attempts,
        )

    async def _sync_run_info(
        self, project: Project, agent_task: AgentTask, logs_dir: Path
    ) -> None:
        """Download the task's metadata and full event log to ``logs_dir``.

        Writes ``project.json``, ``task.json``, ``events.json`` (every event,
        oldest-first), and a human-readable ``transcript.txt``.
        """
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Page through every event, oldest-first, so the transcript reads top-down.
        events = []
        pagination_key = None
        while True:
            page, pagination_key = await self._with_retry(
                lambda: agent_task.get_events(
                    limit=100, pagination_key=pagination_key, newest_first=False
                ),
                "fetch events",
            )
            events.extend(page)
            if not pagination_key:
                break

        def _dump(obj: object) -> str:
            return json.dumps(obj, default=str, indent=2)

        (logs_dir / "project.json").write_text(_dump(project.model_dump()))
        (logs_dir / "task.json").write_text(_dump(agent_task.model_dump()))
        (logs_dir / "events.json").write_text(_dump([e.model_dump() for e in events]))
        (logs_dir / "transcript.txt").write_text(
            "\n\n".join(f"[{e.created_at.isoformat()}] {e}" for e in events)
        )

    @staticmethod
    def _extract_over(tar_path: Path, workdir: Path) -> None:
        """Unpack Aristotle's archive over the workdir (completed files win).

        Aristotle wraps its result in a single top-level directory (e.g.
        ``<name>_aristotle/``); we unwrap that so files land at the workdir root and
        overwrite the originals, rather than nesting a second copy one level down.
        """
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            with tarfile.open(tar_path, "r:gz") as tar:
                tar.extractall(staging, filter="data")

            # Unwrap iff everything sits under exactly one top-level directory.
            entries = list(staging.iterdir())
            wrapped = len(entries) == 1 and entries[0].is_dir()
            source = entries[0] if wrapped else staging

            # Recreate directories too (not just files): an empty directory can be
            # load-bearing -- e.g. a lakefile that globs a dir the agent had to create.
            for item in source.rglob("*"):
                dest = workdir / item.relative_to(source)
                if item.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                elif item.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest)
