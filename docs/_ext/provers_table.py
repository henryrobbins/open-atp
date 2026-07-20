"""Generate the prover comparison tables from ``docs/provers.yaml``.

Single source of truth for the two hand-synced tables that previously lived in
``README.md`` and ``docs/provers/index.md``. This module renders both from the
YAML and is used in two ways:

* As a **Sphinx extension** (``extensions = [..., "provers_table"]``): the
  ``builder-inited`` hook writes ``docs/provers/_table.md`` (gitignored), which
  ``index.md`` pulls in with an ``{include}`` directive. Regenerated on every
  build, including Read the Docs.
* As a **CLI** (``python docs/_ext/provers_table.py``): materializes the table
  into ``README.md`` between marker comments (GitHub can't run Sphinx). Pass
  ``--check`` to fail without writing if the README table is stale -- wired into
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
YAML_PATH = DOCS_DIR / "provers.yaml"
FRAGMENT_PATH = PROVERS_DIR / "_table.md"
README_PATH = REPO_ROOT / "README.md"

_GENERATED = "<!-- Generated from docs/provers.yaml by the provers_table extension. Do not edit by hand. -->"  # noqa: E501

BEGIN_MARKER = "<!-- BEGIN PROVER TABLE (generated from docs/provers.yaml) -->"
END_MARKER = "<!-- END PROVER TABLE -->"

EM_DASH = "—"
CHECK = "✓"
CROSS = "✗"

COLUMNS = ("Prover", "ID", "Skills", "MCP", "Paper", "Source")


def _link(item: dict | None) -> str:
    """Render a ``{label, url}`` mapping as a Markdown link, or — if null."""
    if not item:
        return EM_DASH
    return f"[{item['label']}]({item['url']})"


def _mcp_cell(prover: dict) -> str:
    """Render the MCP cell: ✓/✗ for a boolean, — when null (e.g. hosted)."""
    if prover.get("mcp") is None:
        return EM_DASH
    return CHECK if prover["mcp"] else CROSS


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


def _render_table(data: dict, page_prefix: str, *, cite: bool) -> str:
    """Render the Markdown table. ``page_prefix`` prefixes prover doc links so
    the README (repo root) and the docs page (``docs/provers/``) resolve them.
    ``cite`` renders the Paper cell as an author-year ``{cite:t}`` role (docs)
    rather than a plain link (README)."""
    skill_urls = data["skills"]
    header = "| " + " | ".join(COLUMNS) + " |"
    sep = "| " + " | ".join("---" for _ in COLUMNS) + " |"
    rows = [header, sep]
    for p in data["provers"]:
        prover = f"[{p['name']}]({page_prefix}{p['page']}.md)"
        cells = [
            prover,
            f"`{p['id']}`",
            _skills_cell(p.get("skills") or [], skill_urls),
            _mcp_cell(p),
            _paper_cell(p, cite=cite),
            _link(p.get("source")),
        ]
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"


def load() -> dict:
    with YAML_PATH.open() as fh:
        return yaml.safe_load(fh)


def render_docs_table(data: dict) -> str:
    # index.md lives in docs/provers/, so doc links are bare ``<page>.md``.
    return _render_table(data, page_prefix="", cite=True)


def render_readme_table(data: dict) -> str:
    # README.md lives at the repo root, so doc links are ``docs/provers/<page>.md``.
    return _render_table(data, page_prefix="docs/provers/", cite=False)


def render_meta(prover: dict, skill_urls: dict[str, str]) -> str:
    """Render a prover's metadata as one compact inline bar.

    A field list wastes vertical space (each value drops to its own line), so we
    emit a single ``label value · label value`` paragraph. ``ID`` is always
    shown; ``Company`` is dropped when null (e.g. unaffiliated).
    """
    parts = [f"**ID** `{prover['id']}`"]
    if prover.get("company"):
        parts.append(f"**Company** {_link(prover['company'])}")
    return " · ".join(parts) + "\n"


def _meta_path(page: str) -> Path:
    return PROVERS_DIR / f"_meta_{page}.md"


def _write_if_changed(path: Path, content: str) -> None:
    # Skip the write when unchanged so `sphinx-autobuild` doesn't see a touched
    # file and rebuild in an infinite loop.
    if path.exists() and path.read_text() == content:
        return
    path.write_text(content)


def write_fragment(data: dict | None = None) -> None:
    """Write the table fragment plus a per-page metadata fragment for each prover."""
    data = data if data is not None else load()
    _write_if_changed(FRAGMENT_PATH, f"{_GENERATED}\n\n{render_docs_table(data)}")
    skill_urls = data["skills"]
    for prover in data["provers"]:
        _write_if_changed(
            _meta_path(prover["page"]),
            f"{_GENERATED}\n\n{render_meta(prover, skill_urls)}",
        )


def _splice_readme(text: str, table: str) -> str:
    try:
        pre, rest = text.split(BEGIN_MARKER, 1)
        _, post = rest.split(END_MARKER, 1)
    except ValueError as exc:
        raise SystemExit(
            f"README markers {BEGIN_MARKER!r} / {END_MARKER!r} not found in "
            f"{README_PATH}"
        ) from exc
    return f"{pre}{BEGIN_MARKER}\n{table}{END_MARKER}{post}"


def update_readme(*, check: bool) -> bool:
    """Sync README's prover table. Returns True if it was (or would be) changed.

    With ``check=True`` nothing is written; the return value reports drift so the
    caller can fail CI.
    """
    data = load()
    current = README_PATH.read_text()
    updated = _splice_readme(current, render_readme_table(data))
    changed = updated != current
    if changed and not check:
        README_PATH.write_text(updated)
    return changed


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if README's table is stale; do not write",
    )
    args = parser.parse_args(argv)

    write_fragment()
    changed = update_readme(check=args.check)
    if args.check and changed:
        print(
            "README prover table is out of date. Run `make gen-provers` and "
            "commit the result.",
            file=sys.stderr,
        )
        return 1
    if not args.check and changed:
        print(f"Updated {README_PATH.relative_to(REPO_ROOT)}")
    return 0


# --- Sphinx extension -------------------------------------------------------


def setup(app):  # type: ignore[no-untyped-def]  # noqa: ANN001
    """Sphinx entry point: regenerate the docs fragment before reading sources."""
    app.connect("builder-inited", lambda _app: write_fragment())
    return {"parallel_read_safe": True, "parallel_write_safe": True}


if __name__ == "__main__":
    raise SystemExit(_main())
