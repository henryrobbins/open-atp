# Grok

```{include} _meta_grok.md
:parser: myst
```

Use xAI's [Grok](https://x.ai/) as an automated theorem prover. This prover runs on the OpenCode harness pinned to `grok-4.5`. See {doc}`/provers/opencode` for harness details.

:::{warning}
The `grok-4.5` model is geo-gated: requests are refused with `403 permission-denied` from some countries. On the {class}`~open_atp.backends.modal.ModalBackend`, the default region is `us` to ensure availability when using Modal compute. If you configure Modal to use non-US regions, the Grok prover may fail.
:::

## Authentication

There are two ways to authenticate with xAI's Grok model: via an API key or via {ref}`OpenCode Authentication <opencode-authentication>`.

It is recommended to sign up for a [SuperGrok subscription](https://grok.com/) and use the OpenCode login method. This bills usage against your subscription and is more cost-effective than using an API key.

### API key

Create an xAI account at [xAI Console](https://console.x.ai/) and generate an API key. Then set the key in the environment:

```bash
export XAI_API_KEY=...
```

It is recommended to define this in a `.env` file in your project root. Alternatively, pass the key to the harness explicitly:

```{testcode}
from open_atp.harness import OpenCodeHarness

OpenCodeHarness(provider="xai", model="grok-4.5", api_key="sk-...")
```

(opencode-grok-login)=
### OpenCode login

The xAI provider supports both API key and OAuth login methods. Here, we used the "xAI Grok OAuth (SuperGrok Subscription)" login method.

First, sign up for a [SuperGrok subscription](https://grok.com/). Then follow the instructions in {ref}`OpenCode Authentication <opencode-authentication>` to log in with the `xAI` provider's OAuth flow. 

You can then use the harness with the `auth="login"` argument to forward the xAI credentials into the agent sandbox.

```{testcode}
from open_atp.harness import OpenCodeHarness

OpenCodeHarness(provider="xai", model="grok-4.5", auth="login")
```

:::{warning}
**Token expiration:** The xAI entry in `~/.local/share/opencode/auth.json` expires roughly **6 hours** after it is minted, and it only renews when OpenCode runs *on the host*. A sandboxed run will fail if the token expires mid-run. Check the time remaining with `open-atp auth-status`, and log in again on the host before a long benchmark.
:::

## Using the prover

### Standard prover via Python API

The simplest way to run the prover is through {func}`~open_atp.config.standard_prover`, which pins the `grok-4.5` model on the OpenCode harness. The standard prover uses the {ref}`opencode-grok-login` authentication method.

Here, we prove the {ref}`MUL_REORDER` example theorem:

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover
from open_atp.examples import EXAMPLE, example_task

task = example_task(EXAMPLE.MUL_REORDER)
prover = standard_prover("grok", backend=DockerBackend())
result = prover.prove(task, output_dir=Path("demo"))
```

### Standard prover via CLI

The standard prover can also be run from the CLI:

```bash
open-atp prove path/to/task.lean output_dir grok
```

### Customizing the prover

To override knobs like `model` and `effort`, construct the harness directly.

```{testcode}
from pathlib import Path

from open_atp.backends.docker import DockerBackend
from open_atp.examples import EXAMPLE, example_task
from open_atp.harness import OpenCodeHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers import AgentProver

task = example_task(EXAMPLE.MUL_REORDER)
prover = AgentProver(
    harness=OpenCodeHarness(
        provider="xai", model="grok-4.5", auth="login", effort="medium"
    ),
    backend=DockerBackend(image=DEFAULT_IMAGE),
)
result = prover.prove(task, output_dir=Path("demo"))
```

## Tracking cost and usage

Cost is captured from the OpenCode CLI's per-call breakdown and summed into `cost_usd` on {class}`~open_atp.provers.base.ProofResult` (see {ref}`tracking-cost-and-usage-opencode`). If you use an API key, you can monitor usage on the provider dashboard at [xAI Console](https://console.x.ai). If you use a SuperGrok subscription with OAuth, you can monitor usage on the provider dashboard at [Grok Usage](https://grok.com/?_s=usage).

