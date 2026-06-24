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

from open_atp.lean import LeanProject, ProofTask, stage_files
from open_atp.provers import PROVERS, available_provers, get_prover
from open_atp.provers.base import AutomatedProver, AutomatedProverConfig
from open_atp.verify import ProofResult, VerificationReport

__all__ = [
    "AutomatedProver",
    "AutomatedProverConfig",
    "ProofResult",
    "VerificationReport",
    "LeanProject",
    "ProofTask",
    "PROVERS",
    "available_provers",
    "get_prover",
    "stage_files",
]
