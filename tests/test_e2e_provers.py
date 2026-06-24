"""End-to-end prover suite: every prover, on every compute backend, against the
trivial Mathematics-in-Lean fixture (one sorry'd theorem).

This is the *single* live test the project needs: one parametrized function over
``backend (docker, modal) x prover``, routed through :func:`get_prover` +
:meth:`~open_atp.provers.base.AutomatedProver.prove` (so it exercises the backend
factory, the registry, and the shared verifier exactly as a caller would). It replaces
the per-prover ``test_live_*_solves_trivial`` copies that used to live in each prover's
test module.

Each case is gated two ways and skips (never fails) when its prerequisites are
absent:

* the **compute** marker (``docker`` / ``modal``) -- opt out with ``-m 'not modal'``;
  Modal also needs credentials + a published image (``open-atp build-modal-image``).
* the **API** marker (``aristotle_api`` / ``agent_api`` / ``numina_api``) -- excluded
  by default (billable), opt in with e.g. ``-m agent_api``. That selects the prover's
  rows on *both* backends; add ``and docker`` to pin one.

``test_registry_is_fully_covered`` is a fast guard (no markers, no creds) that fails
if a new prover lands in the registry without an e2e row here.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from open_atp.backends.base import ComputeBackend
from open_atp.backends.docker import DockerBackend, DockerConfig
from open_atp.backends.modal import ModalBackend, ModalConfig
from open_atp.images import DEFAULT_IMAGE
from open_atp.lean import LeanProject, ProofTask
from open_atp.provers import available_provers, get_prover

FIXTURE = Path(__file__).parent / "fixtures" / "mil_trivial"


def make_backend(kind: str, image: str = DEFAULT_IMAGE) -> ComputeBackend:
    """Construct a compute backend by name (``docker`` | ``modal``)."""
    if kind == "docker":
        return DockerBackend(DockerConfig(image=image))
    if kind == "modal":
        return ModalBackend(ModalConfig(image=image))
    raise ValueError(f"Unknown backend {kind!r}; choose 'docker' or 'modal'.")


# --- credential gates: each returns a skip reason, or None when ready ---------

CredCheck = Callable[[], "str | None"]


def _need_env(*names: str) -> CredCheck:
    """All of ``names`` must be set in the env (read from ``.env`` by conftest)."""

    def check() -> str | None:
        missing = [n for n in names if not os.environ.get(n)]
        return f"missing env: {', '.join(missing)}" if missing else None

    return check


def _need_codex() -> str | None:
    """Codex authenticates via a ``~/.codex`` dir (``codex login``), not an env var."""
    return None if (Path.home() / ".codex").exists() else "no ~/.codex (codex login)"


def _need_modal() -> str | None:
    """Modal: env tokens or a ``~/.modal.toml`` profile (``modal token set``)."""
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return None
    if (Path.home() / ".modal.toml").is_file():
        return None
    return "Modal not configured (no MODAL_TOKEN_* env and no ~/.modal.toml)"


# --- the parametrization: backends x provers ----------------------------------
#
# ``vibe`` covers the vibe harness on its non-Labs Magistral default (the real
# ``labs-leanstral-2603`` model is Mistral-Labs-gated and not runnable today). Keep
# this set in sync with the registry -- ``test_registry_is_fully_covered`` enforces it.

BACKENDS = [
    pytest.param("docker", lambda: None, marks=pytest.mark.docker, id="docker"),
    pytest.param("modal", _need_modal, marks=pytest.mark.modal, id="modal"),
]

PROVERS = [
    pytest.param(
        "aristotle",
        _need_env("ARISTOTLE_API_KEY"),
        marks=pytest.mark.aristotle_api,
        id="aristotle",
    ),
    pytest.param(
        "agent",
        _need_env("CLAUDE_CODE_OAUTH_TOKEN"),
        marks=pytest.mark.agent_api,
        id="agent",
    ),
    pytest.param(
        "agent:codex",
        _need_codex,
        marks=pytest.mark.agent_api,
        id="agent-codex",
    ),
    pytest.param(
        "agent:opencode",
        _need_env("ANTHROPIC_API_KEY"),
        marks=pytest.mark.agent_api,
        id="agent-opencode",
    ),
    pytest.param(
        "agent:axprover",
        _need_env("ANTHROPIC_API_KEY"),
        marks=pytest.mark.agent_api,
        id="agent-axprover",
    ),
    pytest.param(
        "numina",
        _need_env("CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY"),
        marks=pytest.mark.numina_api,
        id="numina",
    ),
    pytest.param(
        "vibe",
        _need_env("MISTRAL_API_KEY"),
        marks=pytest.mark.agent_api,
        id="vibe",
    ),
]


@pytest.mark.parametrize("backend, backend_ready", BACKENDS)
@pytest.mark.parametrize("spec, creds_ready", PROVERS)
def test_prover_solves_trivial_theorem(
    backend: str,
    backend_ready: CredCheck,
    spec: str,
    creds_ready: CredCheck,
    tmp_path: Path,
) -> None:
    """Build ``spec`` on ``backend`` and confirm it solves + verifies the fixture."""
    for reason in (backend_ready(), creds_ready()):
        if reason:
            pytest.skip(reason)

    compute = make_backend(backend, DEFAULT_IMAGE)
    prover = get_prover(spec, verification_backend=compute)
    proof = prover.prove(ProofTask(LeanProject(FIXTURE)), tmp_path / "run")

    assert proof.completed_files, f"{spec} returned no changed files"
    assert proof.success, proof.verification and proof.verification.compile_log


def test_registry_is_fully_covered() -> None:
    """Every registered prover has an e2e row (so new provers can't slip through)."""
    covered = {p.values[0] for p in PROVERS}
    registry = {p.value for p in available_provers()}
    assert covered >= registry, (
        f"provers missing an e2e case: {sorted(registry - covered)}"
    )
