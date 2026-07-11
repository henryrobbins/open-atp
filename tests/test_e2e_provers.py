"""End-to-end prover suite: every prover, on every compute backend, against the
trivial Mathematics-in-Lean fixture (one sorry'd theorem).

This is the *single* live test the project needs: one parametrized function over
``backend (docker, modal) x prover``, routed through :func:`standard_prover` +
:meth:`~open_atp.provers.base.AutomatedProver.prove` (so it exercises the backend
factory, the catalog, and the shared verifier exactly as a caller would). It replaces
the per-prover ``test_live_*_solves_trivial`` copies that used to live in each prover's
test module.

Each case is gated two ways and skips (never fails) when its prerequisites are
absent:

* the **compute** marker (``docker`` / ``modal``) -- opt out with ``-m 'not modal'``;
  Modal also needs credentials + a published image (``open-atp build-modal-image``).
* the **API** marker (``aristotle_api`` / ``agent_api`` / ``numina_api``) -- excluded
  by default (billable), opt in with e.g. ``-m agent_api``. That selects the prover's
  rows on *both* backends; add ``and docker`` to pin one.

``test_catalog_is_fully_covered`` is a fast guard (no markers, no creds) that fails
if a new prover lands in the standard catalog without an e2e row here.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from open_atp.backends.base import ComputeBackend
from open_atp.backends.docker import DockerBackend
from open_atp.backends.modal import ModalBackend
from open_atp.config import standard_prover, standard_provers
from open_atp.images import DEFAULT_IMAGE, Image
from open_atp.lean import LeanProject, ProofTask

FIXTURE = Path(__file__).parent / "fixtures" / "mil_trivial"


def make_backend(kind: str, image: Image = DEFAULT_IMAGE) -> ComputeBackend:
    """Construct a compute backend by name (``docker`` | ``modal``)."""
    if kind == "docker":
        return DockerBackend(image=image)
    if kind == "modal":
        return ModalBackend(image=image)
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


def _need_grok() -> str | None:
    """Grok runs via opencode's xai provider (``opencode auth login`` -> xAI)."""
    store = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    try:
        ok = "xai" in json.loads(store.read_text())
    except (OSError, ValueError):
        ok = False
    return None if ok else "no xai login in opencode auth.json (opencode auth login)"


def _need_modal() -> str | None:
    """Modal: env tokens or a ``~/.modal.toml`` profile (``modal token set``)."""
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return None
    if (Path.home() / ".modal.toml").is_file():
        return None
    return "Modal not configured (no MODAL_TOKEN_* env and no ~/.modal.toml)"


# --- the parametrization: backends x provers ----------------------------------
#
# ``leanstral`` covers the vibe harness on its ``labs-leanstral-1-5`` default (the
# lab model, reachable now that Lab Model access is enabled). Keep this set in sync
# with the catalog -- ``test_catalog_is_fully_covered`` enforces it.

BACKENDS = [
    pytest.param("docker", lambda: None, marks=pytest.mark.docker, id="docker"),
    pytest.param("modal", _need_modal, marks=pytest.mark.modal, id="modal"),
]

PROVER_SPECS = [
    pytest.param(
        "aristotle",
        _need_env("ARISTOTLE_API_KEY"),
        marks=pytest.mark.aristotle_api,
        id="aristotle",
    ),
    pytest.param(
        "claude",
        _need_env("CLAUDE_CODE_OAUTH_TOKEN"),
        marks=pytest.mark.agent_api,
        id="agent-claude",
    ),
    pytest.param(
        "codex",
        _need_codex,
        marks=pytest.mark.agent_api,
        id="agent-codex",
    ),
    pytest.param(
        "opencode",
        _need_env("ANTHROPIC_API_KEY"),
        marks=pytest.mark.agent_api,
        id="agent-opencode",
    ),
    pytest.param(
        "axproverbase",
        _need_env("ANTHROPIC_API_KEY"),
        marks=pytest.mark.agent_api,
        id="agent-axproverbase",
    ),
    pytest.param(
        "numina",
        _need_env("CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY"),
        marks=pytest.mark.numina_api,
        id="numina",
    ),
    pytest.param(
        "leanstral",
        _need_env("MISTRAL_API_KEY"),
        marks=pytest.mark.agent_api,
        id="agent-leanstral",
    ),
    pytest.param(
        "grok",
        _need_grok,
        marks=pytest.mark.agent_api,
        id="agent-grok",
    ),
]


@pytest.mark.parametrize("backend, backend_ready", BACKENDS)
@pytest.mark.parametrize("spec, creds_ready", PROVER_SPECS)
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
    prover = standard_prover(spec, backend=compute)
    proof = prover.prove(ProofTask(LeanProject(FIXTURE)), tmp_path / "run")

    assert proof.completed_files, f"{spec} returned no changed files"
    assert proof.success, proof.verification and proof.verification.compile_log


def test_catalog_is_fully_covered() -> None:
    """Every catalog prover has an e2e row (so new provers can't slip through)."""
    covered = {p.values[0] for p in PROVER_SPECS}
    catalog = set(standard_provers())
    assert covered >= catalog, (
        f"provers missing an e2e case: {sorted(catalog - covered)}"
    )
