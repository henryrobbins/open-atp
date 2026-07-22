# ![OpenATP](_static/logo_light.svg){.hero-logo .only-light}![OpenATP](_static/logo_dark.svg){.hero-logo .only-dark}

**OpenATP** is an open-source Python package providing a common interface for **Automated Theorem Proving (ATP)**. OpenATP focuses on recent **agentic ATP methods** that prove formal statements in [Lean](https://lean-lang.org/). Each method runs in an isolated sandbox, either locally with Docker or remotely with [Modal](https://modal.com/). OpenATP also provides benchmarking utilities to run methods on **common datasets**.

```{image} ../banner/banner.png
:alt: OpenATP banner
:align: center
:width: 80%
```

Follow the {doc}`/installation` instructions to install the `open-atp` Python package. Use the {doc}`guides/index` to configure your compute backend ({doc}`Docker </guides/docker>` or {doc}`Modal </guides/modal>`) and then {doc}`run </guides/run_provers>` and {doc}`benchmark </guides/benchmark>` provers. See {doc}`/provers/index` for a complete list of the standard provers and {doc}`/datasets` for supported proof-synthesis datasets. If you are interested in contributing to the OpenATP project, you can find useful resources in {doc}`/development/index`! Lastly, we provide complete {doc}`API </api/index>` and {doc}`CLI </cli>` references.

```{toctree}
:maxdepth: 1
:caption: Contents

installation
guides/index
provers/index
datasets
examples
development/index
api/index
cli
citations
```
