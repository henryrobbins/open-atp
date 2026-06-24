"""The prover registry: name -> a constructed :class:`AutomatedProver`.

The package is a library: a caller picks a prover, constructs it against a compute
backend, and calls :meth:`~open_atp.provers.base.AutomatedProver.prove` directly::

    from open_atp import PROVERS, get_prover
    from open_atp.backends.docker import DockerBackend, DockerConfig

    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    prover = get_prover(PROVERS.CLAUDE, verification_backend=backend)
    result = prover.prove(task, output_dir)

This module is just the registry/factory over that flow:

* :class:`PROVERS` -- the available prover names as an enum.
* :func:`available_provers` -- list them.
* :func:`get_prover` -- construct one by name with a shared image + verify backend.

Backends: the verifier (cheap final check) and the agent (generation) backends are
kept separate -- the split already exists in ``AgentProver`` -- so a job can run
generation on Modal and the check on local Docker.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from open_atp.backends.base import ComputeBackend
from open_atp.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_atp.provers.agent_prover import AgentProver, AgentProverConfig
from open_atp.provers.aristotle import AristotleProver, AristotleProverConfig
from open_atp.provers.base import AutomatedProver, AutomatedProverConfig
from open_atp.provers.numina import NuminaProver, NuminaProverConfig

# --- prover registry / factory ---------------------------------------------


class PROVERS(StrEnum):
    """The provers :func:`get_prover` accepts.

    Each member's value is the registry key. ``agent:<harness>`` members select an
    :class:`~open_atp.provers.agent_prover.AgentProver` harness; :attr:`CLAUDE` is the
    bare ``agent`` (config default, ``claude_code``).
    """

    CLAUDE = "agent"
    CODEX = "agent:codex"
    OPENCODE = "agent:opencode"
    AXPROVER = "agent:axprover"
    NUMINA = "numina"
    VIBE = "vibe"
    ARISTOTLE = "aristotle"


@dataclass(frozen=True)
class _Entry:
    """A registry row: which classes back a prover, plus baked-in config."""

    prover_cls: type[AutomatedProver]
    config_cls: type[AutomatedProverConfig]
    # Config overrides implied by the name itself (e.g. ``agent:codex`` -> codex).
    defaults: Mapping[str, object] = field(default_factory=dict)


#: ``PROVERS -> (ProverClass, ConfigClass, defaults)``. Config-driven so callers never
#: hand-wire classes.
_REGISTRY: dict[PROVERS, _Entry] = {
    PROVERS.ARISTOTLE: _Entry(AristotleProver, AristotleProverConfig),
    PROVERS.CLAUDE: _Entry(AgentProver, AgentProverConfig),
    # codex authenticates via ChatGPT/OpenAI (``codex login``), so it must run an
    # OpenAI model -- not the AgentProverConfig default (``claude-opus-4-8``), which
    # codex can't serve without an Anthropic ``model_providers`` entry.
    PROVERS.CODEX: _Entry(
        AgentProver, AgentProverConfig, {"harness": "codex", "model": "gpt-5.5"}
    ),
    PROVERS.OPENCODE: _Entry(AgentProver, AgentProverConfig, {"harness": "opencode"}),
    # ax-prover-base runs as an AgentProver on the ``axprover`` harness: its own
    # LangGraph proposer->builder->reviewer loop edits the .lean in place, while
    # AgentProver staging/diff/auth and the shared Verifier do the final
    # compile/sorry/axiom check (we don't trust ax-prover's own reviewer).
    PROVERS.AXPROVER: _Entry(
        AgentProver,
        AgentProverConfig,
        {"harness": "axprover", "model": "claude-opus-4-8", "effort": "high"},
    ),
    PROVERS.NUMINA: _Entry(NuminaProver, NuminaProverConfig),
    # ``vibe`` runs as an AgentProver on the ``vibe`` harness driving Mistral Vibe's
    # Lean agent scaffold (the builtin ``lean`` agent *is* Leanstral; api-hosted, no
    # GPU). The real model ``labs-leanstral-2603`` is Labs-gated (403 until a Mistral
    # org admin enables Labs), so this defaults to Magistral -- a non-Labs *reasoning*
    # model any La Plateforme key can reach. The model is a knob, like the agent specs:
    # ``overrides={"model": "devstral-medium-latest"}`` swaps it. Vibe has no
    # ``--model`` flag, so the harness templates the model into the vendored
    # ``lean-standin`` profile at launch. Rename/repoint to the Labs model once enabled.
    PROVERS.VIBE: _Entry(
        AgentProver,
        AgentProverConfig,
        {
            "harness": "vibe",
            "agent": "lean-standin",
            "model": "magistral-medium-latest",
        },
    ),
}


def available_provers() -> list[PROVERS]:
    """The provers :func:`get_prover` accepts."""
    return list(PROVERS)


def get_prover(
    name: PROVERS | str,
    *,
    image: str = DEFAULT_IMAGE,
    toolchain: str = DEFAULT_TOOLCHAIN,
    verification_backend: ComputeBackend,
    agent_backend: ComputeBackend | None = None,
    overrides: Mapping[str, object] | None = None,
) -> AutomatedProver:
    """Construct the prover ``name`` with the shared image + verify backend.

    ``name`` is a :class:`PROVERS` member (or its string value). The config is built
    from ``image``/``toolchain`` + the name's baked-in defaults + caller ``overrides``
    (per-prover knobs: model, effort, max_rounds, ...). Agentic provers also receive
    ``agent_backend`` for generation (defaults to the verify backend), keeping the
    agent-vs-verify backend split available.

    Parameters
    ----------
    name : PROVERS or str
        The prover to build -- a :class:`PROVERS` member or its registry-key string
        (e.g. ``PROVERS.CLAUDE`` or ``"agent"``). Raises :class:`ValueError` for an
        unknown name.
    image : str, optional
        The sandbox image the prover compiles/verifies against. Defaults to
        :data:`~open_atp.images.DEFAULT_IMAGE`.
    toolchain : str, optional
        The Lean toolchain the prover supports. Defaults to
        :data:`~open_atp.images.DEFAULT_TOOLCHAIN`.
    verification_backend : ComputeBackend
        The backend that runs the shared :class:`~open_atp.verify.Verifier` (the cheap
        final compile/sorry/axiom check).
    agent_backend : ComputeBackend, optional
        The backend agentic provers run generation on. Defaults to
        ``verification_backend``, so generation and verification share a backend unless
        you split them (e.g. generate on Modal, verify on local Docker). Ignored by
        non-agentic provers (e.g. :class:`~open_atp.provers.aristotle.AristotleProver`).
    overrides : Mapping[str, object], optional
        Per-prover config knobs layered over the name's baked-in defaults (model,
        effort, max_rounds, ...). Keys must be fields of the prover's config class.

    Returns
    -------
    prover : AutomatedProver
        The constructed prover, ready to drive via
        :meth:`~open_atp.provers.base.AutomatedProver.prove`.

    Examples
    --------

    Construction is cheap and offline (the backend is wired in, not called), so the
    factory builds a ready-to-drive prover directly. ``overrides`` layer per-prover
    knobs over the name's baked-in defaults:

    >>> from open_atp import PROVERS, get_prover
    >>> from open_atp.backends.docker import DockerBackend, DockerConfig
    >>> from open_atp.images import DEFAULT_IMAGE
    >>> backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    >>> prover = get_prover(
    ...     PROVERS.CLAUDE,
    ...     verification_backend=backend,
    ...     overrides={"model": "claude-sonnet-4-6", "effort": "low"},
    ... )
    >>> prover.config.model
    'claude-sonnet-4-6'

    See each prover class for a family-specific construction example
    (:class:`~open_atp.provers.agent_prover.AgentProver`,
    :class:`~open_atp.provers.numina.NuminaProver`,
    :class:`~open_atp.provers.aristotle.AristotleProver`).

    Drive a constructed prover with
    :meth:`~open_atp.provers.base.AutomatedProver.prove` (this step runs the sandbox,
    so it needs a working Docker backend):

    .. code-block:: python

        from pathlib import Path
        from open_atp.lean import LeanProject, ProofTask

        task = ProofTask(project=LeanProject("path/to/lake/project"))
        result = prover.prove(task, output_dir=Path("runs/demo"))
        result.success      # compiles, sorry-free, no foreign axioms
        result.cost_usd     # estimated USD, when the prover reports it
        result.duration_s   # wall-clock seconds

    See :doc:`/user_guide/run_provers` for more example usage.
    """
    try:
        prover = name if isinstance(name, PROVERS) else PROVERS(name)
    except ValueError:
        raise ValueError(
            f"Unknown prover {name!r}; choose from {[p.value for p in PROVERS]}."
        ) from None
    entry = _REGISTRY[prover]

    kwargs: dict[str, object] = {"image": image, "supported_toolchain": toolchain}
    kwargs.update(entry.defaults)
    if overrides:
        kwargs.update(overrides)
    config = entry.config_cls(**kwargs)  # type: ignore[arg-type]

    # Agentic provers take (config, verify_backend, agent_backend); Aristotle does
    # its generation over the network and takes only the verify backend.
    if isinstance(config, AgentProverConfig):
        return entry.prover_cls(config, verification_backend, agent_backend)  # type: ignore[call-arg]
    return entry.prover_cls(config, verification_backend)
