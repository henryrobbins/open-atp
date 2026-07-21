"""Filesystem anchors for the harness package's vendored assets.

Centralised here so the asset/vendor locations are computed from the package
*directory* rather than counting ``Path(__file__).parents[N]`` hops in each
module -- the hop counts shift whenever a file moves, the package anchor does not.
"""

from __future__ import annotations

from pathlib import Path

import open_atp

#: Directory of this ``harness`` package; the vendored agent assets live under it.
_HARNESS_DIR = Path(__file__).parent
_ASSETS = _HARNESS_DIR / "assets"
_SCRIPTS = _ASSETS / "scripts"
_MCP_JSON = _ASSETS / "configs" / "mcp.json"

#: Root of the ``open_atp`` package, used to locate the vendored bundles.
_PACKAGE_DIR = Path(open_atp.__file__).parent


def _vendor_dir(name: str) -> Path:
    """Locate a vendored bundle ``vendor/<name>`` in both wheel and source layouts."""
    candidates = [
        # Built wheel: force-included at open_atp/vendor/<name> (see pyproject).
        _PACKAGE_DIR / "vendor" / name,
        # Source checkout / editable install: vendor/ at the repo root.
        _PACKAGE_DIR.parent.parent / "vendor" / name,
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _vendor_numina_dir() -> Path:
    """Locate the vendored Numina bundle."""
    return _vendor_dir("numina")


def _vendor_leanprover_skills_dir() -> Path:
    """Locate the vendored ``leanprover/skills`` repo (host-agnostic Lean skills)."""
    return _vendor_dir("leanprover-skills")


def _vendor_lean4_skills_dir() -> Path:
    """Locate the vendored ``lean4-skills`` repo (the Claude Code ``lean4`` plugin)."""
    return _vendor_dir("lean4-skills")
