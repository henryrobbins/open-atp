<p align="center">
  <img src="docs/_static/logo_light.svg" alt="OpenATP" width="360">
</p>

[![PyPI](https://img.shields.io/pypi/v/open-atp.svg)](https://pypi.org/project/open-atp/)
[![Docs](https://readthedocs.org/projects/open-atp/badge/?version=latest)](https://open-atp.readthedocs.io/en/latest/)
[![CI](https://github.com/henryrobbins/open-atp/actions/workflows/ci-python.yml/badge.svg)](https://github.com/henryrobbins/open-atp/actions/workflows/ci-python.yml)
[![codecov](https://codecov.io/gh/henryrobbins/open-atp/branch/main/graph/badge.svg?flag=src)](https://codecov.io/gh/henryrobbins/open-atp)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**OpenATP** is an open-source Python package providing a common interface for **Automated Theorem Proving (ATP)**. OpenATP focuses on recent **agentic ATP methods** that prove formal statements in [Lean](https://lean-lang.org/). Each method runs in an isolated sandbox, either locally with Docker or remotely with [Modal](https://modal.com/). OpenATP also provides benchmarking utilities to run methods on **common datasets**.

<div align="center">
  <img src="banner/banner.png" alt="OpenATP banner" width="80%">
</div>

## Installation

```bash
pip install open-atp
```

`OpenATP` runs each prover (e.g., Claude Code, Codex, OpenCode) in a
Docker container. The image must be built before running any prover:

```bash
open-atp build-docker-image
```

Each prover has its own authentication requirements. See each [prover](https://open-atp.readthedocs.io/en/latest/provers/index.html) page for its authentication instructions.

## Quickstart

Complete the `sorry`s in a lake project (or a `.lean` file) from the CLI:

```bash
open-atp prove path/to/project runs/example claude
```

Or programmatically, here on a simple example theorem:

```python
from open_atp import standard_prover
from open_atp.backends import DockerBackend
from open_atp.examples import EXAMPLE, example_task

prover = standard_prover("claude", backend=DockerBackend())
task = example_task(EXAMPLE.MUL_REORDER)

result = prover.prove(task, output_dir="runs/example")
```

## Available provers

The `ID` is the standard prover name used by `standard_prover`, the CLI `prove` command's `prover` argument, and the `benchmark` command's `-p/--provers` option. Also see [Provers](https://open-atp.readthedocs.io/en/latest/provers/index.html).

<!-- BEGIN PROVER TABLE (generated from docs/provers.yaml) -->
| Prover | ID | Skills | MCP | Paper | Source |
| --- | --- | --- | --- | --- | --- |
| [Claude Code](docs/provers/claude_code.md) | `claude` | [leanprover](https://github.com/leanprover/skills), [lean4](https://github.com/cameronfreer/lean4-skills) | ✓ | — | — |
| [Codex](docs/provers/codex.md) | `codex` | [leanprover](https://github.com/leanprover/skills) | ✓ | — | [GitHub](https://github.com/openai/codex) |
| [DeepSeek](docs/provers/deepseek.md) | `deepseek` | [leanprover](https://github.com/leanprover/skills) | ✓ | — | [GitHub](https://github.com/deepseek-ai) |
| [Grok](docs/provers/grok.md) | `grok` | [leanprover](https://github.com/leanprover/skills) | ✓ | — | [xAI](https://x.ai/api) |
| [AxProverBase](docs/provers/axproverbase.md) | `axproverbase` | — | ✗ | [Requena et al. 2026](https://openreview.net/forum?id=E30g7bO7rU) | [GitHub](https://github.com/Axiomatic-AI/ax-prover-base) |
| [Leanstral](docs/provers/leanstral.md) | `leanstral` | [leanprover](https://github.com/leanprover/skills) | ✓ | [Leanstral (blog)](https://mistral.ai/news/leanstral) | [HuggingFace](https://huggingface.co/mistralai/Leanstral-2603) |
| [Numina](docs/provers/numina.md) | `numina` | — | ✓ | [Liu et al. 2026](https://arxiv.org/abs/2601.14027) | [GitHub](https://github.com/project-numina/numina-lean-agent) |
| [Aristotle](docs/provers/aristotle.md) | `aristotle` | — | — | [Achim et al. 2025](https://arxiv.org/abs/2510.01346) | — |
<!-- END PROVER TABLE -->

## Download common datasets

OpenATP provides utilities to download common proof-synthesis benchmarks (see [Downloading a dataset](https://open-atp.readthedocs.io/en/latest/guides/benchmark.html#downloading-a-dataset)). The available datasets are listed in the `DATASET` enum.

| Benchmark | `DATASET` | Toolchain | Paper | Source |
| --- | --- | --- | --- | --- |
| Examples | `EXAMPLES` | `v4.28.0` | — | [docs](https://open-atp.readthedocs.io/en/latest/examples.html) |
| PutnamBench | `PUTNAM` | `v4.27.0` | [Tsoukalas et al. 2024](https://arxiv.org/abs/2407.11214) | [trishullab/PutnamBench](https://github.com/trishullab/PutnamBench) |
| FATE-H | `FATE_H` | `v4.28.0` | [Jiang et al. 2025](https://arxiv.org/abs/2511.02872) | [frenzymath/FATE-H](https://github.com/frenzymath/FATE-H) |
| FATE-M | `FATE_M` | `v4.28.0` | [Jiang et al. 2025](https://arxiv.org/abs/2511.02872) | [frenzymath/FATE-M](https://github.com/frenzymath/FATE-M) |
| FATE-X | `FATE_X` | `v4.28.0` | [Jiang et al. 2025](https://arxiv.org/abs/2511.02872) | [frenzymath/FATE-X](https://github.com/frenzymath/FATE-X) |

## Citing

If you use `OpenATP` in your work, please cite it:

```bibtex
@software{openatp,
  title = {OpenATP: Open Automated Theorem Proving},
  author = {Henry Robbins},
  year = {2026},
  publisher = {GitHub},
  url = {https://github.com/henryrobbins/open-atp}
}
```

OpenATP includes provers with associated papers and bundles popular open-source tools for improving agentic theorem proving. Please see [Citations](https://open-atp.readthedocs.io/en/latest/citations.html) for a comprehensive list of references.

## Development

See `AGENTS.md` for development information.

## License

[MIT](LICENSE)
