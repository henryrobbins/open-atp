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

The `open-atp ex-benchmark` CLI command runs exactly this sweep over all
{func}`~open_atp.config.standard_provers` and the five bundled
{class}`~open_atp.examples.EXAMPLE` tasks; pick the backend with
`--compute {docker,modal}`.

## Tasks from a directory

To benchmark a directory of `.lean` files (each a `sorry`'d task),
{func}`~open_atp.benchmark.tasks_from_dir` builds the `tasks` mapping: each loose
`.lean` file becomes a task named by its stem, and each subdirectory becomes one
multi-file task.

## Downloading a dataset

{func}`~open_atp.benchmark.download_dataset` fetches one of the included public
benchmarks — a sparse clone of just the task subdirectory — straight into a directory
ready for {func}`~open_atp.benchmark.tasks_from_dir`:

```python
from open_atp.benchmark import DATASET, download_dataset, run_benchmark, tasks_from_dir

src = download_dataset(DATASET.FATE_M, "datasets")  # datasets/fate-m/FATEM
result = run_benchmark(tasks_from_dir(src), provers, Path("runs/fate-m"))
```

Each {class}`~open_atp.benchmark.DATASET` member:

| Benchmark | `DATASET` | Toolchain | Paper | Source |
| --- | --- | --- | --- | --- |
| PutnamBench | `PUTNAM` | `v4.27.0` | [Tsoukalas et al. 2024](https://arxiv.org/abs/2407.11214) | [trishullab/PutnamBench](https://github.com/trishullab/PutnamBench) |
| FATE-H (hard) | `FATE_H` | `v4.28.0` | [Jiang et al. 2025](https://arxiv.org/abs/2511.02872) | [frenzymath/FATE-H](https://github.com/frenzymath/FATE-H) |
| FATE-M (medium) | `FATE_M` | `v4.28.0` | [Jiang et al. 2025](https://arxiv.org/abs/2511.02872) | [frenzymath/FATE-M](https://github.com/frenzymath/FATE-M) |
| FATE-X (extra) | `FATE_X` | `v4.28.0` | [Jiang et al. 2025](https://arxiv.org/abs/2511.02872) | [frenzymath/FATE-X](https://github.com/frenzymath/FATE-X) |

PutnamBench pins an older Lean than the default skeleton, so stage it against a
matching skeleton (`tasks_from_dir(src, skeleton=...)`).

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

## Citing the benchmarks

If you run these benchmarks, please cite their authors:

```bibtex
@article{jiang2025fate,
  title={Fate: A formal benchmark series for frontier algebra of multiple difficulty levels},
  author={Jiang, Jiedong and He, Wanyi and Wang, Yuefeng and Gao, Guoxiong and Hu, Yongle and Wang, Jingting and Guan, Nailin and Wu, Peihao and Dai, Chunbo and Xiao, Liang and others},
  journal={arXiv preprint arXiv:2511.02872},
  year={2025}
}

@article{tsoukalas2024putnambench,
  title={Putnambench: Evaluating neural theorem-provers on the putnam mathematical competition},
  author={Tsoukalas, George and Lee, Jasper and Jennings, John and Xin, Jimmy and Ding, Michelle and Jennings, Michael and Thakur, Amitayush and Chaudhuri, Swarat},
  journal={Advances in Neural Information Processing Systems},
  volume={37},
  pages={11545--11569},
  year={2024}
}
```
