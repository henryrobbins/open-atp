---
tocdepth: 3
---

# `lean`

The Lean input contract: the project to complete, the task describing what to fill,
and the staging helper. A project is a *full lake project* carrying its own
`lean-toolchain` and `lake-manifest.json`.

## Project

A full lake project on disk is just `LeanProject(Path(path))`.
{func}`~open_atp.lean.create_project` stages one or more bare `.lean` files into the
pinned Mathlib skeleton.

```{eval-rst}
.. autoclass:: open_atp.lean.LeanProject

.. autofunction:: open_atp.lean.create_project
```

## Task

```{eval-rst}
.. autoclass:: open_atp.lean.ProofTask
```

## Exceptions

```{eval-rst}
.. autoexception:: open_atp.lean.ToolchainMismatch
   :no-members:

.. autoexception:: open_atp.lean.MathlibRevMismatch
   :no-members:
```

## Image defaults

The constants describing the baked sandbox image (the contract the verifier
enforces).

```{eval-rst}
.. autodata:: open_atp.images.DEFAULT_IMAGE

.. autoclass:: open_atp.images.Image
   :members:
```
