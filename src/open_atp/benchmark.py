"""Run a set of provers across a set of named proof tasks and tabulate the results.

:func:`run_benchmark` is the matrix runner: given a name -> task mapping and a
name -> prover mapping, it runs every ``(task, prover)`` pair and lays the artifacts
out under ``output_dir`` as::

    output_dir/<task>/<prover>/{wd,logs,results.json}

``wd`` and ``logs`` are exactly what
:meth:`~open_atp.provers.base.AutomatedProver.prove` writes; ``results.json`` is the
:class:`~open_atp.provers.base.ProofResult` for that cell. A prover that raises is
recorded as a failed result (its ``error`` captured) so one bad run never aborts the
sweep.

The returned :class:`BenchmarkResult` collects every cell; the ``open-atp benchmark``
CLI renders it as a table, and :meth:`BenchmarkResult.to_dict` is the JSON view.

:func:`tasks_from_dir` builds the ``tasks`` mapping from a directory laid out like the
public Lean benchmarks (`PutnamBench
<https://github.com/trishullab/PutnamBench/tree/main/lean4/src>`_, `FATE
<https://github.com/frenzymath/FATE>`_): a flat directory of standalone ``.lean``
files, optionally with subdirectories grouping several files into one task.
:func:`download_dataset` fetches one of those benchmarks (a :class:`DATASET` member)
straight to such a directory.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from tqdm import tqdm

from open_atp.backends.base import ExecTimeout
from open_atp.images import SKELETON_DIR
from open_atp.lean import LeanProject, ProofTask, create_project
from open_atp.provers.base import AutomatedProver, ProofResult

log = logging.getLogger("open_atp")


@dataclass(frozen=True)
class BenchmarkRun:
    """One ``(task, prover)`` cell of a benchmark sweep.

    Attributes
    ----------
    task : str
        The task's key in the benchmark's ``tasks`` mapping.
    prover : str
        The prover's key in the benchmark's ``provers`` mapping. Caller-chosen, so
        it may differ from the prover's own
        :attr:`~open_atp.provers.base.ProofResult.prover` (e.g. two entries running
        the same prover under different labels).
    result : ~open_atp.provers.base.ProofResult
        The run's result. On an exception its
        :attr:`~open_atp.provers.base.ProofResult.error` is set and
        :attr:`~open_atp.provers.base.ProofResult.verification` is ``None``.
    """

    task: str
    prover: str
    result: ProofResult


@dataclass(frozen=True)
class BenchmarkResult:
    """The collected cells of a benchmark sweep.

    Attributes
    ----------
    output_dir : pathlib.Path
        The benchmark's output root, laid out as ``output_dir/<task>/<prover>/``.
    runs : list[BenchmarkRun]
        One :class:`BenchmarkRun` per ``(task, prover)`` pair, in run order.
    """

    output_dir: Path
    runs: list[BenchmarkRun]

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view: the output root plus each cell's task, prover, result."""
        return {
            "output_dir": str(self.output_dir),
            "runs": [
                {
                    "task": run.task,
                    "prover": run.prover,
                    "result": run.result.to_dict(),
                }
                for run in self.runs
            ],
        }


def _prove_bounded(
    prover: AutomatedProver, task: ProofTask, run_dir: Path, ceiling_s: float
) -> ProofResult:
    """Run ``prover.prove`` on a daemon thread, bounded by a hard wall-clock ceiling.

    The backend already bounds each Modal call, so this rarely fires; it is the outer
    backstop guaranteeing no single wedged run can stall the whole sweep. On timeout
    the worker thread is abandoned (daemon, so it can't block process exit) and
    :class:`~open_atp.backends.base.ExecTimeout` is raised for the caller to classify.
    """
    result: list[ProofResult] = []
    error: list[BaseException] = []

    def target() -> None:
        try:
            result.append(prover.prove(task, run_dir))
        except BaseException as exc:  # re-raised on the caller's thread
            error.append(exc)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(ceiling_s)
    if worker.is_alive():
        log.error(
            "task exceeded wall-clock ceiling",
            extra={"prover": prover.name, "task": task.name, "ceiling_s": ceiling_s},
        )
        raise ExecTimeout(f"task exceeded {ceiling_s:.0f}s wall-clock ceiling")
    if error:
        raise error[0]
    return result[0]


