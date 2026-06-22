"""Live end-to-end test against the real Aristotle API.

Opt-in only: the ``aristotle_api`` marker is excluded by default (see ``addopts``
in pyproject), so run it explicitly. It makes a real, billable submission and waits
for completion, so it is slow.

    cp .env.example .env   # add your ARISTOTLE_API_KEY
    uv run pytest -m aristotle_api

Submits the trivial Mathematics-in-Lean fixture (one sorry'd theorem), then runs
the same shared Docker verifier every prover uses on what Aristotle returns.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from open_afps.backends.docker import DockerBackend, DockerConfig
from open_afps.core.task import LeanProject, ProofTask
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_afps.provers.aristotle import AristotleProver, AristotleProverConfig

FIXTURE = Path(__file__).parent / "fixtures" / "mil_trivial"


@pytest.mark.aristotle_api
@pytest.mark.docker
def test_live_aristotle_solves_trivial_theorem(tmp_path: Path) -> None:
    if not os.environ.get("ARISTOTLE_API_KEY"):
        pytest.skip("ARISTOTLE_API_KEY not set (add it to .env)")

    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    config = AristotleProverConfig(
        image=DEFAULT_IMAGE, supported_toolchain=DEFAULT_TOOLCHAIN
    )
    prover = AristotleProver(config, backend)

    result = prover.run(ProofTask(LeanProject(FIXTURE)), tmp_path / "wd")

    # The submission round-tripped and returned completed work.
    assert result.metadata.get("project_id"), result.metadata
    assert result.completed_files, "Aristotle returned no changed files"
    # And the proof it produced actually verifies under our pinned toolchain.
    assert result.success, (
        f"status={result.metadata.get('task_status')} "
        f"log={result.verification and result.verification.compile_log}"
    )
