"""Build provers (with their compute backend and harness) from plain config dicts.

The library's runtime objects -- :class:`~open_atp.provers.base.AutomatedProver`,
:class:`~open_atp.harness.Harness`, :class:`~open_atp.backends.base.ComputeBackend` --
take plain keyword arguments. This module is the thin factory that turns a nested
config dict (e.g. parsed from YAML; parsing is the caller's job, so there is no YAML
dependency here) into a constructed prover::

    from open_atp.config import build_prover

    prover = build_prover({
        "compute": {"type": "modal", "cpu": 2, "memory_mib": 4096},
        "prover": {
            "type": "agent",
            "harness": {"type": "claude_code", "model": "claude-opus-4-8"},
            "skills": ["lean-proof"],
        },
    })

Each level is dispatched on its ``type`` key; the remaining keys become constructor
kwargs. An unknown ``type`` or a key the target doesn't accept raises -- a typo'd
option fails loudly rather than being silently ignored.

For the common case of "give me a sensible default prover by name", the
:data:`STANDARD_PROVERS` catalog names each ready-to-run default
(``"claude"``, ``"numina"``, ...) as a canonical prover spec, and
:func:`standard_prover` builds one against a backend.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import TypeVar, cast

from open_atp.backends import _BACKENDS
from open_atp.backends.base import ComputeBackend
from open_atp.harness import _HARNESSES, Harness
from open_atp.provers import _PROVERS
from open_atp.provers.base import AutomatedProver

_T = TypeVar("_T")


def _split(
    registry: Mapping[str, type[_T]], spec: Mapping[str, object], kind: str
) -> tuple[type[_T], dict[str, object]]:
    """Resolve ``spec["type"]`` to a class and return ``(cls, kwargs)``.

    ``kwargs`` is ``spec`` minus ``type``; any key that is not a constructor parameter
    of ``cls`` raises :class:`ValueError` (a loud signal for a typo'd option).
    """
    rest = dict(spec)
    type_ = rest.pop("type", None)
    if not isinstance(type_, str) or type_ not in registry:
        raise ValueError(
            f"unknown {kind} type {type_!r}; choose from {sorted(registry)}"
        )
    cls = registry[type_]
    known = set(inspect.signature(cls).parameters) - {"self"}
    extra = set(rest) - known
    if extra:
        raise ValueError(
            f"unknown {type_!r} {kind} option(s) {sorted(extra)}; "
            f"valid: {sorted(known)}"
        )
    return cls, rest


def build_backend(spec: Mapping[str, object]) -> ComputeBackend:
    """Construct a :class:`~open_atp.backends.base.ComputeBackend` from a compute spec.

    ``spec`` is a mapping with a ``type`` (``"docker"`` / ``"modal"``) plus that
    backend's kwargs (e.g. ``{"type": "modal", "cpu": 2}``). A nested ``image`` mapping
    is coerced to an :class:`~open_atp.images.Image` by the backend itself.
    """
    cls, kwargs = _split(_BACKENDS, spec, "compute")
    return cls(**kwargs)  # type: ignore[arg-type]  # validated dict -> kwargs


def build_harness(spec: Mapping[str, object] | str) -> Harness:
    """Construct a :class:`~open_atp.harness.Harness` from a harness spec.

    ``spec`` is either a bare type name (``"claude_code"``) or a mapping with a ``type``
    plus the harness's kwargs (``{"type": "vibe", "max_turns": 8}``).
    """
    spec = {"type": spec} if isinstance(spec, str) else spec
    cls, kwargs = _split(_HARNESSES, spec, "harness")
    return cls(**kwargs)  # type: ignore[arg-type]  # validated dict -> kwargs


def _build_prover(
    spec: Mapping[str, object], backend: ComputeBackend, *, name: str | None = None
) -> AutomatedProver:
    """Construct a prover from a ``prover`` spec against an already-built ``backend``.

    Dispatches ``spec["type"]`` through the package-internal ``_PROVERS`` map, builds a
    nested ``harness`` spec along the way, and wires the backend in.

    ``name`` (the standard-catalog key) is injected only for provers that accept it
    (``AgentProver``), so the reported name matches the user-facing registry key
    rather than the harness name. Provers that don't accept it report their own.
    """
    cls, kwargs = _split(_PROVERS, spec, "prover")
    if "harness" in kwargs:
        kwargs["harness"] = build_harness(
            cast("Mapping[str, object] | str", kwargs["harness"])
        )
    if name is not None and "name" in inspect.signature(cls).parameters:
        kwargs.setdefault("name", name)
    return cls(backend=backend, **kwargs)  # type: ignore[arg-type]  # validated kwargs


def build_prover(config: Mapping[str, object]) -> AutomatedProver:
    """Construct a fully-wired prover from a ``{compute, prover}`` config dict.

    Builds the backend from ``config["compute"]``, then the prover from
    ``config["prover"]`` (dispatched on its ``type``), recursively building a nested
    ``harness`` spec along the way, and wires the backend in.
    """
    backend = build_backend(cast("Mapping[str, object]", config["compute"]))
    return _build_prover(cast("Mapping[str, object]", config["prover"]), backend)


#: The standard catalog: a friendly name -> the canonical ``prover`` spec for that
#: ready-to-run default, and the source of truth for which names the CLI accepts.
#: Most entries are the shared :class:`~open_atp.provers.agent_prover.AgentProver`
#: on a different harness -- several share the ``opencode`` harness, differing only
#: in provider and auth strategy -- and the rest are standalone provers. Build one
#: with :func:`standard_prover`.
STANDARD_PROVERS: dict[str, dict[str, object]] = {
    "claude": {"type": "agent", "harness": {"type": "claude_code"}},
    "codex": {"type": "agent", "harness": {"type": "codex"}},
    "deepseek": {
        "type": "agent",
        "harness": {
            "type": "opencode",
            "model": "deepseek-v4-pro",
            "provider": "deepseek",
            "auth": "api_key",
        },
    },
    "grok": {
        "type": "agent",
        "harness": {
            "type": "opencode",
            "model": "grok-4.5",
            "provider": "xai",
            "auth": "login",
        },
    },
    "leanstral": {"type": "agent", "harness": {"type": "vibe"}},
    "axproverbase": {"type": "agent", "harness": {"type": "axproverbase"}},
    "kimi": {"type": "agent", "harness": {"type": "kimi"}},
    "numina": {"type": "numina"},
    "aristotle": {"type": "aristotle"},
}


def standard_prover(name: str, *, backend: ComputeBackend) -> AutomatedProver:
    """Construct a standard default prover ``name`` against ``backend``.

    ``name`` is a :data:`STANDARD_PROVERS` key, as listed by
    :func:`standard_provers`. The prover is built with its class's baked-in defaults;
    to customize any knob (model, effort, skills, ...), use :func:`build_prover` with
    a full config dict instead.

    The sandbox image (and the toolchain + Mathlib pins projects are checked against)
    comes from ``backend``, not a parameter here.

    Examples
    --------

    >>> from open_atp.backends.docker import DockerBackend
    >>> prover = standard_prover("claude", backend=DockerBackend())
    >>> prover.harness.model
    'claude-opus-4-8'
    """
    if name not in STANDARD_PROVERS:
        raise ValueError(f"unknown prover {name!r}; choose from {standard_provers()}")
    return _build_prover(STANDARD_PROVERS[name], backend, name=name)


def standard_provers() -> list[str]:
    """The names :func:`standard_prover` accepts: the :data:`STANDARD_PROVERS` keys."""
    return list(STANDARD_PROVERS)
