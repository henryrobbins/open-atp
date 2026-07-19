"""Benchmark sweep tests (no Docker, no creds).

Drives :func:`~open_atp.benchmark.run_benchmark` with scripted, no-backend provers
(reusing the ``FakeProver`` seam): it lays out ``output_dir/<task>/<prover>/`` with a
``results.json`` per cell, records a raising prover as a failed result without aborting
the sweep, and renders a table with one row per ``(task, prover)`` pair.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from rich.console import Console

from open_atp import benchmark
from open_atp.benchmark import (
    DATASET,
    download_dataset,
    run_benchmark,
    tasks_from_dir,
)
from open_atp.lean import LeanProject, ProofTask
from open_atp.provers.base import ProofResult, ProofStatus

from .test_api import FIXTURE, FakeProver


def _tasks() -> dict[str, ProofTask]:
    task = ProofTask(LeanProject(FIXTURE))
    return {"alpha": task, "beta": task}


class _Peak:
    """Tracks the peak number of threads simultaneously inside its ``with`` block."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.peak = 0

    def __enter__(self) -> None:
        with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)

    def __exit__(self, *_exc: object) -> None:
        with self._lock:
            self.current -= 1


class ProbeProver(FakeProver):
    """A ``FakeProver`` whose ``_generate`` sleeps while recording concurrency."""

    def __init__(self, name: str, peak: _Peak) -> None:
        super().__init__(name)
        self._peak = peak

    def _generate(
        self, task: ProofTask, wd: Path, logs_dir: Path, result: ProofResult
    ) -> None:
        with self._peak:
            time.sleep(0.02)
        super()._generate(task, wd, logs_dir, result)


def _many_tasks(n: int) -> dict[str, ProofTask]:
    task = ProofTask(LeanProject(FIXTURE))
    return {f"t{i}": task for i in range(n)}


