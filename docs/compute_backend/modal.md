# Modal

`open-atp` can run commands in [Modal](https://modal.com/) Sandboxes instead of
local Docker containers. Each run gets its own cloud Sandbox, which avoids duplicate
[Lean](https://lean-lang.org/) environments and lets many runs proceed in parallel
without consuming local resources.

## Install Modal

Modal ships as a dependency of `open-atp`. Authenticate the `modal` CLI against your
Modal workspace (this writes a token to `~/.modal.toml`):

```bash
modal setup
```

Verify the credentials are working with:

```bash
modal app list
```

## Build the Modal image

Unlike Docker, Modal Sandboxes have an isolated filesystem and ignore a container
`USER`, so the sandbox image is built and published programmatically (rather than
from `images/Dockerfile`). It installs the Lean toolchain and agent CLIs globally as
root and bakes the same warm Mathlib `olean` cache. Build and publish it with:

```bash
open-atp build-modal-image
```

This publishes a named Modal image (`open-atp` by default) that the backend looks
up at run time. The name must match `ModalConfig.image` (sans `:tag`); pass `--name`
to publish under a different name and `--force` to rebuild even when Modal has cached
layers. As with Docker, the first build pre-builds Mathlib and is expected to take a
while.

## Using the Modal backend

A {class}`~open_atp.backends.modal.ModalBackend` is constructed from a
{class}`~open_atp.backends.modal.ModalConfig` and is a drop-in
{class}`~open_atp.backends.base.ComputeBackend` — substitute it for the
`DockerBackend` anywhere a verification or generation backend is expected:

```python
from open_atp.backends.modal import ModalBackend, ModalConfig
from open_atp.images import DEFAULT_IMAGE

backend = ModalBackend(ModalConfig(image=DEFAULT_IMAGE, cpu=4.0, memory_mib=4096))
```

`cpu` is a guaranteed floor of cores (the Sandbox may burst higher) and `memory_mib`
is in MiB. See the {doc}`/api/backends` reference for the full set of options.

For the common case of verifying against the published image, the
{func}`~open_atp.verify.modal_verifier` helper wires up a
{class}`~open_atp.verify.Verifier` for you — the Modal counterpart of
{func}`~open_atp.verify.docker_verifier`:

```python
from open_atp.verify import modal_verifier

verifier = modal_verifier()
```

:::{note}
Running on Modal incurs cloud compute charges billed by your Modal workspace. See
[Modal's pricing](https://modal.com/pricing) for details.
:::
