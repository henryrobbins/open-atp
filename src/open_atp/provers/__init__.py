"""Concrete provers and the type map used to route config to them."""

from open_atp.provers.agent_prover import AgentProver
from open_atp.provers.aristotle import AristotleProver
from open_atp.provers.base import AutomatedProver, ProofResult, ProofStatus
from open_atp.provers.numina import NuminaProver

#: Prover *type* name -> the :class:`AutomatedProver` subclass. Package-internal: the
#: factory in :mod:`open_atp.config` dispatches a ``prover`` spec's ``type`` through
#: this, and the high-level standard catalog
#: (:data:`~open_atp.config.STANDARD_PROVERS`) is layered on top of it.
_PROVERS: dict[str, type[AutomatedProver]] = {
    "agent": AgentProver,
    "numina": NuminaProver,
    "aristotle": AristotleProver,
}

__all__ = [
    "AutomatedProver",
    "AgentProver",
    "AristotleProver",
    "NuminaProver",
    "ProofResult",
    "ProofStatus",
]
