"""AristotleProver tests.

The remote Aristotle call (``_submit_and_download``) is stubbed with a fake result
archive, so these run with no API key and no network. The full ``run()`` test still
exercises the *real* Docker verifier -- mocked remote, real local check -- which is
exactly the path every prover shares.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from open_afps.backends.docker import DockerBackend, DockerConfig
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_afps.provers.aristotle import AristotleProver, AristotleProverConfig

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"

SOLVED_FILE = """\
import Mathlib

theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by
  rw [mul_comm a b, mul_assoc b a c]
"""

SUMMARY = "# Summary\nFilled the single sorry; project compiles.\n"


def _make_prover() -> AristotleProver:
    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    config = AristotleProverConfig(
        image=DEFAULT_IMAGE, supported_toolchain=DEFAULT_TOOLCHAIN
    )
    return AristotleProver(config, backend)


def _fake_result(*, solved: bool) -> object:
    """An async stand-in for ``_submit_and_download`` that writes a result archive."""
    body = SOLVED_FILE if solved else open(FIXTURE / "MILExample.lean").read()

    async def _stub(
        self: AristotleProver,
        project_dir: Path,
        prompt: str,
        dest_tar: Path,
        logs_dir: Path,
    ) -> tuple[Path, dict[str, object]]:
        # Real Aristotle archives wrap everything under a single top-level dir;
        # mirror that so the test exercises _extract_over's unwrapping.
        with tarfile.open(dest_tar, "w:gz") as tar:
            for name, text in (
                ("MILExample.lean", body),
                ("ARISTOTLE_SUMMARY.md", SUMMARY),
            ):
                data = text.encode()
                info = tarfile.TarInfo(f"{project_dir.name}_aristotle/{name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        # Stand in for the real run-info sync so prove() sees a populated logs dir.
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / "events.json").write_text("[]")
        return dest_tar, {
            "project_id": "test-123",
            "task_status": "COMPLETE",
            "logs_dir": str(logs_dir),
        }

    return _stub


def test_prove_extracts_result_and_reports_changed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prove() alone: stage -> (stub) submit -> extract -> diff. No Docker needed."""
    from open_afps.core.task import LeanProject, ProofTask

    monkeypatch.setattr(
        AristotleProver, "_submit_and_download", _fake_result(solved=True)
    )
    prover = _make_prover()
    workdir = tmp_path / "wd"

    output = prover.prove(ProofTask(LeanProject(FIXTURE)), workdir)

    # The completed file landed in the workdir and is reported as changed.
    assert (workdir / "MILExample.lean").read_text() == SOLVED_FILE
    assert "MILExample.lean" in output.completed_files
    assert "rw [mul_comm" in output.completed_files["MILExample.lean"]
    assert output.metadata["project_id"] == "test-123"
    assert "Summary" in output.logs
    # The run record was synced to the host alongside the workdir.
    assert Path(output.metadata["logs_dir"]).joinpath("events.json").is_file()


@pytest.mark.docker
def test_run_end_to_end_verifies_completed_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full run(): mocked remote returns a real proof; Docker verifier confirms it."""
    from open_afps.core.task import LeanProject, ProofTask

    monkeypatch.setattr(
        AristotleProver, "_submit_and_download", _fake_result(solved=True)
    )
    prover = _make_prover()

    result = prover.run(ProofTask(LeanProject(FIXTURE)), tmp_path / "wd")

    assert result.success, result.verification and result.verification.compile_log
    assert result.verification is not None and result.verification.verified
    assert result.prover == "aristotle"
    assert result.cost_usd is None  # Aristotle exposes no per-run cost


@pytest.mark.docker
def test_run_reports_unverified_when_sorry_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Aristotle returns the file still sorry'd, the verifier catches it."""
    from open_afps.core.task import LeanProject, ProofTask

    monkeypatch.setattr(
        AristotleProver, "_submit_and_download", _fake_result(solved=False)
    )
    prover = _make_prover()

    result = prover.run(ProofTask(LeanProject(FIXTURE)), tmp_path / "wd")

    assert not result.success
    assert result.verification is not None and not result.verification.sorry_free
