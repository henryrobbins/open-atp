"""Tests for the dict-config factory (:mod:`open_atp.config`).

All offline: construction wires backends/harnesses/provers together but contacts no
daemon, so these exercise the dispatch + kwargs plumbing and the loud-on-typo guard.
"""

from __future__ import annotations

import pytest

from open_atp.config import build_backend, build_harness, build_prover
from open_atp.harness import ClaudeCodeHarness, CodexHarness, VibeHarness
from open_atp.images import DEFAULT_IMAGE
from open_atp.provers.agent_prover import AgentProver
from open_atp.provers.aristotle import AristotleProver
from open_atp.provers.numina import NuminaProver

# --- build_backend ---------------------------------------------------------


def test_build_backend_docker_defaults() -> None:
    backend = build_backend({"type": "docker"})
    assert backend.name == "docker"
    assert backend.image == DEFAULT_IMAGE


def test_build_backend_modal_with_knobs() -> None:
    backend = build_backend({"type": "modal", "cpu": 4, "memory_mib": 8192})
    assert backend.name == "modal"
    assert backend.cpu == 4
    assert backend.memory_mib == 8192


def test_build_backend_coerces_nested_image_mapping() -> None:
    backend = build_backend(
        {"type": "docker", "image": {"lean_toolchain": "leanprover/lean4:v4.31.0"}}
    )
    assert backend.image.lean_toolchain == "leanprover/lean4:v4.31.0"


# --- build_harness ---------------------------------------------------------


def test_build_harness_from_mapping() -> None:
    harness = build_harness({"type": "codex", "effort": "low"})
    assert isinstance(harness, CodexHarness)
    assert harness.model == "gpt-5.5"  # codex's baked-in default
    assert harness.effort == "low"


def test_build_harness_string_shorthand() -> None:
    harness = build_harness("vibe")
    assert isinstance(harness, VibeHarness)
    assert harness.agent == "lean"


# --- build_prover ----------------------------------------------------------


def test_build_prover_agent_with_nested_harness() -> None:
    prover = build_prover(
        {
            "compute": {"type": "docker", "image": {"mathlib_rev": "v4.31.0"}},
            "prover": {
                "type": "agent",
                "harness": {"type": "claude_code", "model": "claude-opus-4-8"},
                "skills": ["lean-proof"],
            },
        }
    )
    assert isinstance(prover, AgentProver)
    assert isinstance(prover.harness, ClaudeCodeHarness)
    assert prover.harness.model == "claude-opus-4-8"
    assert prover.skills == ["lean-proof"]
    # The backend is wired into the prover's verifier.
    assert prover.verifier.backend.name == "docker"
    assert prover.verifier.backend.image.mathlib_rev == "v4.31.0"


def test_build_prover_harness_string_shorthand() -> None:
    prover = build_prover(
        {"compute": {"type": "docker"}, "prover": {"type": "agent", "harness": "codex"}}
    )
    assert isinstance(prover.harness, CodexHarness)


def test_build_prover_numina_on_modal() -> None:
    prover = build_prover(
        {
            "compute": {"type": "modal", "cpu": 2},
            "prover": {"type": "numina", "max_rounds": 5},
        }
    )
    assert isinstance(prover, NuminaProver)
    assert prover.max_rounds == 5
    assert prover.verifier.backend.cpu == 2


def test_build_prover_aristotle() -> None:
    prover = build_prover(
        {
            "compute": {"type": "docker"},
            "prover": {"type": "aristotle", "poll_interval_s": 3},
        }
    )
    assert isinstance(prover, AristotleProver)
    assert prover.poll_interval_s == 3


# --- loud on bad input -----------------------------------------------------


def test_unknown_compute_type_raises() -> None:
    with pytest.raises(ValueError, match="unknown compute type 'dockerr'"):
        build_backend({"type": "dockerr"})


def test_unknown_prover_option_raises() -> None:
    with pytest.raises(ValueError, match="unknown 'agent' prover option"):
        build_prover(
            {"compute": {"type": "docker"}, "prover": {"type": "agent", "effrt": "x"}}
        )


def test_unknown_harness_option_raises() -> None:
    with pytest.raises(ValueError, match="unknown 'codex' harness option"):
        build_harness({"type": "codex", "modl": "x"})


def test_missing_type_raises() -> None:
    with pytest.raises(ValueError, match="unknown harness type None"):
        build_harness({"model": "claude-opus-4-8"})
