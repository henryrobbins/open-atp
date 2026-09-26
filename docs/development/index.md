# Development

Guides for extending `open-atp`. They assume the development setup below and that you
have read the engineering reference in {github}`AGENTS.md </blob/main/AGENTS.md>`.

## Set up from source

Clone the repository and install with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/henryrobbins/open-atp.git
cd open-atp
uv sync
```

```{toctree}
:maxdepth: 1

adding_a_prover
adding_a_dataset
testing
```
