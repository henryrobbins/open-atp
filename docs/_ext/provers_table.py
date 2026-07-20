"""Generate the prover and harness comparison tables from the docs YAML.

Single source of truth for the hand-synced tables that previously lived in
``README.md`` and the docs. A **prover** row (``docs/provers.yaml``) names a
method/model; a **harness** row (``docs/harnesses.yaml``) names the coding-agent
CLI it runs on, and carries the skills/MCP columns since those are harness
properties. This module renders both tables and is used in two ways:

* As a **Sphinx extension** (``extensions = [..., "provers_table"]``): the
  ``builder-inited`` hook writes the gitignored fragments
  (``docs/provers/_table.md``, ``docs/harnesses/_table.md``) that the index
  pages pull in with ``{include}``, plus a per-page ``_meta_<page>.md``.
  Regenerated on every build, including Read the Docs.
* As a **CLI** (``python docs/_ext/provers_table.py``): materializes both tables
  into ``README.md`` between marker comments (GitHub can't run Sphinx). Pass
  ``--check`` to fail without writing if a README table is stale -- wired into
  ``make check-provers``.

Sphinx is imported lazily inside ``setup`` so the CLI works with only PyYAML
installed (the dev extra), not the full docs toolchain.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

_THIS = Path(__file__).resolve()
DOCS_DIR = _THIS.parent.parent
REPO_ROOT = DOCS_DIR.parent
PROVERS_DIR = DOCS_DIR / "provers"
HARNESSES_DIR = DOCS_DIR / "harnesses"
PROVERS_YAML = DOCS_DIR / "provers.yaml"
HARNESSES_YAML = DOCS_DIR / "harnesses.yaml"
README_PATH = REPO_ROOT / "README.md"

_GENERATED = "<!-- Generated from docs/*.yaml by the provers_table extension. Do not edit by hand. -->"  # noqa: E501

PROVER_BEGIN = "<!-- BEGIN PROVER TABLE (generated from docs/provers.yaml) -->"
PROVER_END = "<!-- END PROVER TABLE -->"
HARNESS_BEGIN = "<!-- BEGIN HARNESS TABLE (generated from docs/harnesses.yaml) -->"
HARNESS_END = "<!-- END HARNESS TABLE -->"

EM_DASH = "—"
CHECK = "✓"
CROSS = "✗"

PROVER_COLUMNS = ("Prover", "ID", "Harness", "Paper", "Source")
HARNESS_COLUMNS = ("Harness", "ID", "Skills", "MCP", "Source")


def _link(item: dict | None) -> str:
    """Render a ``{label, url}`` mapping as a Markdown link, or — if null."""
    if not item:
        return EM_DASH
    return f"[{item['label']}]({item['url']})"


def _page_link(name: str, page: str | None, prefix: str) -> str:
    """Render ``name`` as a link to its doc page, or plain text if it has none."""
    if not page:
        return name
    return f"[{name}]({prefix}{page}.md)"


def _mcp_cell(mcp: bool | None) -> str:
    """Render the MCP cell: ✓/✗ for a boolean, — when null (e.g. hosted)."""
    if mcp is None:
        return EM_DASH
    return CHECK if mcp else CROSS


def _skills_cell(keys: list[str], skill_urls: dict[str, str]) -> str:
    if not keys:
        return EM_DASH
    return ", ".join(f"[{k}]({skill_urls[k]})" for k in keys)


def _paper_cell(prover: dict, *, cite: bool) -> str:
    """Render the Paper cell.

    In the docs (``cite=True``) a prover's ``cite`` BibTeX key becomes an
    author-year ``{cite:t}`` role resolved against ``docs/citations.md``; the
    README (``cite=False``) and any prover without a key fall back to the plain
    ``paper`` link.
    """
    if cite and prover.get("cite"):
        return f"{{cite:t}}`{prover['cite']}`"
    return _link(prover.get("paper"))


def _table(columns: tuple[str, ...], rows: list[list[str]]) -> str:
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [header, sep] + ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def _harness_index(harnesses: list[dict]) -> dict[str, dict]:
    return {h["id"]: h for h in harnesses}


def _render_prover_table(
    data: dict,
    harnesses: dict,
    *,
    prover_prefix: str,
    harness_prefix: str,
    cite: bool,
) -> str:
    """Render the prover table. ``*_prefix`` prefix the prover- and harness-page
    links so the README (repo root) and the docs page (``docs/provers/``) resolve
    them. ``cite`` renders the Paper cell as an author-year ``{cite:t}`` role
    (docs) rather than a plain link (README)."""
    by_id = _harness_index(harnesses["harnesses"])
    rows = []
    for p in data["provers"]:
        h = by_id.get(p.get("harness"))
        harness_cell = (
            _page_link(h["name"], h.get("page"), harness_prefix) if h else EM_DASH
        )
        rows.append(
            [
                _page_link(p["name"], p["page"], prover_prefix),
                f"`{p['id']}`",
                harness_cell,
                _paper_cell(p, cite=cite),
                _link(p.get("source")),
            ]
        )
    return _table(PROVER_COLUMNS, rows)


def _render_harness_table(harnesses: dict, *, harness_prefix: str) -> str:
    """Render the harness table. ``harness_prefix`` prefixes harness-page links."""
    skill_urls = harnesses["skills"]
    rows = []
    for h in harnesses["harnesses"]:
        rows.append(
            [
                _page_link(h["name"], h.get("page"), harness_prefix),
                f"`{h['id']}`",
                _skills_cell(h.get("skills") or [], skill_urls),
                _mcp_cell(h.get("mcp")),
                _link(h.get("source")),
            ]
        )
    return _table(HARNESS_COLUMNS, rows)


def load() -> tuple[dict, dict]:
    with PROVERS_YAML.open() as fh:
        provers = yaml.safe_load(fh)
    with HARNESSES_YAML.open() as fh:
        harnesses = yaml.safe_load(fh)
    return provers, harnesses


def render_meta(item: dict) -> str:
    """Render a prover's or harness's metadata as one compact inline bar.

    A field list wastes vertical space (each value drops to its own line), so we
    emit a single ``label value · label value`` paragraph. ``ID`` is always
    shown; ``Company`` is dropped when null (e.g. unaffiliated).
    """
    parts = [f"**ID** `{item['id']}`"]
    if item.get("company"):
        parts.append(f"**Company** {_link(item['company'])}")
    return " · ".join(parts) + "\n"


def _write_if_changed(path: Path, content: str) -> None:
    # Skip the write when unchanged so `sphinx-autobuild` doesn't see a touched
    # file and rebuild in an infinite loop.
    if path.exists() and path.read_text() == content:
        return
    path.write_text(content)


def write_fragment(data: tuple[dict, dict] | None = None) -> None:
    """Write both table fragments plus a per-page metadata fragment for each row."""
    provers, harnesses = data if data is not None else load()
    _write_if_changed(
        PROVERS_DIR / "_table.md",
        f"{_GENERATED}\n\n"
        + _render_prover_table(
            provers,
            harnesses,
            prover_prefix="",
            harness_prefix="../harnesses/",
            cite=True,
        ),
    )
    _write_if_changed(
        HARNESSES_DIR / "_table.md",
        f"{_GENERATED}\n\n" + _render_harness_table(harnesses, harness_prefix=""),
    )
    for item in provers["provers"]:
        _write_if_changed(
            PROVERS_DIR / f"_meta_{item['page']}.md",
            f"{_GENERATED}\n\n{render_meta(item)}",
        )
    for item in harnesses["harnesses"]:
        if item.get("page"):
            _write_if_changed(
                HARNESSES_DIR / f"_meta_{item['page']}.md",
                f"{_GENERATED}\n\n{render_meta(item)}",
            )


def _splice(text: str, begin: str, end: str, table: str) -> str:
    try:
        pre, rest = text.split(begin, 1)
        _, post = rest.split(end, 1)
    except ValueError as exc:
        raise SystemExit(
            f"README markers {begin!r} / {end!r} not found in {README_PATH}"
        ) from exc
    return f"{pre}{begin}\n{table}{end}{post}"


def update_readme(*, check: bool) -> bool:
    """Sync README's prover and harness tables. Returns True if either was (or
    would be) changed.

    With ``check=True`` nothing is written; the return value reports drift so the
    caller can fail CI.
    """
    provers, harnesses = load()
    current = README_PATH.read_text()
    updated = _splice(
        current,
        PROVER_BEGIN,
        PROVER_END,
        _render_prover_table(
            provers,
            harnesses,
            prover_prefix="docs/provers/",
            harness_prefix="docs/harnesses/",
            cite=False,
        ),
    )
    updated = _splice(
        updated,
        HARNESS_BEGIN,
        HARNESS_END,
        _render_harness_table(harnesses, harness_prefix="docs/harnesses/"),
    )
    changed = updated != current
    if changed and not check:
        README_PATH.write_text(updated)
    return changed


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if a README table is stale; do not write",
    )
    args = parser.parse_args(argv)

    write_fragment()
    changed = update_readme(check=args.check)
    if args.check and changed:
        print(
            "README tables are out of date. Run `make gen-provers` and "
            "commit the result.",
            file=sys.stderr,
        )
        return 1
    if not args.check and changed:
        print(f"Updated {README_PATH.relative_to(REPO_ROOT)}")
    return 0


# --- Sphinx extension -------------------------------------------------------


def setup(app):  # type: ignore[no-untyped-def]  # noqa: ANN001
    """Sphinx entry point: regenerate the docs fragments before reading sources."""
    app.connect("builder-inited", lambda _app: write_fragment())
    return {"parallel_read_safe": True, "parallel_write_safe": True}


if __name__ == "__main__":
    raise SystemExit(_main())