def run_benchmark(
    tasks: Mapping[str, ProofTask],
    provers: Mapping[str, AutomatedProver],
    output_dir: Path | str,
    *,
    only: Sequence[str] | None = None,
    max_workers: int | None = None,
    max_per_prover: int = 5,
    progress: bool = True,
) -> BenchmarkResult:
    """Run every prover over every task, writing artifacts under ``output_dir``.

    Each ``(task, prover)`` pair runs in its own ``output_dir/<task>/<prover>/``
    directory, which receives the prover's ``wd``/``logs`` and a ``results.json``
    dump of the :class:`~open_atp.provers.base.ProofResult`. A prover that raises is
    recorded as a failed result (its ``error`` captured) and the sweep continues.

    Pairs run concurrently on a thread pool (``prove`` is I/O-bound on the sandbox and
    the prover's API). Two independent limits bound the concurrency: ``max_workers``
    caps the total in-flight pairs, and ``max_per_prover`` caps how many of those may
    belong to any one prover -- so a rate-limited prover (e.g. a hosted model) never
    has more than ``max_per_prover`` calls in flight even when other provers fill the
    pool. The returned ``runs`` keep task-major, prover-minor order regardless of
    completion order.

    Parameters
    ----------
    tasks : Mapping[str, ~open_atp.lean.ProofTask]
        Tasks keyed by name; the name becomes the task's output subdirectory.
    provers : Mapping[str, ~open_atp.provers.base.AutomatedProver]
        Provers keyed by name; the name becomes the per-task output subdirectory.
        A mapping (not a list) so the caller labels each entry -- the same prover can
        appear under several keys (e.g. different models) and stay distinct on disk
        and in the table.
    output_dir : pathlib.Path or str
        Output root for the sweep, laid out as ``output_dir/<task>/<prover>/``.
    only : Sequence[str], optional
        Restrict the sweep to these task names (a subset of ``tasks``), in the given
        order. ``None`` (default) runs every task. An unknown name raises
        :class:`ValueError`.
    max_workers : int, optional
        Total ``prove`` calls in flight at once. ``None`` (default) lets the thread
        pool pick a default; ``1`` runs the sweep serially.
    max_per_prover : int
        Most concurrent ``prove`` calls for any single prover. Default ``5``, to stay
        under rate limits.
    progress : bool
        Show a :mod:`tqdm` progress bar over the ``(task, prover)`` pairs. Default
        ``True``. Each completed pair is logged (task, prover, status, duration, cost)
        on the ``open_atp`` logger regardless.

    Returns
    -------
    BenchmarkResult
        Every ``(task, prover)`` cell (see :meth:`~BenchmarkResult.to_dict`).
    """
    output_dir = Path(output_dir)
    if only is not None:
        missing = [name for name in only if name not in tasks]
        if missing:
            raise ValueError(f"unknown task(s) {missing}; available: {sorted(tasks)}")
        tasks = {name: tasks[name] for name in only}
    # Carry the benchmark key as each task's name so ``prove`` can attribute its log
    # records to the task; a name already set on the task wins.
    tasks = {
        name: task if task.name else replace(task, name=name)
        for name, task in tasks.items()
    }
    gates = {name: threading.Semaphore(max_per_prover) for name in provers}

    def run_pair(
        task_name: str, task: ProofTask, prover_name: str, prover: AutomatedProver
    ) -> BenchmarkRun:
        run_dir = output_dir / task_name / prover_name
        run_dir.mkdir(parents=True, exist_ok=True)
        # ``prove`` binds task/prover/run_id onto the context itself, so backend and
        # verifier records stay attributed and a generation crash is already logged
        # (with traceback) inside that binding -- here we only build the error result.
        with gates[prover_name]:
            try:
                result = _prove_bounded(prover, task, run_dir, prover.max_duration_s)
            except Exception as exc:
                result = ProofResult.errored(prover.name, run_dir, exc)
        (run_dir / "results.json").write_text(
            json.dumps(result.to_dict(), indent=2, default=str)
        )
        return BenchmarkRun(task=task_name, prover=prover_name, result=result)

    jobs = [
        (task_name, task, prover_name, prover)
        for task_name, task in tasks.items()
        for prover_name, prover in provers.items()
    ]
    slots: list[BenchmarkRun | None] = [None] * len(jobs)
    bar = tqdm(total=len(jobs), disable=not progress, unit="run")

    def record(index: int, run: BenchmarkRun) -> None:
        slots[index] = run
        r = run.result
        status = "✓" if r.success else r.status.value
        log.debug(
            "run complete",
            extra={"task": run.task, "prover": run.prover, "status": status},
        )
        bar.update(1)

    try:
        if max_workers == 1:
            for i, job in enumerate(jobs):
                record(i, run_pair(*job))
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(run_pair, *job): i for i, job in enumerate(jobs)}
                for future in as_completed(futures):
                    record(futures[future], future.result())
    finally:
        bar.close()

    runs = [run for run in slots if run is not None]
    return BenchmarkResult(output_dir=output_dir, runs=runs)


