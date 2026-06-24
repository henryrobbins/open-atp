(prover-aristotle)=
# AristotleProver

The {class}`~open_atp.provers.aristotle.AristotleProver` wraps Harmonic's hosted
[Aristotle](https://www.harmonic.fun/) API. No agentic sandbox is needed for
generation — the lake project is handed to the hosted agent via `aristotlelib`
(submit → wait → download), the returned archive is unpacked over the workdir, and
the shared {class}`~open_atp.verify.Verifier` does the final check in a local
Docker sandbox. This is the platform's simplest end-to-end slice.

## Authentication

The prover reads an API key from the environment variable named by
{attr}`~open_atp.provers.aristotle.AristotleProverConfig.api_key_env` (default
`ARISTOTLE_API_KEY`). Set it on the host:

```bash
export ARISTOTLE_API_KEY=...
```

or add it to a `.env` file in your project.

## Usage

```python
from open_atp.backends.docker import DockerBackend, DockerConfig
from open_atp.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_atp.provers.aristotle import AristotleProver, AristotleProverConfig

backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
config = AristotleProverConfig(
    image=DEFAULT_IMAGE,
    supported_toolchain=DEFAULT_TOOLCHAIN,
)
prover = AristotleProver(config, verification_backend=backend)
```

The remote interaction is isolated in
`AristotleProver._submit_and_download`, so tests can stand in a fake result without
touching the network or an API key. See {doc}`../user_guide/run_provers` for an
end-to-end run and {class}`~open_atp.provers.aristotle.AristotleProverConfig` in the
{doc}`../api/provers` reference for configuration.

The prompt submitted to the hosted agent is the task's `instructions` when set,
otherwise Aristotle's own default prompt (the agent CLI harnesses share a longer,
tool-specific prompt instead):

:::{dropdown} Default Aristotle prompt
:icon: code
```{literalinclude} ../../src/open_atp/provers/aristotle.py
:language: python
:start-after: _DEFAULT_PROMPT = (
:end-before: END _DEFAULT_PROMPT
```
:::

:::{note}
Aristotle runs are billed by Harmonic against your `ARISTOTLE_API_KEY`. Verification
still happens locally in your own Docker sandbox.
:::
