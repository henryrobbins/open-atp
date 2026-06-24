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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from open_atp.lean import ProofTask
from open_atp.provers.base import AutomatedProver, AutomatedProverConfig
from open_atp.verify import ProofResult

if TYPE_CHECKING:
    from aristotlelib import AgentTask, Project

log = logging.getLogger(__name__)

_T = TypeVar("_T")

_DEFAULT_PROMPT = (
    "Complete every `sorry` in this Lean project. Make the project compile and be "
    "sorry-free without introducing new axioms; do not weaken or delete the stated "
    "theorems."
)
# END _DEFAULT_PROMPT (docs literalinclude end marker -- keep adjacent)

# Directories never worth shipping to Aristotle / copying into the workdir.
_IGNORE = shutil.ignore_patterns(".lake", ".git", "*.tar.gz")


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


@dataclass
class AristotleProverConfig(AutomatedProverConfig):
    """Configuration for :class:`AristotleProver`.

    Extends :class:`~open_atp.provers.base.AutomatedProverConfig` (``image``,
    ``supported_toolchain``, ``timeout_s``, ``env``) with the hosted-API knobs.

    Attributes
    ----------
    api_key_env : str
        Name of the environment variable holding the Harmonic API key. Default
        ``ARISTOTLE_API_KEY``.
    allow_agent_questions : bool
        Whether to let the hosted agent ask clarifying questions. Off by default:
        this is a headless API path and a prompt for stdin would hang the run.
    max_connection_retries : int
        Bounds per-call retries (list/refresh/download) when a connection drops.
        The hosted run lives server-side, so a dropped connection is recoverable:
        re-fetch rather than reporting the run failed. Default ``5``.
    max_resume_attempts : int
        Bounds how many times we re-attach to the event stream when it drops
        mid-run. Default ``20``.
    resume_backoff_seconds : float
        Initial sleep between retries/resumes, doubling (capped) between tries.
        Default ``5.0``.
    """

    api_key_env: str = "ARISTOTLE_API_KEY"
    allow_agent_questions: bool = False
    max_connection_retries: int = 5
    max_resume_attempts: int = 20
    resume_backoff_seconds: float = 5.0


class AristotleProver(AutomatedProver):
    name = "aristotle"

    config: AristotleProverConfig

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

        prompt = task.instructions or _DEFAULT_PROMPT
        # The raw result archive and the full run record both belong with the run's
        # logs, not the proof project. ``prove`` already created ``logs_dir``; the
        # hosted agent has no live stdout stream, so its record (events, transcript,
        # summary) is downloaded here rather than teed.
        result_tar = logs_dir / "aristotle_result.tar.gz"
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

        key = os.environ.get(self.config.api_key_env)
        if key:
            aristotlelib.set_api_key(key)

        questions = (
            AgentQuestionsSetting.TIMEOUT_15_MIN
            if self.config.allow_agent_questions
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
        # Resume across dropped connections until the task truly settles server-side.
        await self._wait_until_terminal(agent_task)
        await self._with_retry(project.refresh, "refresh project")

        metadata.update(
            task_id=agent_task.agent_task_id,
            task_status=agent_task.status.name,
            percent_complete=agent_task.percent_complete,
            output_summary=agent_task.output_summary,
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
        delay = self.config.resume_backoff_seconds
        for attempt in range(1, self.config.max_connection_retries + 1):
            try:
                return await op()
            except Exception as exc:  # noqa: BLE001 -- re-raised unless transient
                if (
                    not _is_transient(exc)
                    or attempt == self.config.max_connection_retries
                ):
                    raise
                log.warning(
                    "aristotle: %s failed (%s); retrying (attempt %d/%d)",
                    what,
                    exc,
                    attempt,
                    self.config.max_connection_retries,
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
        settles (or we exhaust the resume budget, in which case we proceed with
        whatever output exists -- the run dashboard is the source of truth).
        """
        from aristotlelib.agent_task import TaskStatus

        terminal = {
            TaskStatus.COMPLETE,
            TaskStatus.COMPLETE_WITH_ERRORS,
            TaskStatus.OUT_OF_BUDGET,
            TaskStatus.FAILED,
            TaskStatus.CANCELED,
        }
        delay = self.config.resume_backoff_seconds
        for attempt in range(1, self.config.max_resume_attempts + 1):
            try:
                await agent_task.wait_for_completion()
            except Exception as exc:  # noqa: BLE001 -- re-raised unless transient
                if not _is_transient(exc):
                    raise
                log.warning("aristotle: wait interrupted (%s); resuming", exc)
            await self._with_retry(agent_task.refresh, "refresh task status")
            if agent_task.status in terminal:
                return
            log.warning(
                "aristotle: connection dropped with task still %s; resuming "
                "(attempt %d/%d)",
                agent_task.status.name,
                attempt,
                self.config.max_resume_attempts,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
        log.warning(
            "aristotle: task %s still %s after %d resume attempts; proceeding with "
            "whatever output is available",
            agent_task.agent_task_id,
            agent_task.status.name,
            self.config.max_resume_attempts,
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

            for item in source.rglob("*"):
                if item.is_file():
                    dest = workdir / item.relative_to(source)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest)
