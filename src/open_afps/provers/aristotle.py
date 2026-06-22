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
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from open_afps.core.prover import AutomatedProver, AutomatedProverConfig
from open_afps.core.result import GenerationOutput
from open_afps.core.task import ProofTask

_DEFAULT_PROMPT = (
    "Complete every `sorry` in this Lean project. Make the project compile and be "
    "sorry-free without introducing new axioms; do not weaken or delete the stated "
    "theorems."
)

# Directories never worth shipping to Aristotle / copying into the workdir.
_IGNORE = shutil.ignore_patterns(".lake", ".git", "*.tar.gz")


@dataclass
class AristotleProverConfig(AutomatedProverConfig):
    api_key_env: str = "ARISTOTLE_API_KEY"
    # Allow the hosted agent to ask clarifying questions? Off by default: this is a
    # headless API path and a prompt for stdin would hang the run.
    allow_agent_questions: bool = False


class AristotleProver(AutomatedProver):
    name = "aristotle"

    config: AristotleProverConfig

    def prove(self, task: ProofTask, workdir: Path) -> GenerationOutput:
        # Stage the original project so the workdir is a complete project both for the
        # upload and, after extraction, for verification.
        shutil.copytree(task.project.root, workdir, dirs_exist_ok=True, ignore=_IGNORE)

        original = {
            p.relative_to(task.project.root).as_posix(): p.read_text()
            for p in task.project.lean_files()
        }

        prompt = task.instructions or _DEFAULT_PROMPT
        result_tar = workdir.parent / f"{workdir.name}_aristotle.tar.gz"
        downloaded, metadata = asyncio.run(
            self._submit_and_download(workdir, prompt, result_tar)
        )

        if downloaded is not None:
            self._extract_over(downloaded, workdir)

        # Report the .lean files Aristotle changed or added.
        completed: dict[str, str] = {}
        for path in sorted(workdir.rglob("*.lean")):
            if ".lake" in path.parts:
                continue
            rel = path.relative_to(workdir).as_posix()
            content = path.read_text()
            if original.get(rel) != content:
                completed[rel] = content

        summary = (
            (workdir / "ARISTOTLE_SUMMARY.md").read_text()
            if (workdir / "ARISTOTLE_SUMMARY.md").is_file()
            else ""
        )

        return GenerationOutput(
            completed_files=completed,
            # The Aristotle API does not expose a per-run cost; leave it unset.
            cost_usd=None,
            logs=summary,
            metadata=metadata,
        )

    async def _submit_and_download(
        self, project_dir: Path, prompt: str, dest_tar: Path
    ) -> tuple[Path | None, dict[str, object]]:
        """Submit ``project_dir`` to Aristotle, wait, and download the result archive.

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
        tasks, _ = await project.get_tasks(limit=1)
        metadata: dict[str, object] = {"project_id": project.project_id}
        if not tasks:
            metadata["error"] = "Aristotle returned no task to wait on."
            return None, metadata

        agent_task = tasks[0]
        await agent_task.wait_for_completion()
        await project.refresh()

        metadata.update(
            task_status=agent_task.status.name,
            percent_complete=agent_task.percent_complete,
            output_summary=agent_task.output_summary,
        )

        if not project.has_files:
            metadata["error"] = "Aristotle produced no output files."
            return None, metadata

        await project.get_files(destination=dest_tar)
        return dest_tar, metadata

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
