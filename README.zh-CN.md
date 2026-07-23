<p align="center">
  <img src="docs/_static/logo_light.svg" alt="OpenATP" width="360">
</p>

[English](README.md) | [日本語](README.ja.md)

[![PyPI](https://img.shields.io/pypi/v/open-atp.svg)](https://pypi.org/project/open-atp/)
[![Docs](https://readthedocs.org/projects/open-atp/badge/?version=latest)](https://open-atp.readthedocs.io/en/latest/)
[![CI](https://github.com/henryrobbins/open-atp/actions/workflows/ci-python.yml/badge.svg)](https://github.com/henryrobbins/open-atp/actions/workflows/ci-python.yml)
[![codecov](https://codecov.io/gh/henryrobbins/open-atp/branch/main/graph/badge.svg?flag=src)](https://codecov.io/gh/henryrobbins/open-atp)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**OpenATP** 是一个开源 Python 包，为**自动定理证明（Automated Theorem Proving，ATP）**提供统一接口。OpenATP 专注于近期的**智能体式 ATP 方法**，这些方法用于证明 [Lean](https://lean-lang.org/) 中的形式化命题。每种方法都在隔离的沙箱中运行：可以通过 Docker 在本地运行，也可以通过 [Modal](https://modal.com/) 远程运行。OpenATP 还提供基准测试工具，用于在**常用数据集**上运行这些方法。

<div align="center">
  <img src="banner/banner.png" alt="OpenATP banner" width="80%">
</div>

## 安装

```bash
pip install open-atp
```

`OpenATP` 会在 Docker 容器中运行每个证明器（例如 Claude Code、Codex、OpenCode）。运行任何证明器之前，必须先构建镜像：

```bash
open-atp build-docker-image
```

每个证明器都有各自的身份验证要求。请参阅相应的[证明器](https://open-atp.readthedocs.io/en/latest/provers/index.html)页面了解身份验证说明，并使用以下命令检查主机当前的身份验证状态：

```bash
open-atp auth-status
```

## 快速开始

通过 CLI 补全 lake 项目（或 `.lean` 文件）中的 `sorry`：

```bash
open-atp prove path/to/project runs/example claude
```

也可以通过编程方式完成。下面是一个简单的示例定理：

```python
from open_atp import standard_prover
from open_atp.backends import DockerBackend
from open_atp.examples import EXAMPLE, example_task

prover = standard_prover("claude", backend=DockerBackend())
task = example_task(EXAMPLE.MUL_REORDER)

result = prover.prove(task, output_dir="runs/example")
```

## 可用的证明器

`ID` 是标准证明器名称，供 `standard_prover`、CLI `prove` 命令的 `prover` 参数以及 `benchmark` 命令的 `-p/--provers` 选项使用。另请参阅[证明器](https://open-atp.readthedocs.io/en/latest/provers/index.html)。

<!-- BEGIN PROVER TABLE (generated from docs/provers.yaml) -->
| 证明器 | ID | 技能 | MCP | 论文 | 源代码 |
| --- | --- | --- | --- | --- | --- |
| [Claude Code](docs/provers/claude_code.md) | `claude` | [leanprover](https://github.com/leanprover/skills), [lean4](https://github.com/cameronfreer/lean4-skills) | ✓ | — | — |
| [Codex](docs/provers/codex.md) | `codex` | [leanprover](https://github.com/leanprover/skills) | ✓ | — | [GitHub](https://github.com/openai/codex) |
| [DeepSeek](docs/provers/deepseek.md) | `deepseek` | [leanprover](https://github.com/leanprover/skills) | ✓ | — | [GitHub](https://github.com/deepseek-ai) |
| [Grok](docs/provers/grok.md) | `grok` | [leanprover](https://github.com/leanprover/skills) | ✓ | — | — |
| [AxProverBase](docs/provers/axproverbase.md) | `axproverbase` | — | ✗ | [Requena et al. 2026](https://openreview.net/forum?id=E30g7bO7rU) | [GitHub](https://github.com/Axiomatic-AI/ax-prover-base) |
| [Leanstral](docs/provers/leanstral.md) | `leanstral` | [leanprover](https://github.com/leanprover/skills) | ✓ | [Leanstral (blog)](https://mistral.ai/news/leanstral) | [HuggingFace](https://huggingface.co/mistralai/Leanstral-2603) |
| [Kimi Code](docs/provers/kimi.md) | `kimi` | [leanprover](https://github.com/leanprover/skills) | ✓ | — | [GitHub](https://github.com/MoonshotAI/kimi-code) |
| [Numina](docs/provers/numina.md) | `numina` | — | ✓ | [Liu et al. 2026](https://arxiv.org/abs/2601.14027) | [GitHub](https://github.com/project-numina/numina-lean-agent) |
| [Aristotle](docs/provers/aristotle.md) | `aristotle` | — | — | [Achim et al. 2025](https://arxiv.org/abs/2510.01346) | — |
<!-- END PROVER TABLE -->

## 下载常用数据集

OpenATP 提供用于下载常用证明合成基准测试数据集的工具（请参阅[下载数据集](https://open-atp.readthedocs.io/en/latest/guides/benchmark.html#downloading-a-dataset)）。可用的数据集列在 `DATASET` 枚举中。

| 基准测试 | `DATASET` | 工具链 | 论文 | 来源 |
| --- | --- | --- | --- | --- |
| 示例 | `EXAMPLES` | `v4.28.0` | — | [文档](https://open-atp.readthedocs.io/en/latest/examples.html) |
| PutnamBench | `PUTNAM` | `v4.27.0` | [Tsoukalas et al. 2024](https://arxiv.org/abs/2407.11214) | [trishullab/PutnamBench](https://github.com/trishullab/PutnamBench) |
| FATE-H | `FATE_H` | `v4.28.0` | [Jiang et al. 2025](https://arxiv.org/abs/2511.02872) | [frenzymath/FATE-H](https://github.com/frenzymath/FATE-H) |
| FATE-M | `FATE_M` | `v4.28.0` | [Jiang et al. 2025](https://arxiv.org/abs/2511.02872) | [frenzymath/FATE-M](https://github.com/frenzymath/FATE-M) |
| FATE-X | `FATE_X` | `v4.28.0` | [Jiang et al. 2025](https://arxiv.org/abs/2511.02872) | [frenzymath/FATE-X](https://github.com/frenzymath/FATE-X) |

## 引用

如果你在工作中使用了 `OpenATP`，请按以下方式引用：

```bibtex
@software{openatp,
  title = {OpenATP: Open Automated Theorem Proving},
  author = {Henry Robbins},
  year = {2026},
  publisher = {GitHub},
  url = {https://github.com/henryrobbins/open-atp}
}
```

OpenATP 包含带有相关论文的证明器，并集成了用于改进智能体式定理证明的常用开源工具。完整参考文献列表请参阅[引用](https://open-atp.readthedocs.io/en/latest/citations.html)。

## 开发

开发信息请参阅 `AGENTS.md`。

## 许可证

[MIT](LICENSE)
