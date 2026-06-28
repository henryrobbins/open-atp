# Benchmarking provers

To compare provers, {func}`~open_atp.benchmark.run_benchmark` runs every
`(task, prover)` pair and writes each cell to
`output_dir/<task>/<prover>/{wd,logs,results.json}`. Both tasks and provers are passed
as **name → object** mappings (the names become the subdirectory names, so several
provers sharing a class `name` — every `agent:*` is `"agent"` — stay distinct):

```python
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.benchmark import run_benchmark
from open_atp.config import standard_prover
from open_atp.examples import EXAMPLE, example_task
from open_atp.images import DEFAULT_IMAGE

backend = DockerBackend(image=DEFAULT_IMAGE)
tasks = {EXAMPLE.MUL_REORDER.value: example_task(EXAMPLE.MUL_REORDER)}
provers = {
    name: standard_prover(name, backend=backend)
    for name in ("agent:claude", "numina")
}

result = run_benchmark(tasks, provers, Path("runs/benchmark"))
print(result.table())
```

The returned {class}`~open_atp.benchmark.BenchmarkResult` collects every cell
(`result.runs`) and renders a terminal table with one row per `(task, prover)`. A
prover that raises is recorded as a failed
{class}`~open_atp.provers.base.ProofResult` (its `error` captured) so one bad run never
aborts the sweep.

Pairs run concurrently on a thread pool. `max_workers` caps the total in flight
(`None` auto-sizes; `1` is serial), and `max_per_prover` (default `5`) caps how many
runs any *single* prover may have in flight — so a rate-limited prover (e.g. a hosted
model) stays under its limit even when other provers fill the pool. Pass `only=[...]`
to run a subset of the tasks (by name, in the given order) instead of the whole set.

A `tqdm` progress bar tracks the pairs (disable with `progress=False`), and each
completion is logged via `structlog` with its task, prover, status, duration, and
cost. Aristotle's live progress display is captured to its run's `logs/stdout.txt`
rather than streamed to the terminal, so the sweep stays readable.

To run this sweep over the five bundled {class}`~open_atp.examples.EXAMPLE` tasks,
download them like any other dataset and benchmark the resulting directory:

```bash
open-atp download examples datasets         # -> datasets/examples
open-atp benchmark datasets/examples runs/examples --compute docker
```

`download examples` copies the bundled assets from the package (no clone); the
directory is then an ordinary {func}`~open_atp.benchmark.tasks_from_dir` input, so
all the `benchmark` options (`--provers`, `--compute {docker,modal}`, ...) apply.

## Tasks from a directory

To benchmark a directory of tasks, {func}`~open_atp.benchmark.tasks_from_dir` builds
the `tasks` mapping. Each entry directly under the directory becomes one task:

- A loose `.lean` file becomes a task named by its stem, staged into the skeleton.
- A subdirectory becomes a task named by the subdirectory. If it is *already* a
  complete lake project (it carries its own `lean-toolchain` and lakefile) it is used
  as-is; otherwise its `.lean` files (which may be several) are staged into the
  skeleton.

Entries beginning with `.` and subdirectories with no `.lean` files are skipped, so a
mix of all three styles in one directory is fine:

```
benchmark/
├── easy_lemma.lean              # bare file → task "easy_lemma"
├── another.lean                 # bare file → task "another"
├── multi_file/                  # staged subdir → task "multi_file"
│   ├── Defs.lean
│   └── Problem.lean             # several .lean files, staged together
└── full_project/                # complete lake project → task "full_project"
    ├── lean-toolchain
    ├── lakefile.toml
    ├── lake-manifest.json
    └── FullProject/
        └── Problem.lean
```

A directory may also be entirely one style — all bare files, or all per-task
subdirectories, as the public benchmarks ship:

```
fate-m/FATEM/                    putnam/                     loose/
├── algebra_001/                 ├── putnam_1962_a1/         ├── thm_1.lean
│   └── problem.lean             │   └── putnam_1962_a1.lean ├── thm_2.lean
└── algebra_002/                 └── putnam_1963_a1/         └── thm_3.lean
    └── problem.lean                 └── putnam_1963_a1.lean
```

## Downloading a dataset

{func}`~open_atp.benchmark.download_dataset` fetches one of the included public
benchmarks — a sparse clone of just the task subdirectory — straight into a directory
ready for {func}`~open_atp.benchmark.tasks_from_dir`:

```python
from open_atp.benchmark import DATASET, download_dataset, run_benchmark, tasks_from_dir

src = download_dataset(DATASET.FATE_M, "datasets")  # datasets/fate-m/FATEM
result = run_benchmark(tasks_from_dir(src), provers, Path("runs/fate-m"))
```

See {doc}`../datasets` for the bundled {class}`~open_atp.benchmark.DATASET` members
and their toolchains. PutnamBench pins an older Lean than the default skeleton, so
stage it against a matching skeleton (`tasks_from_dir(src, skeleton=...)`).

## From the command line

Download a dataset, then benchmark it — the `benchmark` command runs
{func}`~open_atp.benchmark.tasks_from_dir` over the directory and prints the table:

```bash
open-atp download fate-m datasets            # -> datasets/fate-m/FATEM
open-atp benchmark datasets/fate-m/FATEM --compute docker
```

`benchmark` runs every standard prover by default. To choose provers, pass a YAML
config with `--provers` (or drop a `provers.yaml` in the benchmarked directory). The
config is either a single standard prover name, or a list whose entries are each a
standard prover name **or** a prover-config mapping (an optional `name` keys the
result; otherwise it is derived from the prover/harness type):

```yaml
- agent:claude            # a standard prover name
- type: agent             # a prover config (built against --compute)
  harness:
    type: codex
    model: gpt-5.5
- name: aristotle-hosted  # an explicit name for this entry
  type: aristotle
```

`--compute {docker,modal}` picks the backend (standard image, default `docker`),
`-n/--max-workers` bounds total parallelism (each prover is still capped at 5
concurrent runs), `--tasks t1,t2` runs only those tasks (default: all in the
directory), and `--json` emits the {class}`~open_atp.benchmark.BenchmarkResult` as
JSON.

The CLI loads credentials from a `.env` in the working directory (or a parent), so
provers like Aristotle find their API keys without exporting them by hand.
