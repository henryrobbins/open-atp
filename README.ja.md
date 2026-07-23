<p align="center">
  <img src="docs/_static/logo_light.svg" alt="OpenATP" width="360">
</p>

[English](README.md) | [简体中文](README.zh-CN.md)

[![PyPI](https://img.shields.io/pypi/v/open-atp.svg)](https://pypi.org/project/open-atp/)
[![Docs](https://readthedocs.org/projects/open-atp/badge/?version=latest)](https://open-atp.readthedocs.io/en/latest/)
[![CI](https://github.com/henryrobbins/open-atp/actions/workflows/ci-python.yml/badge.svg)](https://github.com/henryrobbins/open-atp/actions/workflows/ci-python.yml)
[![codecov](https://codecov.io/gh/henryrobbins/open-atp/branch/main/graph/badge.svg?flag=src)](https://codecov.io/gh/henryrobbins/open-atp)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**OpenATP** は、**自動定理証明（Automated Theorem Proving、ATP）**のための共通インターフェースを提供するオープンソースの Python パッケージです。OpenATP は、[Lean](https://lean-lang.org/) の形式的な命題を証明する近年の**エージェント型 ATP 手法**に重点を置いています。各手法は隔離されたサンドボックス内で実行され、Docker を使ってローカルで動かすことも、[Modal](https://modal.com/) を使ってリモートで動かすこともできます。また OpenATP は、**一般的なデータセット**上で手法を実行するためのベンチマークツールも提供します。

<div align="center">
  <img src="banner/banner.png" alt="OpenATP banner" width="80%">
</div>

## インストール

```bash
pip install open-atp
```

`OpenATP` は各証明器（Claude Code、Codex、OpenCode など）を Docker コンテナ内で実行します。証明器を実行する前にイメージをビルドする必要があります。

```bash
open-atp build-docker-image
```

証明器ごとに認証要件が異なります。認証方法については各[証明器](https://open-atp.readthedocs.io/en/latest/provers/index.html)のページを参照し、ホストの現在の認証状態は次のコマンドで確認してください。

```bash
open-atp auth-status
```

## クイックスタート

CLI から lake プロジェクト（または `.lean` ファイル）内の `sorry` を補完します。

```bash
open-atp prove path/to/project runs/example claude
```

プログラムから実行することもできます。以下は単純な定理の例です。

```python
from open_atp import standard_prover
from open_atp.backends import DockerBackend
from open_atp.examples import EXAMPLE, example_task

prover = standard_prover("claude", backend=DockerBackend())
task = example_task(EXAMPLE.MUL_REORDER)

result = prover.prove(task, output_dir="runs/example")
```

## 利用可能な証明器

`ID` は、`standard_prover`、CLI の `prove` コマンドの `prover` 引数、および `benchmark` コマンドの `-p/--provers` オプションで使用する標準的な証明器名です。[証明器](https://open-atp.readthedocs.io/en/latest/provers/index.html)も参照してください。

<!-- BEGIN PROVER TABLE (generated from docs/provers.yaml) -->
| 証明器 | ID | スキル | MCP | 論文 | ソース |
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

## 一般的なデータセットのダウンロード

OpenATP は、一般的な証明合成ベンチマークをダウンロードするためのツールを提供します（[データセットのダウンロード](https://open-atp.readthedocs.io/en/latest/guides/benchmark.html#downloading-a-dataset)を参照）。利用可能なデータセットは `DATASET` 列挙型に定義されています。

| ベンチマーク | `DATASET` | ツールチェーン | 論文 | ソース |
| --- | --- | --- | --- | --- |
| 例 | `EXAMPLES` | `v4.28.0` | — | [ドキュメント](https://open-atp.readthedocs.io/en/latest/examples.html) |
| PutnamBench | `PUTNAM` | `v4.27.0` | [Tsoukalas et al. 2024](https://arxiv.org/abs/2407.11214) | [trishullab/PutnamBench](https://github.com/trishullab/PutnamBench) |
| FATE-H | `FATE_H` | `v4.28.0` | [Jiang et al. 2025](https://arxiv.org/abs/2511.02872) | [frenzymath/FATE-H](https://github.com/frenzymath/FATE-H) |
| FATE-M | `FATE_M` | `v4.28.0` | [Jiang et al. 2025](https://arxiv.org/abs/2511.02872) | [frenzymath/FATE-M](https://github.com/frenzymath/FATE-M) |
| FATE-X | `FATE_X` | `v4.28.0` | [Jiang et al. 2025](https://arxiv.org/abs/2511.02872) | [frenzymath/FATE-X](https://github.com/frenzymath/FATE-X) |

## 引用

研究や開発で `OpenATP` を使用する場合は、次のように引用してください。

```bibtex
@software{openatp,
  title = {OpenATP: Open Automated Theorem Proving},
  author = {Henry Robbins},
  year = {2026},
  publisher = {GitHub},
  url = {https://github.com/henryrobbins/open-atp}
}
```

OpenATP には関連論文のある証明器が含まれており、エージェント型定理証明を改善するための一般的なオープンソースツールも同梱されています。参考文献の完全な一覧については、[引用](https://open-atp.readthedocs.io/en/latest/citations.html)を参照してください。

## 開発

開発情報については `AGENTS.md` を参照してください。

## ライセンス

[MIT](LICENSE)
