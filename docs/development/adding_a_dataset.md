# Adding a dataset

A *dataset* is one of the public Lean benchmarks {func}`~open_atp.benchmark.download_dataset`
can fetch — a {class}`~open_atp.benchmark.DATASET` member that maps to a GitHub repo and
the subdirectory holding its `.lean` task files. `download_dataset` sparse-clones just
that subdirectory into a task directory, ready for
{func}`~open_atp.benchmark.tasks_from_dir`. Adding one is two small edits in
`src/open_atp/benchmark.py` plus docs.

## 1. Add the enum member

Add a member to {class}`~open_atp.benchmark.DATASET`. Its value is the directory name the
dataset clones into (also the `open-atp download` CLI choice — those are derived from
`[d.value for d in DATASET]`, so the CLI picks it up with no further change):

```python
class DATASET(Enum):
    ...
    #: `MyBench <https://github.com/owner/MyBench>`_ description.
    MYBENCH = "mybench"
```

## 2. Map it to a repo and subdirectory

Add the GitHub `owner/name` and the path to the `.lean` task files to `_DATASETS`:

```python
_DATASETS = {
    ...
    DATASET.MYBENCH: ("owner/MyBench", "lean4/src"),
}
```

The subdirectory is what gets sparse-checked-out and is what
{func}`~open_atp.benchmark.tasks_from_dir` walks — each loose `.lean` file becomes a task
named by its stem, each subdirectory a multi-file task.

## 3. Mind the toolchain pin

The verifier rejects projects whose `lean-toolchain` doesn't match the sandbox image's
pin (the default skeleton is **v4.28.0**). If the dataset targets a different Lean (as
PutnamBench does with v4.27.0), it must be staged against a matching skeleton —
`tasks_from_dir(src, skeleton=...)`. Note the pin in the `DATASET` member's docstring so
users know.

## 4. Document it

- Add a row to the dataset table in `docs/user_guide/benchmark.md` (benchmark, `DATASET`
  member, toolchain, paper, source).
- The {class}`~open_atp.benchmark.DATASET` autodoc in `docs/api/benchmark.md` renders
  member docstrings automatically — no edit needed beyond the `#:` comment in step 1.
- Add the dataset's citation bibtex under *Citing the benchmarks* in
  `docs/user_guide/benchmark.md`.

## 5. Test it

Mirror the dataset cases in `tests/test_benchmark.py`. Keep the sparse-clone /
network-touching path out of the default suite.

## Verify the result

```bash
make check
make docs        # -W: a broken xref or stale table fails the build
```

End-to-end, the new dataset now works from the CLI:

```bash
open-atp download mybench datasets         # -> datasets/mybench/<subdir>
open-atp benchmark datasets/mybench/<subdir> --compute docker
```
