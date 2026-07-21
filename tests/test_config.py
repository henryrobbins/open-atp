"""Tests for the standard prover catalog (:mod:`open_atp.config`).

All offline: construction wires backends/harnesses/provers together but contacts no
daemon.
"""

from __future__ import annotations

import pytest

from open_atp import standard_prover, standard_provers
from open_atp.backends.docker import DockerBackend
from open_atp.config import STANDARD_PROVERS, _build_backend, _build_harness
from open_atp.harness import ClaudeCodeHarness, CodexHarness, VibeHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers.agent_prover import AgentProver
from open_atp.provers.aristotle import AristotleProver
from open_atp.provers.numina import NuminaProver

# --- the catalog -----------------------------------------------------------


def test_standard_provers_lists_the_catalog_keys() -> None:
    assert standard_provers() == list(STANDARD_PROVERS)


@pytest.mark.parametrize("name", sorted(STANDARD_PROVERS))
def test_every_standard_prover_builds(name: str) -> None:
    prover = standard_prover(name, backend=DockerBackend())
    assert prover.name == name
    assert prover.verifier.backend.name == "docker"


def test_agentic_entries_are_one_prover_on_different_harnesses() -> None:
    backend = DockerBackend()
    claude = standard_prover("claude", backend=backend)
    codex = standard_prover("codex", backend=backend)

    assert isinstance(claude, AgentProver) and isinstance(codex, AgentProver)
    assert isinstance(claude.harness, ClaudeCodeHarness)
    assert isinstance(codex.harness, CodexHarness)
    assert codex.harness.model == "gpt-5.5"  # the harness's baked-in default


def test_standalone_provers_are_their_own_classes() -> None:
    backend = DockerBackend()
    assert isinstance(standard_prover("numina", backend=backend), NuminaProver)
    assert isinstance(standard_prover("aristotle", backend=backend), AristotleProver)


def test_unknown_standard_prover_raises() -> None:
    with pytest.raises(ValueError, match="unknown prover 'nope'"):
        standard_prover("nope", backend=DockerBackend())


# --- the dict factory ------------------------------------------------------
#
# The spec factories are private: in production they are reached only through the
# CLI's ``--config`` file, and test_cli.py covers that path end to end. They are
# exercised directly here because the dispatch/validation in ``_split`` is the
# intricate part -- the loud-on-typo guard exists to turn a silently-ignored YAML
# key into an error, and driving every branch of it through a config file would
# obscure what is being asserted.


def test_backend_spec_dispatches_and_carries_knobs() -> None:
    docker = _build_backend({"type": "docker"})
    assert docker.name == "docker"
    assert docker.image == DEFAULT_IMAGE

    modal = _build_backend({"type": "modal", "cpu": 4, "memory_mib": 8192})
    assert modal.name == "modal"
    assert (modal.cpu, modal.memory_mib) == (4, 8192)  # type: ignore[attr-defined]


def test_backend_spec_coerces_a_nested_image_mapping() -> None:
    backend = _build_backend(
        {"type": "docker", "image": {"lean_toolchain": "leanprover/lean4:v4.31.0"}}
    )
    assert backend.image.lean_toolchain == "leanprover/lean4:v4.31.0"


def test_harness_spec_accepts_a_mapping_or_a_bare_type_name() -> None:
    harness = _build_harness({"type": "codex", "effort": "low"})
    assert isinstance(harness, CodexHarness)
    assert harness.effort == "low"

    assert isinstance(_build_harness("vibe"), VibeHarness)


def test_unknown_type_raises() -> None:
    with pytest.raises(ValueError, match="unknown compute type 'dockerr'"):
        _build_backend({"type": "dockerr"})

    with pytest.raises(ValueError, match="unknown harness type None"):
        _build_harness({"model": "claude-opus-4-8"})


def test_misspelled_option_raises_rather_than_being_ignored() -> None:
    with pytest.raises(ValueError, match="unknown 'codex' harness option"):
        _build_harness({"type": "codex", "modl": "x"})
