"""Open Automated Formal Proof Synthesis (open-afps).

A platform that takes Lean projects containing ``sorry`` and returns verified
completed proofs across multiple proof-synthesis backends (agentic coding agents,
the Numina Lean agent, and Harmonic's Aristotle API).

The architecture rests on two reusable primitives:

* :class:`~open_afps.backends.base.ComputeBackend` -- run a command over a working
  directory in a Lean+Mathlib sandbox (Docker or Modal).
* :class:`~open_afps.core.verifier.Verifier` -- compile a project in a backend and
  report whether it is sorry-free and axiom-clean.

Everything else is a candidate generator
(:class:`~open_afps.core.prover.AutomatedProver`) that produces completed files, which
are then funnelled through the shared verifier.
"""

from open_afps.core.prover import AutomatedProver, AutomatedProverConfig
from open_afps.core.result import ProofResult, VerificationReport
from open_afps.core.task import LeanProject, ProofTask

__all__ = [
    "AutomatedProver",
    "AutomatedProverConfig",
    "ProofResult",
    "VerificationReport",
    "LeanProject",
    "ProofTask",
]