def tasks_from_dir(
    directory: Path | str,
    *,
    skeleton: Path = SKELETON_DIR,
) -> dict[str, ProofTask]:
    """Build a benchmark's ``tasks`` mapping from a directory of Lean files.

    Mirrors the layout the public Lean benchmarks ship (PutnamBench, FATE):

    - Each ``.lean`` file directly under ``directory`` becomes one task named by its
      filename stem, staged into ``skeleton`` (a bare file carries no lake project).
    - Each subdirectory becomes one task named by the subdirectory name. A subdirectory
      that is *already* a complete lake project (carries its own ``lean-toolchain`` and
      lakefile) is used as-is; otherwise its ``.lean`` files are staged into
      ``skeleton``.

    In both staged cases :func:`~open_atp.lean.create_project` supplies the skeleton.
    Subdirectories with no ``.lean`` files (and entries whose name starts with ``.``)
    are skipped. The result is ready to hand to :func:`run_benchmark`.

    Parameters
    ----------
    directory : pathlib.Path or str
        The benchmark directory: ``.lean`` files and/or per-task subdirectories.
    skeleton : pathlib.Path
        Project skeleton staged around bare files (see
        :func:`~open_atp.lean.create_project`). Default
        :data:`~open_atp.images.SKELETON_DIR` -- the baked image's pinned Mathlib
        skeleton, only present in a source checkout. Pass a checkout of a benchmark's
        own toolchain to stage against a non-default Lean/Mathlib pin.

    Returns
    -------
    dict[str, ~open_atp.lean.ProofTask]
        Tasks keyed by file stem (loose files) or subdirectory name.
    """
    directory = Path(directory)
    tasks: dict[str, ProofTask] = {}
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_file() and entry.suffix == ".lean":
            dest = Path(tempfile.mkdtemp()) / entry.stem
            project = create_project([entry], dest, skeleton=skeleton)
            tasks[entry.stem] = ProofTask(project, name=entry.stem)
        elif entry.is_dir():
            subdir_project = _subdir_project(entry, skeleton)
            if subdir_project is not None:
                tasks[entry.name] = ProofTask(subdir_project, name=entry.name)
    return tasks


def _subdir_project(subdir: Path, skeleton: Path) -> LeanProject | None:
    """The subdirectory as a project: itself if a lake project, else staged.

    Returns ``None`` when the subdirectory is not a lake project and holds no ``.lean``
    files (nothing to run).
    """
    try:
        return LeanProject(subdir)
    except FileNotFoundError:
        pass
    lean_files = [p for p in sorted(subdir.rglob("*.lean")) if ".lake" not in p.parts]
    if not lean_files:
        return None
    dest = Path(tempfile.mkdtemp()) / subdir.name
    return create_project(lean_files, dest, skeleton=skeleton)


