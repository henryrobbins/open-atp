# Aristotle

```{include} _meta_aristotle.md
:parser: myst
```

Harmonic offers API access to their advanced formal reasoning agent, [Aristotle](https://www.harmonic.fun/). The {class}`~open_atp.provers.aristotle.AristotleProver` hands the lake project to the hosted agent via the Aristotle Python package: `aristotlelib`. The prover still requires a compute backend to run the final verification on Aristotle's returned output.

## Authentication

By default the prover reads the Harmonic API key from the host environment:

```bash
export ARISTOTLE_API_KEY=...
```

It is recommended to define this in a `.env` file in your project root. Alternatively, pass it explicitly as the `api_key` argument to {class}`~open_atp.provers.aristotle.AristotleProver`.

```{testcode}
from open_atp.backends.docker import DockerBackend
from open_atp.provers.aristotle import AristotleProver

prover = AristotleProver(api_key="sk-...", backend=DockerBackend())
```

## Using the prover

### Run via the Python API

The simplest way to run the prover is through {func}`~open_atp.config.standard_prover` which uses a standard configuration. Here, we prove the {ref}`MUL_REORDER` example theorem:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover
from open_atp.examples import EXAMPLE, example_task

task = example_task(EXAMPLE.MUL_REORDER)
prover = standard_prover("aristotle", backend=DockerBackend())
result = prover.prove(task, output_dir=Path("demo"))
```

### Run via the CLI

The standard prover can also be run from the CLI:

```bash
open-atp prove path/to/task.lean output_dir aristotle
```

## Prover details

The agent prompt passed to Aristotle is simple:

```{literalinclude} ../../src/open_atp/provers/aristotle.py
:language: python
:start-after: PROVER_PROMPT = (
:end-before: )
```

Since Aristotle is closed-source, no other prover details are known. However, `aristotlelib` does provide rich agent logs which are written to the logs subdirectory of the output folder.

(tracking-cost-and-usage-aristotle)=
## Tracking cost and usage

Aristotle is currently available for **free**! The cost is reported as `0.0` in {class}`~open_atp.provers.base.ProofResult`.
