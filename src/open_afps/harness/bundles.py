"""Selectable agent assets mounted into the agent workdir.

Two kinds of asset can be mounted, both resolvable **by name** (from a vendored
catalog) or **by full path**:

* **skills** -- host-agnostic Agent Skills (``<name>/SKILL.md``) copied into every
  harness's skill location (``.claude/skills``, ``.agents/skills``,
  ``VIBE_HOME/skills``). The default is ``lean-proof`` from the vendored
  ``leanprover/skills`` catalog.
* **plugins** -- Claude Code plugins (a dir with ``.claude-plugin/plugin.json``)
  loaded **only** by the Claude harness via ``--plugin-dir``. The default is the
  vendored ``lean4`` plugin. The other harnesses can't consume plugins and ignore
  them.

A named :class:`AssetBundle` packages a coherent preset (skills + plugins +
optional default prompt / extra dirs); :func:`bundle_for_config` resolves the
active bundle for a config and applies any per-run ``skills`` / ``plugins``
overrides.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from open_afps.harness._paths import (
    _vendor_lean4_skills_dir,
    _vendor_leanprover_skills_dir,
    _vendor_numina_dir,
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


@dataclass(frozen=True)
class AssetBundle:
    """A selectable set of agent assets mounted into the workdir.

    Attributes
    ----------
    name:
        Bundle identifier matching ``AgentProverConfig.assets``.
    skills:
        Individual skill source dirs (each a ``<name>/SKILL.md`` tree). Each is
        copied to ``<dest>/<dir-name>`` in every harness's skill location.
    plugins:
        Claude Code plugin source dirs (each a ``.claude-plugin/plugin.json``
        tree). Mounted and ``--plugin-dir``-loaded by the Claude harness only;
        ignored by the others.
    prompt_file:
        Optional default system prompt for the bundle, used when the task carries
        no explicit ``instructions``.
    extra_dirs:
        Additional ``(src_dir, dest_relative_to_workdir)`` trees to copy in (e.g.
        Numina's coordinator/subagent prompts under ``.claude/prompts``).
    skills_dir:
        Legacy whole-directory mount: its *contents* are copied to the skill
        location root (so a top-level ``SKILL.md`` lands at ``<dest>/SKILL.md``).
        Used by the Numina bundle, which mounts one root-level skill plus helper
        subdirs (``cli/`` etc.). Prefer ``skills`` for ordinary skills.
    """

    name: str
    skills: tuple[Path, ...] = ()
    plugins: tuple[Path, ...] = ()
    prompt_file: Path | None = None
    extra_dirs: tuple[tuple[Path, str], ...] = ()
    skills_dir: Path | None = None

    def default_prompt(self) -> str | None:
        if self.prompt_file is not None and self.prompt_file.is_file():
            return self.prompt_file.read_text()
        return None


def _default_bundle() -> AssetBundle:
    """The built-in default: the official ``lean-proof`` skill + ``lean4`` plugin.

    Every harness gets ``lean-proof`` (a host-agnostic skill); the Claude harness
    additionally loads the ``lean4`` plugin (commands/subagents/hooks it alone can
    consume).
    """
    return AssetBundle(
        name="default",
        skills=(resolve_skill("lean-proof"),),
        plugins=(resolve_plugin("lean4"),),
    )


def _numina_bundle() -> AssetBundle:
    root = _vendor_numina_dir()
    return AssetBundle(
        name="numina",
        # Numina is one root-mounted skill (top-level SKILL.md + cli/ helpers), so
        # its whole skills/ tree is copied to the skill-location root.
        skills_dir=root / "skills",
        prompt_file=root / "prompts" / "main_entry.md",
        # The coordinator prompt tells the agent to read its subagent prompts from
        # .claude/prompts/subagent_prompts/, so stage the whole prompt tree there.
        extra_dirs=((root / "prompts", ".claude/prompts"),),
    )


#: Asset-bundle registry selected by ``AgentProverConfig.assets``.
BUNDLES: dict[str, Callable[[], AssetBundle]] = {
    "default": _default_bundle,
    "numina": _numina_bundle,
}

#: Eagerly-resolved default (the common case), so callers have a ready bundle.
DEFAULT_BUNDLE = _default_bundle()


def resolve_bundle(name: str) -> AssetBundle:
    """Resolve an ``assets`` name to its :class:`AssetBundle`."""
    try:
        return BUNDLES[name]()
    except KeyError:
        raise ValueError(
            f"unknown asset bundle {name!r}; known: {sorted(BUNDLES)}"
        ) from None


def bundle_for_config(config: Any) -> AssetBundle:
    """The active bundle for a config, with per-run skill/plugin overrides applied.

    Starts from the named bundle (``config.assets``) and, when ``config.skills`` /
    ``config.plugins`` are given (a list of names or paths), replaces the bundle's
    skills/plugins with the resolved set. An empty list explicitly mounts none; an
    unset (``None``) field keeps the bundle's own assets.
    """
    bundle = resolve_bundle(getattr(config, "assets", "default"))
    skills = getattr(config, "skills", None)
    plugins = getattr(config, "plugins", None)
    if skills is not None:
        bundle = replace(bundle, skills=tuple(resolve_skill(s) for s in skills))
    if plugins is not None:
        bundle = replace(bundle, plugins=tuple(resolve_plugin(p) for p in plugins))
    return bundle