class DATASET(Enum):
    """The benchmark datasets accepted by :func:`download_dataset`.

    Each member's value is the directory name the dataset lands in. The FATE datasets
    and the bundled examples are on the default ``v4.28.0``; PutnamBench pins an older
    Lean (``v4.27.0``), so stage it with a matching ``skeleton`` (see
    :func:`tasks_from_dir`). ``EXAMPLES`` is the package's bundled
    :class:`~open_atp.examples.EXAMPLE` set, copied from the wheel rather than cloned.
    """

    #: The bundled :class:`~open_atp.examples.EXAMPLE` tasks (copied, not cloned).
    EXAMPLES = "examples"
    #: `PutnamBench <https://github.com/trishullab/PutnamBench>`_ (Lean 4).
    PUTNAM = "putnam"
    #: `FATE-H <https://github.com/frenzymath/FATE-H>`_ (hard).
    FATE_H = "fate-h"
    #: `FATE-M <https://github.com/frenzymath/FATE-M>`_ (medium).
    FATE_M = "fate-m"
    #: `FATE-X <https://github.com/frenzymath/FATE-X>`_ (extra).
    FATE_X = "fate-x"


#: Each dataset's GitHub ``owner/name`` and the subdirectory holding its ``.lean``
#: task files. Package-internal: :func:`download_dataset` clones from it.
_DATASETS: dict[DATASET, tuple[str, str]] = {
    DATASET.PUTNAM: ("trishullab/PutnamBench", "lean4/src"),
    DATASET.FATE_H: ("frenzymath/FATE-H", "FATEH"),
    DATASET.FATE_M: ("frenzymath/FATE-M", "FATEM"),
    DATASET.FATE_X: ("frenzymath/FATE-X", "FATEX"),
}


def download_dataset(
    dataset: DATASET,
    dest: Path | str,
    *,
    ref: str | None = None,
) -> Path:
    """Download a benchmark dataset's task directory to ``dest/<dataset>``.

    Sparse-clones only the dataset's task subdirectory (shallow + blobless), then
    lifts its ``.lean`` files to ``dest/<dataset>`` -- a flat directory ready for
    :func:`tasks_from_dir`, with no surrounding git repo. An already-present download
    is reused as-is (the clone is skipped), so repeated calls are cheap.
    :attr:`DATASET.EXAMPLES` instead copies the package's bundled example assets into
    ``dest/examples`` (no clone, ``ref`` ignored).

    Parameters
    ----------
    dataset : DATASET
        Which benchmark to fetch.
    dest : pathlib.Path or str
        Parent directory; the dataset lands at ``dest/<dataset>``. Created if missing.
    ref : str, optional
        Branch or tag to check out. Default ``None`` -- the repo's default branch.
        Ignored for :attr:`DATASET.EXAMPLES`.

    Returns
    -------
    pathlib.Path
        The dataset's task directory (``dest/<dataset>``).
    """
    dest = Path(dest)
    task_dir = dest / dataset.value
    if dataset is DATASET.EXAMPLES:
        return _copy_examples(task_dir)
    if task_dir.is_dir():
        return task_dir

    repo, subdir = _DATASETS[dataset]
    url = f"https://github.com/{repo}.git"
    clone = ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse"]
    if ref is not None:
        clone += ["--branch", ref]

    dest.mkdir(parents=True, exist_ok=True)
    # Clone into a temp dir on the same filesystem as task_dir, then move just the
    # task subdirectory into place so the git repo and other repo files don't linger.
    with tempfile.TemporaryDirectory(dir=dest) as tmp:
        repo_dir = Path(tmp) / "repo"
        subprocess.run(clone + [url, str(repo_dir)], check=True)
        subprocess.run(
            ["git", "-C", str(repo_dir), "sparse-checkout", "set", subdir], check=True
        )
        src = repo_dir / subdir
        if not src.is_dir():
            raise FileNotFoundError(
                f"{repo} has no {subdir!r} at ref {ref or 'default'}"
            )
        shutil.move(str(src), str(task_dir))
    return task_dir


def _copy_examples(task_dir: Path) -> Path:
    """Copy the bundled example ``.lean`` assets into ``task_dir`` and return it."""
    from open_atp.examples import example_assets

    task_dir.mkdir(parents=True, exist_ok=True)
    for asset in example_assets():
        shutil.copy2(asset, task_dir / asset.name)
    return task_dir
