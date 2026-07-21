"""The sandbox image and its baked-in Lean pins.

An :class:`Image` describes the container the sandbox runs: its tag plus the Lean
toolchain and Mathlib revision baked into it. It is the contract the verifier
enforces -- an uploaded project must pin the same toolchain (and Mathlib revision)
as the image it is about to run in. :data:`DEFAULT_IMAGE` is the image built from
``images/Dockerfile``.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Image:
    """A sandbox image: its container tag and the Lean pins baked into it.

    Carried by a :class:`~open_atp.backends.base.ComputeBackend` and used by the
    :class:`~open_atp.verify.Verifier` as the compatibility contract: a project must
    match :attr:`lean_toolchain` (and, when it locks one, :attr:`mathlib_rev`) or it
    is rejected before any compute is spent.

    Parameters
    ----------
    name : str, default "open-atp:latest"
        Container image tag carrying Lean + Mathlib that the sandbox runs. The
        default is the tag produced by ``docker build -t open-atp:latest images/``.
    lean_toolchain : str, default "leanprover/lean4:v4.28.0"
        Lean toolchain baked into the image (see ``images/lean/lean-toolchain``);
        projects must pin it or the verifier rejects them.
    mathlib_rev : str, default "v4.28.0"
        Mathlib revision whose olean cache is pre-baked, matching the declared pin
        in ``images/lean/lakefile.toml``.
    """

    name: str = "open-atp:latest"
    lean_toolchain: str = "leanprover/lean4:v4.28.0"
    mathlib_rev: str = "v4.28.0"


#: The image built from ``images/Dockerfile`` -- all default pins.
DEFAULT_IMAGE = Image()

#: The lake-project skeleton (``lakefile.toml`` + ``lean-toolchain``) matching
#: :data:`DEFAULT_IMAGE`. Used to stage bare ``.lean`` uploads into a full project for
#: the pinned toolchain/deps only. Lives at the repo root in a source checkout.
SKELETON_DIR = Path(__file__).resolve().parents[3] / "images" / "lean"

__all__ = [
    "Image",
    "DEFAULT_IMAGE",
    "SKELETON_DIR",
]
