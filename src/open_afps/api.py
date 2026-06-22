"""The common platform API: one project + chosen provers -> aggregated results.

Phases 1-4 built the engines (a uniform :class:`~open_afps.core.prover.AutomatedProver`
per method). This module is the dispatch/orchestration layer the project set out to
build: accept *"a lake project (or bare ``.lean`` files) + a list of provers"*, fan it
out across those provers concurrently, and return per-prover
:class:`~open_afps.core.result.ProofResult`\\s with verification + cost, compared.

The public surface is:

* :func:`build_prover` -- name -> constructed prover (the registry/factory).
* :class:`Platform` -- holds the shared image/toolchain + compute backends and runs
  :meth:`Platform.solve`.
* :class:`SolveResult` -- the aggregate, with :meth:`~SolveResult.verified`,
  :meth:`~SolveResult.best`, ``total_cost_usd`` and ``to_dict``.

Backends: the verifier (cheap final check) and the agent (generation) backends are
kept separate -- the split already exists in ``AgentProver`` -- so a job can run
generation on Modal and the check on local Docker. Both ``backend="docker"`` and
``backend="modal"`` are wired (Modal needs a published image; see
``open-afps build-modal-image``).
"""

from __future__ import annotations

import math
import shutil
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from open_afps.backends.base import ComputeBackend
from open_afps.backends.docker import DockerBackend, DockerConfig
from open_afps.backends.modal import ModalBackend, ModalConfig
from open_afps.core.prover import AutomatedProver, AutomatedProverConfig
from open_afps.core.result import ProofResult
from open_afps.core.task import LeanProject, ProofTask, ToolchainMismatch
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN, SKELETON_DIR
from open_afps.provers.agent import AgentProver, AgentProverConfig
from open_afps.provers.aristotle import AristotleProver, AristotleProverConfig
from open_afps.provers.numina import NuminaProver, NuminaProverConfig

# --- prover registry / factory ---------------------------------------------


@dataclass(frozen=True)
class _Entry:
    """A registry row: which classes back a prover name, plus baked-in config."""

    prover_cls: type[AutomatedProver]
    config_cls: type[AutomatedProverConfig]
    # Config overrides implied by the name itself (e.g. ``agent:codex`` -> codex).
    defaults: Mapping[str, object] = field(default_factory=dict)


#: ``name -> (ProverClass, ConfigClass, defaults)``. Names are config-driven so jobs
#: never hand-wire classes. ``agent:<harness>`` selects an :class:`AgentProver`
#: harness; the bare ``agent`` uses the config default (``claude_code``).
REGISTRY: dict[str, _Entry] = {
    "aristotle": _Entry(AristotleProver, AristotleProverConfig),
    "agent": _Entry(AgentProver, AgentProverConfig),
    "agent:codex": _Entry(AgentProver, AgentProverConfig, {"harness": "codex"}),
    "agent:opencode": _Entry(AgentProver, AgentProverConfig, {"harness": "opencode"}),
    "numina": _Entry(NuminaProver, NuminaProverConfig),
}


def available_provers() -> list[str]:
    """The prover names :func:`build_prover` accepts."""
    return list(REGISTRY)


def build_prover(
    spec: str,
    *,
    image: str = DEFAULT_IMAGE,
    toolchain: str = DEFAULT_TOOLCHAIN,
    verification_backend: ComputeBackend,
    agent_backend: ComputeBackend | None = None,
    overrides: Mapping[str, object] | None = None,
) -> AutomatedProver:
    """Construct the prover named ``spec`` with the shared image + verify backend.

    The config is built from ``image``/``toolchain`` + the name's baked-in defaults +
    caller ``overrides`` (per-prover knobs: model, effort, max_rounds, ...). Agentic
    provers also receive ``agent_backend`` for generation (defaults to the verify
    backend), keeping the agent-vs-verify backend split available.
    """
    try:
        entry = REGISTRY[spec]
    except KeyError:
        raise ValueError(
            f"Unknown prover {spec!r}; choose from {available_provers()}."
        ) from None

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


