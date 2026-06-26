# Installation

## Install the package

`open-atp` targets Python 3.12+. Install it with `pip`:

```bash
pip install open-atp
```

To work on `open-atp` itself, install from source instead — see
{doc}`development/index`.

## Quickstart

This quickstart compiles and checks a complete lake project in a local Docker
sandbox via the shared {class}`~open_atp.verify.Verifier`. It requires:

- **Docker** installed and the `open-atp:latest` image built (see
  {doc}`compute_backend/docker`).
- A **complete lake project** — a directory carrying its own `lean-toolchain` and
  `lake-manifest.json` — whose toolchain matches the image's pin
  ({attr}`~open_atp.images.Image.lean_toolchain`).

```python
from open_atp.lean import LeanProject
from open_atp.verify import docker_verifier

report = docker_verifier().verify(LeanProject("path/to/lake/project"))
print(report.verified, report.sorry_free, report.axioms)
```

To go further than verification and actually *fill* the `sorry`s, hand a project to
a prover. See {doc}`provers/index` for the prover catalogue and
{doc}`user_guide/run_provers` for an end-to-end walkthrough.

:::{note}
The input contract is a **full lake project**. The verifier rejects projects whose
pinned toolchain does not match the sandbox image
({class}`~open_atp.lean.ToolchainMismatch`) rather than failing deep in a
build.
:::