def _skeleton(root: Path) -> Path:
    """A minimal lake skeleton (lakefile + toolchain) for create_project staging."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "lakefile.toml").write_text('name = "demo"\n')
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.28.0\n")
    return root


def _lean(path: Path, *, name: str = "t") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"theorem {name} : True := by sorry\n")


def test_layout_and_results_json(tmp_path: Path) -> None:
    provers = {"good": FakeProver("agent"), "bad": FakeProver("numina", verified=False)}

    result = run_benchmark(_tasks(), provers, tmp_path)

    # Every (task, prover) cell has its own <task>/<prover>/ dir with wd/logs/results.
    for task in ("alpha", "beta"):
        for prover in ("good", "bad"):
            run_dir = tmp_path / task / prover
            assert (run_dir / "wd").is_dir()
            assert (run_dir / "logs").is_dir()
            payload = json.loads((run_dir / "results.json").read_text())
            assert payload["success"] is (prover == "good")

    assert len(result.runs) == 4
    assert {(r.task, r.prover) for r in result.runs} == {
        ("alpha", "good"),
        ("alpha", "bad"),
        ("beta", "good"),
        ("beta", "bad"),
    }


def test_raising_prover_recorded_not_aborted(tmp_path: Path) -> None:
    provers = {
        "boom": FakeProver("agent", raises=RuntimeError("docker down")),
        "ok": FakeProver("numina"),
    }

    result = run_benchmark({"alpha": _tasks()["alpha"]}, provers, tmp_path)

    by_prover = {r.prover: r.result for r in result.runs}
    assert by_prover["boom"].error == "docker down"
    assert by_prover["boom"].verification is None
    assert by_prover["boom"].success is False
    # A bare RuntimeError is an unclassified prover failure.
    assert by_prover["boom"].status is ProofStatus.ERROR
    assert by_prover["ok"].success is True
    assert by_prover["ok"].status is ProofStatus.VERIFIED
    # The failed cell still wrote its results.json, status included.
    payload = json.loads((tmp_path / "alpha" / "boom" / "results.json").read_text())
    assert payload["error"] == "docker down"
    assert payload["status"] == "error"


def test_table_has_a_row_per_pair(tmp_path: Path) -> None:
    provers = {"good": FakeProver("agent"), "bad": FakeProver("numina", verified=False)}

    from open_atp.__main__ import _benchmark_table

    result = run_benchmark(_tasks(), provers, tmp_path)
    table = _benchmark_table(result)

    assert [c.header for c in table.columns] == [
        "task",
        "prover",
        "status",
        "cost",
        "time",
    ]
    assert table.row_count == 4  # 2 tasks x 2 provers

    # Render through a plain (non-tty) console: markup collapses to bare glyphs.
    console = Console(
        file=io.StringIO(), width=200, force_terminal=False, color_system=None
    )
    console.print(table)
    rendered = console.file.getvalue()
    assert "✓" in rendered and "✗" in rendered


def test_only_restricts_and_orders_tasks(tmp_path: Path) -> None:
    tasks = _many_tasks(4)  # t0, t1, t2, t3
    provers = {"p": FakeProver("agent")}

    result = run_benchmark(tasks, provers, tmp_path, only=["t2", "t0"])

    assert [r.task for r in result.runs] == ["t2", "t0"]
    assert (tmp_path / "t2" / "p").is_dir()
    assert not (tmp_path / "t1").exists()  # skipped


def test_only_unknown_task_raises(tmp_path: Path) -> None:
    provers = {"p": FakeProver("agent")}
    with pytest.raises(ValueError, match="unknown task"):
        run_benchmark(_many_tasks(2), provers, tmp_path, only=["nope"])


# --- parallelism -----------------------------------------------------------


def test_runs_keep_task_major_order_when_parallel(tmp_path: Path) -> None:
    provers = {"good": FakeProver("agent"), "bad": FakeProver("numina")}

    runs = run_benchmark(_tasks(), provers, tmp_path, max_workers=4).runs

    assert [(r.task, r.prover) for r in runs] == [
        ("alpha", "good"),
        ("alpha", "bad"),
        ("beta", "good"),
        ("beta", "bad"),
    ]


def test_max_workers_one_runs_serially(tmp_path: Path) -> None:
    peak = _Peak()
    provers = {"p": ProbeProver("agent", peak)}

    run_benchmark(_many_tasks(4), provers, tmp_path, max_workers=1)

    assert peak.peak == 1


def test_per_prover_cap_bounds_one_prover(tmp_path: Path) -> None:
    peak = _Peak()
    provers = {"p": ProbeProver("agent", peak)}

    run_benchmark(_many_tasks(8), provers, tmp_path, max_workers=8, max_per_prover=2)

    assert peak.peak == 2  # capped below the 8 available workers


def test_cap_is_per_prover_not_global(tmp_path: Path) -> None:
    shared = _Peak()  # one tracker across both provers measures global concurrency
    provers = {"a": ProbeProver("agent", shared), "b": ProbeProver("numina", shared)}

    run_benchmark(_many_tasks(4), provers, tmp_path, max_workers=4, max_per_prover=1)

    assert shared.peak == 2  # one run each from two provers overlap despite the cap


# --- tasks_from_dir --------------------------------------------------------


def test_loose_lean_files_keyed_by_stem(tmp_path: Path) -> None:
    skeleton = _skeleton(tmp_path / "skel")
    bench = tmp_path / "bench"
    _lean(bench / "putnam_1962_a1.lean")
    _lean(bench / "putnam_1962_a2.lean")

    tasks = tasks_from_dir(bench, skeleton=skeleton)

    assert set(tasks) == {"putnam_1962_a1", "putnam_1962_a2"}
    # A bare file is staged into the skeleton (its project root is not the source dir).
    project = tasks["putnam_1962_a1"].project
    assert project.root != bench
    assert [p.name for p in project.lean_files()] == ["putnam_1962_a1.lean"]
    assert (project.root / "lean-toolchain").is_file()


def test_subdir_already_a_lake_project_used_as_is(tmp_path: Path) -> None:
    bench = tmp_path / "bench"
    proj = bench / "multi"
    _skeleton(proj)
    _lean(proj / "A.lean", name="a")
    _lean(proj / "B.lean", name="b")

    tasks = tasks_from_dir(bench, skeleton=_skeleton(tmp_path / "skel"))

    assert set(tasks) == {"multi"}
    # An existing lake project is used in place, not re-staged.
    assert tasks["multi"].project.root == proj.resolve()


def test_subdir_without_skeleton_is_staged(tmp_path: Path) -> None:
    skeleton = _skeleton(tmp_path / "skel")
    bench = tmp_path / "bench"
    group = bench / "group"
    _lean(group / "A.lean", name="a")
    _lean(group / "B.lean", name="b")

    tasks = tasks_from_dir(bench, skeleton=skeleton)

    assert set(tasks) == {"group"}
    project = tasks["group"].project
    assert project.root != group  # staged into a fresh project
    assert {p.name for p in project.lean_files()} == {"A.lean", "B.lean"}


def test_skips_hidden_and_empty_dirs(tmp_path: Path) -> None:
    skeleton = _skeleton(tmp_path / "skel")
    bench = tmp_path / "bench"
    _lean(bench / "keep.lean")
    (bench / "scripts").mkdir(parents=True)  # no .lean, not a project
    (bench / "scripts" / "run.py").write_text("print('hi')\n")
    hidden = bench / ".git"
    hidden.mkdir()
    _lean(hidden / "ignored.lean")
    shutil.copy2(skeleton / "lakefile.toml", bench / ".hidden.lean")  # leading dot

    tasks = tasks_from_dir(bench, skeleton=skeleton)

    assert set(tasks) == {"keep"}


# --- download_dataset ------------------------------------------------------


def test_sparse_clones_the_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        if "clone" in cmd:  # emulate the clone materializing repo + subdir
            src = Path(cmd[-1]) / "lean4" / "src"
            src.mkdir(parents=True)
            (src / "1.lean").write_text("theorem t : True := sorry")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)

    path = download_dataset(DATASET.PUTNAM, tmp_path)

    # The task subdir is lifted to dest/<dataset>, leaving no surrounding repo.
    assert path == tmp_path / "putnam"
    assert (path / "1.lean").is_file()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["putnam"]
    clone = next(c for c in calls if "clone" in c)
    assert "--sparse" in clone and "--depth" in clone
    assert clone[-2] == "https://github.com/trishullab/PutnamBench.git"
    set_cmd = next(c for c in calls if "sparse-checkout" in c)
    assert set_cmd[-1] == "lean4/src"


def test_existing_download_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fate-m").mkdir(parents=True)

    def boom(*_: object, **__: object) -> object:
        raise AssertionError("should not clone a cached dataset")

    monkeypatch.setattr(benchmark.subprocess, "run", boom)

    assert download_dataset(DATASET.FATE_M, tmp_path) == tmp_path / "fate-m"


def test_examples_copies_bundled_assets_without_cloning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_atp.examples import example_assets

    def boom(*_: object, **__: object) -> object:
        raise AssertionError("examples are copied, not cloned")

    monkeypatch.setattr(benchmark.subprocess, "run", boom)

    path = download_dataset(DATASET.EXAMPLES, tmp_path)

    assert path == tmp_path / "examples"
    assert sorted(p.name for p in path.glob("*.lean")) == sorted(
        a.name for a in example_assets()
    )
