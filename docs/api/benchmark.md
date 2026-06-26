# `benchmark`

Run a set of provers across a set of named {class}`~open_atp.lean.ProofTask`s and
tabulate the results. {func}`~open_atp.benchmark.run_benchmark` runs every
`(task, prover)` pair and lays the artifacts out under `output_dir` as
`output_dir/<task>/<prover>/{wd,logs,results.json}`, returning a
{class}`~open_atp.benchmark.BenchmarkResult` with a terminal-table view. A prover that
raises is recorded as a failed {class}`~open_atp.provers.base.ProofResult` so one bad
run never aborts the sweep.

## Runner

```{eval-rst}
.. autofunction:: open_atp.benchmark.run_benchmark
```

## Building tasks from a directory

{func}`~open_atp.benchmark.tasks_from_dir` builds the `tasks` mapping from a directory
laid out like the public Lean benchmarks
([PutnamBench](https://github.com/trishullab/PutnamBench/tree/main/lean4/src),
[FATE](https://github.com/frenzymath/FATE)) — a flat directory of standalone `.lean`
files, optionally with subdirectories grouping several files into one task.

```{eval-rst}
.. autofunction:: open_atp.benchmark.tasks_from_dir
```

## Downloading a dataset

{func}`~open_atp.benchmark.download_dataset` fetches one of the public benchmarks (a
{class}`~open_atp.benchmark.DATASET` member) straight to a task directory — a sparse
clone of just the dataset's `.lean` subdirectory — ready for
{func}`~open_atp.benchmark.tasks_from_dir`.

```{eval-rst}
.. autofunction:: open_atp.benchmark.download_dataset

.. autoclass:: open_atp.benchmark.DATASET
   :members:
```

## Results

```{eval-rst}
.. autoclass:: open_atp.benchmark.BenchmarkResult
   :members: table, to_dict

.. autoclass:: open_atp.benchmark.BenchmarkRun
```
