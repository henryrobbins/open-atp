"""Concrete provers and the type map used to route config to them."""

from open_atp.provers.agent_prover import AgentProver
from open_atp.provers.aristotle import AristotleProver
from open_atp.provers.base import (
    AutomatedProver,
    GenerationTimeout,
    ProofResult,
    ProofStatus,
    ProverError,
)
from open_atp.provers.numina import NuminaProver

# Prover *type* name -> the AutomatedProver subclass, keyed by the ``type`` a
# ``prover`` config spec names. The friendlier STANDARD_PROVERS catalog is layered on
# top of this: several of its entries are one class on different harnesses.
_PROVERS: dict[str, type[AutomatedProver]] = {
    "agent": AgentProver,
    "numina": NuminaProver,
    "aristotle": AristotleProver,
}

__all__ = [
    "AutomatedProver",
    "AgentProver",
    "AristotleProver",
    "GenerationTimeout",
    "NuminaProver",
    "ProofResult",
    "ProofStatus",
    "ProverError",
]
