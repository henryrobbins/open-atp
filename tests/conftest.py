"""Shared test config: load ``.env`` so opt-in tests can read their credentials.

The live Aristotle test is marked ``aristotle_api`` and excluded by default via
``addopts`` in ``pyproject.toml``; run it explicitly with ``-m aristotle_api``.
Its ``ARISTOTLE_API_KEY`` is read from a ``.env`` file at the repo root.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def _load_dotenv(path: Path) -> None:
    """Minimal ``.env`` reader: ``KEY=VALUE`` lines, without overriding real env."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv(_ENV_FILE)