def _make_backend(kind: str, image: str) -> ComputeBackend:
    """Construct a compute backend by name. Designed for the Modal knob to land into."""
    if kind == "docker":
        return DockerBackend(DockerConfig(image=image))
    if kind == "modal":
        return ModalBackend(ModalConfig(image=image))
    raise ValueError(f"Unknown backend {kind!r}; choose 'docker' or 'modal'.")


# --- aggregate result ------------------------------------------------------


@dataclass
class SolveResult:
    """Per-prover results for one job, plus selectors over them."""

    run_id: str
    results: list[ProofResult]
    run_dir: Path | None = None

    def verified(self) -> list[ProofResult]:
        """The results that passed the shared verifier."""
        return [r for r in self.results if r.success]

    def best(self) -> ProofResult | None:
        """Verified, then cheapest ``cost_usd``, then fastest ``duration_s``.

        Unknown cost/duration (e.g. Aristotle exposes no per-run cost) sort last so a
        priced winner is preferred over an unpriced one. Returns ``None`` if nothing
        verified.
        """

        def rank(r: ProofResult) -> tuple[float, float]:
            cost = r.cost_usd if r.cost_usd is not None else math.inf
            dur = r.duration_s if r.duration_s is not None else math.inf
            return (cost, dur)

        winners = self.verified()
        return min(winners, key=rank) if winners else None

    @property
    def total_cost_usd(self) -> float:
        """Sum of known per-prover costs (entries without a cost contribute 0)."""
        return sum(r.cost_usd for r in self.results if r.cost_usd is not None)

    def to_dict(self, *, log_limit: int = 4000) -> dict[str, object]:
        best = self.best()
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir) if self.run_dir else None,
            "total_cost_usd": self.total_cost_usd,
            "best": best.prover if best else None,
            "verified": [r.prover for r in self.verified()],
            "results": [r.to_dict(log_limit=log_limit) for r in self.results],
        }


# --- the platform ----------------------------------------------------------


