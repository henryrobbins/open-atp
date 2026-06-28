"""Tiny, ready-to-run example tasks bundled with the package.

Each example is a single bare ``.lean`` file (an exercise from *Mathematics in
Lean*, stated with ``sorry``) shipped under ``examples/assets``. :func:`example_task`
stages the chosen file into the pinned Mathlib skeleton with
:func:`~open_atp.lean.create_project`, yielding a complete
:class:`~open_atp.lean.ProofTask`.

So :func:`example_task` doubles as a setup smoke test: handing its result to
:meth:`~open_atp.provers.base.AutomatedProver.prove` exercises the whole pipeline
(stage -> generate -> verify) end to end and confirms your backend image and agent
credentials are wired up correctly. The exercise sources are shown on the
:doc:`/examples` page.

Examples
--------

>>> import tempfile
>>> from open_atp.examples import EXAMPLE, example_task
>>> task = example_task(EXAMPLE.MUL_REORDER)
>>> task.project.lean_toolchain
'leanprover/lean4:v4.28.0'
>>> [p.name for p in task.project.files_with_sorry()]
['MulReorder.lean']
"""

from __future__ import annotations

import tempfile
from enum import Enum
from pathlib import Path

from open_atp.lean import ProofTask, create_project

#: Directory holding the bundled example ``.lean`` assets.
_ASSETS_DIR = Path(__file__).resolve().parent / "assets"


class EXAMPLE(Enum):
    """The bundled examples accepted by :func:`example_task`.

    Each member's value is the ``.lean`` asset's filename stem under
    ``examples/assets``; all are exercises from *Mathematics in Lean*:

    - ``MUL_REORDER`` -- C02 "Calculating": reorder a product of reals.
    - ``ABS_MUL_LT`` -- C03 "Logic": a product of two small reals is small.
    - ``INTER_SUBSET`` -- C04 "Sets and Functions": intersecting preserves a
      subset relation.
    - ``INTER_UNION_DISTRIB`` -- C04 "Sets and Functions": intersection
      distributes over union.
    - ``SMUL_ADD`` -- C09 "Linear Algebra": scalar multiplication distributes
      over addition.
    """

    #: C02 "Calculating": reorder a product of reals.
    MUL_REORDER = "MulReorder"
    #: C03 "Logic": a product of two small reals is small.
    ABS_MUL_LT = "AbsMulLt"
    #: C04 "Sets and Functions": intersecting preserves a subset relation.
    INTER_SUBSET = "InterSubset"
    #: C04 "Sets and Functions": intersection distributes over union.
    INTER_UNION_DISTRIB = "InterUnionDistrib"
    #: C09 "Linear Algebra": scalar multiplication distributes over addition.
    SMUL_ADD = "SmulAdd"


def example_assets() -> list[Path]:
    """The bundled example ``.lean`` asset files (one per :class:`EXAMPLE` member)."""
    return [_ASSETS_DIR / f"{member.value}.lean" for member in EXAMPLE]


def example_task(name: EXAMPLE) -> ProofTask:
    """Stage a bundled example into a ready-to-run :class:`~open_atp.lean.ProofTask`.

    ``name`` is an :class:`EXAMPLE` member (the exercise sources are shown on the
    :doc:`/examples` page). The bare ``.lean`` asset is staged into the pinned Mathlib
    skeleton (:func:`~open_atp.lean.create_project`) under a fresh temp directory, so
    the returned task is a complete lake project pinned to
    :data:`~open_atp.DEFAULT_IMAGE` -- hand it straight to
    :meth:`~open_atp.provers.base.AutomatedProver.prove`.
    """
    asset = _ASSETS_DIR / f"{name.value}.lean"
    dest = Path(tempfile.mkdtemp()) / "project"
    return ProofTask(create_project([asset], dest))


__all__ = ["EXAMPLE", "example_assets", "example_task"]
