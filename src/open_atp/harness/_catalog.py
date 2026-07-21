"""Resolve named (or path-given) agent assets to their source directories.

Two kinds of asset are resolvable **by name** (from a vendored catalog) or **by full
path**:

* **skills** -- host-agnostic Agent Skills (``<name>/SKILL.md``) listed on
  ``AgentProver.skills`` and copied by the
  :class:`~open_atp.provers.agent_prover.AgentProver` into each
  harness's skill location (``.claude/skills``, ``.agents/skills``,
  ``VIBE_HOME/skills``) via :meth:`~open_atp.harness.base.Harness.stage_skills`. The
  default is ``lean-proof`` from the vendored ``leanprover/skills`` catalog.
* **plugins** -- Claude Code plugins (a dir with ``.claude-plugin/plugin.json``)
  listed on ``ClaudeCodeHarness.plugins`` and loaded **only** by the Claude
  harness via ``--plugin-dir`` (no other harness supports plugins). The default is
  the vendored ``lean4`` plugin.

Anything that isn't a simple named skill or plugin -- e.g. Numina's root-mounted
coordinator skill and its subagent-prompt tree -- is staged by that prover itself
(see :class:`~open_atp.provers.numina.NuminaProver`), not resolved here.
"""

from __future__ import annotations

from pathlib import Path

from open_atp.harness._paths import (
    _vendor_lean4_skills_dir,
    _vendor_leanprover_skills_dir,
)

#: Directories searched when a skill is named rather than given as a path: the
#: vendored ``leanprover/skills`` catalog (``lean-proof``, ``lean-setup``, ...).
_SKILL_CATALOGS: tuple[Path, ...] = (_vendor_leanprover_skills_dir() / "skills",)

#: Directories searched when a plugin is named rather than given as a path: the
#: vendored ``lean4-skills`` plugins (``lean4``).
_PLUGIN_CATALOGS: tuple[Path, ...] = (_vendor_lean4_skills_dir() / "plugins",)


def _resolve(spec: str, catalogs: tuple[Path, ...], kind: str) -> Path:
    """Resolve a ``spec`` to a directory: an existing path, else a catalog name."""
    p = Path(spec).expanduser()
    if p.is_dir():
        return p.resolve()
    for catalog in catalogs:
        candidate = catalog / spec
        if candidate.is_dir():
            return candidate
    known = sorted(
        d.name for c in catalogs if c.is_dir() for d in c.iterdir() if d.is_dir()
    )
    raise ValueError(
        f"unknown {kind} {spec!r}: not an existing directory and not found in any "
        f"catalog; known {kind}s: {known}"
    )


def resolve_skill(spec: str) -> Path:
    """Resolve a skill name (from a catalog) or a path to its source directory."""
    return _resolve(spec, _SKILL_CATALOGS, "skill")


def resolve_plugin(spec: str) -> Path:
    """Resolve a plugin name (from a catalog) or a path to its source directory."""
    return _resolve(spec, _PLUGIN_CATALOGS, "plugin")
