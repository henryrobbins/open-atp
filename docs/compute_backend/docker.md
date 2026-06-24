# Docker

`open-atp` uses Docker to isolate agent working directories and to provide a
[Lean](https://lean-lang.org/) + [Mathlib](https://leanprover-community.github.io/)
sandbox with a warm `olean` cache. The same image backs both *generation* (the agent
runs inside it) and *verification* (the shared verifier compiles candidate files
inside it).

## Install Docker

Install either [Docker Desktop](https://docs.docker.com/desktop/) (recommended) or
[Docker Engine](https://docs.docker.com/engine/). Both include the `docker` CLI.
Verify the daemon is running with:

```bash
docker images
```

## Build the base image

The image is built from the `Dockerfile` under `images/`. It pins the supported Lean
toolchain ({data}`~open_atp.images.DEFAULT_TOOLCHAIN`) and pre-builds a Mathlib
`olean` cache, so the first build is expected to take a while.

```bash
docker build -t open-atp:latest images/
```

Run `docker images` to verify the `open-atp` image was created. The large size is
mostly attributable to the bundled Mathlib library.

```
$ docker images
REPOSITORY   TAG       IMAGE ID       CREATED         SIZE
open-atp    latest    8c164dafcbc3   26 hours ago    12GB
```

The image bakes a warm Mathlib `olean` cache at `/workspace/.lake`; the
{class}`~open_atp.backends.docker.DockerBackend` symlinks each workdir's `.lake` to
it so projects build against the cache instead of compiling Mathlib from scratch. See
the `Dockerfile` below.

:::{dropdown} `images/Dockerfile`
:icon: code
```{literalinclude} ../../images/Dockerfile
:language: docker
```
:::

## Using the Docker backend

The {func}`~open_atp.verify.docker_verifier` helper wires up a verifier
against a local Docker sandbox running `open-atp:latest`:

```python
from open_atp.lean import LeanProject
from open_atp.verify import docker_verifier

report = docker_verifier().verify(LeanProject("path/to/lake/project"))
```

Provers take a verification backend explicitly. Construct a
{class}`~open_atp.backends.docker.DockerBackend` and pass it in (see
{doc}`../user_guide/run_provers`).

## Docker resources

Docker lets you configure resource allocation. To run multiple agents in parallel,
budget roughly ~2 CPUs and ~3GB memory per agent on top of the daemon's baseline.

:::{warning}
It is not recommended to allocate *all* of your machine's resources in any single
category.
:::
