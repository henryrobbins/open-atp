"""AristotleProver: a wrapper around Harmonic's Aristotle API.

No agentic sandbox is needed for generation -- we hand the lake project to the
hosted Aristotle agent via ``aristotlelib`` (submit -> wait -> download), unpack the
returned archive over the workdir, and let the shared verifier do the final check in
our own Docker sandbox.

All remote interaction is confined to a single coroutine, so the rest of the prover
runs without the network or an API key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tarfile
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from open_atp._capture import capture_stdout
from open_atp.auth import AuthKind, AuthStatus
from open_atp.backends.base import ComputeBackend
from open_atp.harness.base import MissingCredentials
from open_atp.lean import LeanProject, ProofTask
from open_atp.provers.base import (
    AutomatedProver,
    ProofResult,
    _compose_prompt,
)

if TYPE_CHECKING:
    from aristotlelib import AgentTask, Project

log = logging.getLogger("open_atp")

_T = TypeVar("_T")


class ServiceError(Exception):
    """The hosted Aristotle service produced no candidate to verify."""


PROVER_PROMPT = (
    "Complete every `sorry` in this Lean project. Make the project compile and be "
    "sorry-free without introducing new axioms; do not weaken or delete the stated "
    "theorems."
)
# END PROVER_PROMPT (docs literalinclude end marker -- keep adjacent)

# Directories never worth shipping to Aristotle / copying into the workdir.
_IGNORE = shutil.ignore_patterns(".lake", ".git", "*.tar.gz")


class _AristotleNoiseFilter(logging.Filter):
    """Drop the expected-noise record aristotlelib logs on our headless path.

    We deliberately upload without ``.lake`` (the sandbox already has the warm
    Mathlib cache), so aristotlelib's ".lake folder" warning is expected and
    redundant.
    """

    _DROP = ("no .lake folder",)

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
    leaves HTTP status errors with their code. We treat transport-level failures and
    server-side 5xx as transient, but let real 4xx (bad key, missing project) fail
    fast.
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
    allow_agent_questions : bool, default False
        Whether to let the hosted agent ask clarifying questions. This is a headless
        API path, so a prompt for stdin would hang the run.
    max_connection_retries : int, default 5
        Bounds retries of each API call when a connection drops.
        The hosted run lives server-side, so a dropped connection is recoverable:
        re-fetch rather than reporting the run failed.
    retry_backoff_seconds : float, default 5.0
        Initial sleep between retries of a failed call, doubling (capped) between
        tries.
    poll_interval_s : float, default 15.0
        Seconds between polls of the task's status while waiting for generation.
    timeout_s : int, default 1800
        Hard wall-clock cap on the generation wait, in seconds. When it elapses we
        stop waiting and proceed with whatever Aristotle has produced so far (the run
        keeps going and billing server-side regardless -- this only bounds the
        client).

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
        retry_backoff_seconds: float = 5.0,
        poll_interval_s: float = 15.0,
        timeout_s: int = 1800,
    ) -> None:
        super().__init__(backend=backend, timeout_s=timeout_s)
        self._api_key = api_key
        self.allow_agent_questions = allow_agent_questions
        self.max_connection_retries = max_connection_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.poll_interval_s = poll_interval_s

    @property
    def prover_prompt(self) -> str:
        """The prover's own prompt handed to Aristotle, before any user prompt."""
        return PROVER_PROMPT

    def auth_status(self) -> AuthStatus:
        """Report the ``ARISTOTLE_API_KEY`` the hosted API is called with.

        Returns
        -------
        ~open_atp.auth.AuthStatus
            A non-expiring API-key status, read from the constructor override or
            the host environment.
        """
        return AuthStatus(
            kind=AuthKind.API_KEY,
            source="ARISTOTLE_API_KEY",
            present=bool(self._api_key or os.environ.get("ARISTOTLE_API_KEY")),
            remedy="an Aristotle API key (set it or pass api_key)",
        )

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

        prompt = _compose_prompt(self.prover_prompt, task.user_prompt)
        # The raw result archive and the full run record both belong with the run's
        # logs, not the proof project. The hosted agent has no live stdout stream, so
        # its record (events, transcript, summary) is downloaded here rather than teed.
        # Anything aristotlelib prints to stdout is captured to ``stdout.txt`` so a
        # concurrent benchmark sweep stays readable.
        result_tar = logs_dir / "aristotle_result.tar.gz"
        with capture_stdout(logs_dir / "stdout.txt"):
            downloaded, metadata = asyncio.run(
                self._submit_and_download(wd, prompt, result_tar, logs_dir)
            )

        result.metadata = metadata
        if downloaded is None:
            # No archive means no candidate: fail the run rather than verifying
            raise ServiceError(
                str(metadata.get("error", "Aristotle produced no output."))
            )
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
        # Aristotle is free to use. The API exposes no usage or credit field to
        # read either way.
        result.cost_usd = 0.0

        # Generation was network-only, so there is no live session to reuse
        result.verification = self.verifier.verify(LeanProject(wd))

    async def _submit_and_download(
        self, project_dir: Path, prompt: str, dest_tar: Path, logs_dir: Path
    ) -> tuple[Path | None, dict[str, object]]:
        """Submit ``project_dir`` to Aristotle, wait, and download the result archive.

        Also syncs the full run record (task metadata, every event, a readable
        transcript, project metadata) to ``logs_dir`` on the host.

        Returns ``(downloaded_tar_or_None, metadata)``.
        """
        import aristotlelib
        from aristotlelib import AgentQuestionsSetting, Project

        # We strip ``.lake`` before upload; silence aristotlelib's resulting
        # expected-noise record on the console.
        _quiet_aristotle_logger()

        key = self._api_key or os.environ.get("ARISTOTLE_API_KEY")
        if not key:
            raise MissingCredentials(
                "aristotle prover requires ARISTOTLE_API_KEY (set it or pass api_key)"
            )
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
        # On timeout, fall through and download whatever Aristotle produced so far.
        metadata_timed_out = await self._wait_until_terminal(agent_task)
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
        delay = self.retry_backoff_seconds
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

    async def _wait_until_terminal(self, agent_task: AgentTask) -> bool:
        """Poll the task until it settles server-side or ``timeout_s`` elapses.

        Returns ``True`` if we gave up at the deadline with the task still running.

        We deliberately do not consume aristotlelib's event stream: it is opened with
        ``timeout=None``, so a silently half-open connection blocks forever and cannot
        even be cancelled out of. Polling keeps every network call individually
        bounded, which is what makes the wall-clock deadline enforceable.
        """
        from aristotlelib.agent_task import TaskStatus

        terminal = {
            TaskStatus.COMPLETE,
            TaskStatus.COMPLETE_WITH_ERRORS,
            TaskStatus.OUT_OF_BUDGET,
            TaskStatus.FAILED,
            TaskStatus.CANCELED,
        }

        deadline = time.monotonic() + self.timeout_s
        while True:
            await self._with_retry(agent_task.refresh, "refresh task status")
            if agent_task.status in terminal:
                return False
            if time.monotonic() >= deadline:
                log.warning(
                    "aristotle: task %s still %s after timeout_s=%ss; proceeding with "
                    "whatever output is available",
                    agent_task.agent_task_id,
                    agent_task.status.name,
                    self.timeout_s,
                )
                return True
            # A concurrent sweep runs many of these at once; keep the poll quiet.
            log.debug(
                "aristotle: task %s %s at %s%%",
                agent_task.agent_task_id,
                agent_task.status.name,
                agent_task.percent_complete,
            )
            await asyncio.sleep(self.poll_interval_s)

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