class Platform:
    """Turns *"a project + a list of provers"* into compared per-prover results.

    Holds the shared sandbox image/toolchain and the (verify, agent) compute
    backends. Construct once, call :meth:`solve` per job.
    """

    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        toolchain: str = DEFAULT_TOOLCHAIN,
        backend: str = "docker",
        agent_backend: str | None = None,
        runs_dir: Path | str = "runs",
        verification_backend: ComputeBackend | None = None,
        agent_compute_backend: ComputeBackend | None = None,
    ) -> None:
        self.image = image
        self.toolchain = toolchain
        self.runs_dir = Path(runs_dir)
        # Backends can be injected directly (tests, custom wiring) or named. The agent
        # backend defaults to the verify backend (single-backend jobs).
        self.verification_backend = verification_backend or _make_backend(
            backend, image
        )
        if agent_compute_backend is not None:
            self.agent_backend: ComputeBackend = agent_compute_backend
        elif agent_backend is not None:
            self.agent_backend = _make_backend(agent_backend, image)
        else:
            self.agent_backend = self.verification_backend

    def build(
        self, spec: str, overrides: Mapping[str, object] | None = None
    ) -> AutomatedProver:
        """Construct a registered prover with this platform's image + backends."""
        return build_prover(
            spec,
            image=self.image,
            toolchain=self.toolchain,
            verification_backend=self.verification_backend,
            agent_backend=self.agent_backend,
            overrides=overrides,
        )

    def solve(
        self,
        task: ProofTask,
        provers: Sequence[str | AutomatedProver],
        *,
        max_workers: int | None = None,
        overrides: Mapping[str, Mapping[str, object]] | None = None,
        run_id: str | None = None,
    ) -> SolveResult:
        """Run ``provers`` over ``task`` concurrently and aggregate the results.

        Each prover gets an isolated workdir (``runs/<run_id>/<label>/``) so parallel
        runs never share a ``.lake`` symlink or edit the same files. A prover that
        raises is captured into a failed :class:`ProofResult` (its ``error`` set) and
        the others still return.

        ``provers`` entries may be registry names (built via :meth:`build`, with
        per-name ``overrides``) or already-constructed provers (used as-is -- the unit
        seam for stubbing). ``run_id`` is generated if not supplied.
        """
        # Reject toolchain mismatch up front -- a clean API error before fanning out,
        # rather than each prover failing the same way deep in its run.
        if task.project.toolchain != self.toolchain:
            raise ToolchainMismatch(
                f"Project pins {task.project.toolchain!r} but the platform image "
                f"supports {self.toolchain!r}. Re-submit against a matching image."
            )

        run_id = run_id or uuid.uuid4().hex[:12]
        run_dir = self.runs_dir / run_id
        overrides = overrides or {}

        # Resolve each entry to (label, prover, workdir), keeping labels unique.
        jobs: list[tuple[str, AutomatedProver, Path]] = []
        seen: dict[str, int] = {}
        for entry in provers:
            if isinstance(entry, AutomatedProver):
                prover, label = entry, entry.name
            else:
                prover = self.build(entry, overrides.get(entry))
                label = entry
            label = label.replace(":", "_").replace("/", "_")
            if label in seen:
                seen[label] += 1
                label = f"{label}_{seen[label]}"
            else:
                seen[label] = 0
            workdir = run_dir / label
            workdir.mkdir(parents=True, exist_ok=True)
            jobs.append((label, prover, workdir))

        results: list[ProofResult] = []
        if jobs:
            workers = max_workers or len(jobs)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(
                    pool.map(
                        lambda j: self._run_one(j[0], j[1], task, j[2]),
                        jobs,
                    )
                )

        return SolveResult(run_id=run_id, results=results, run_dir=run_dir)

    @staticmethod
    def _run_one(
        label: str, prover: AutomatedProver, task: ProofTask, workdir: Path
    ) -> ProofResult:
        """Run one prover, capturing any failure so it can't kill the others."""
        try:
            result = prover.run(task, workdir)
            # Label by the spec used (``agent_codex``), not the class name (``agent``).
            result.prover = label
            return result
        except Exception as exc:  # noqa: BLE001 -- isolate per-prover failures
            return ProofResult(
                prover=label,
                verification=None,
                artifacts_dir=workdir,
                error=f"{type(exc).__name__}: {exc}",
            )


# --- input handling: the "upload" contract ---------------------------------


def project_from_dir(path: Path | str) -> LeanProject:
    """A full lake project on disk -> :class:`LeanProject` (validates the layout)."""
    return LeanProject(Path(path))


def stage_files(
    files: Sequence[Path | str],
    dest: Path | str,
    *,
    skeleton: Path = SKELETON_DIR,
) -> LeanProject:
    """Stage bare ``.lean`` files into the default Mathlib skeleton -> a project.

    Convenience for the *"upload one or more ``.lean`` files"* contract: copies the
    skeleton's ``lakefile.toml`` + ``lean-toolchain`` into ``dest`` and drops the
    files at its root. Limitation: this only works for the pinned toolchain/deps the
    skeleton (and the baked image) provide -- a file needing a different Mathlib
    revision or extra deps must arrive as a full lake project instead.
    """
    if not (skeleton / "lean-toolchain").is_file():
        raise FileNotFoundError(
            f"No skeleton project at {skeleton} (only available in a source "
            "checkout); submit a full lake project directory instead."
        )
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    skeleton_files = (
        "lakefile.toml",
        "lakefile.lean",
        "lean-toolchain",
        "lake-manifest.json",
    )
    for name in skeleton_files:
        src = skeleton / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    for f in files:
        f = Path(f)
        if f.suffix != ".lean":
            raise ValueError(f"Expected a .lean file, got {f}")
        shutil.copy2(f, dest / f.name)
    return LeanProject(dest)
