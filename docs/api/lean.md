# `lean`

The Lean input contract: the project to complete, the task describing what to fill,
and the staging helper. A project is a *full lake project* carrying its own
`lean-toolchain` and `lake-manifest.json`.

```{eval-rst}
.. autoclass:: open_atp.lean.LeanProject

.. autoclass:: open_atp.lean.ProofTask

.. autoexception:: open_atp.lean.ToolchainMismatch
   :no-members:

.. autofunction:: open_atp.lean.stage_files
```

## Image defaults

The constants describing the baked sandbox image (the contract the verifier
enforces).

```{eval-rst}
.. autodata:: open_atp.images.DEFAULT_IMAGE

.. autodata:: open_atp.images.DEFAULT_TOOLCHAIN

.. autodata:: open_atp.images.DEFAULT_MATHLIB_REV
```
