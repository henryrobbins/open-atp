# Datasets

`open-atp` bundles several public Lean proof-synthesis benchmarks.
{func}`~open_atp.benchmark.download_dataset` fetches one — a sparse clone of just the
task subdirectory — into a directory ready for
{func}`~open_atp.benchmark.tasks_from_dir` (see {doc}`guides/benchmark`). The
``EXAMPLES`` entry is the package's bundled {class}`~open_atp.examples.EXAMPLE` set,
copied from the wheel rather than cloned.

Each {class}`~open_atp.benchmark.DATASET` member:

| Benchmark | `DATASET` | Toolchain | Paper | Source |
| --- | --- | --- | --- | --- |
| Bundled examples | `EXAMPLES` | `v4.28.0` | — | {doc}`examples` (shipped in the package) |
| PutnamBench | `PUTNAM` | `v4.27.0` | {cite:t}`tsoukalas2024putnambench` | [trishullab/PutnamBench](https://github.com/trishullab/PutnamBench) |
| FATE-H (hard) | `FATE_H` | `v4.28.0` | {cite:t}`jiang2025fate` | [frenzymath/FATE-H](https://github.com/frenzymath/FATE-H) |
| FATE-M (medium) | `FATE_M` | `v4.28.0` | {cite:t}`jiang2025fate` | [frenzymath/FATE-M](https://github.com/frenzymath/FATE-M) |
| FATE-X (extra) | `FATE_X` | `v4.28.0` | {cite:t}`jiang2025fate` | [frenzymath/FATE-X](https://github.com/frenzymath/FATE-X) |

PutnamBench pins an older Lean than the default skeleton, so stage it against a
matching skeleton (`tasks_from_dir(src, skeleton=...)`).
