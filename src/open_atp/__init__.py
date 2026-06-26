"""Open Automated Formal Proof Synthesis (open-atp).

A platform that takes Lean projects containing ``sorry`` and returns verified
completed proofs across multiple proof-synthesis backends (agentic coding agents,
the Numina Lean agent, and Harmonic's Aristotle API).

The architecture rests on two reusable primitives:

* :class:`~open_atp.backends.base.ComputeBackend` -- run a command over a working
  directory in a Lean+Mathlib sandbox (Docker or Modal).
* :class:`~open_atp.verify.Verifier` -- compile a project in a backend and
  report whether it is sorry-free and axiom-clean.

Everything else is a candidate generator
(:class:`~open_atp.provers.base.AutomatedProver`) that produces completed files, which
are then funnelled through the shared verifier.
"""

from open_atp.benchmark import (
    DATASET,
    BenchmarkResult,
    BenchmarkRun,
    download_dataset,
    run_benchmark,
    tasks_from_dir,
)
from open_atp.config import (
    build_backend,
    build_harness,
    build_prover,
    standard_prover,
    standard_provers,
)
from open_atp.images import DEFAULT_IMAGE, Image
from open_atp.lean import LeanProject, ProofTask, create_project
from open_atp.provers.base import AutomatedProver, ProofResult
from open_atp.verify import VerificationReport

__all__ = [
    "AutomatedProver",
    "BenchmarkResult",
    "BenchmarkRun",
    "DATASET",
    "DEFAULT_IMAGE",
    "Image",
    "ProofResult",
    "VerificationReport",
    "LeanProject",
    "ProofTask",
    "standard_prover",
    "standard_provers",
    "build_prover",
    "build_backend",
    "build_harness",
    "create_project",
    "download_dataset",
    "run_benchmark",
    "tasks_from_dir",
]
