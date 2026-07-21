# `examples`

Tiny, ready-to-run example tasks bundled with the package — each a single `sorry`'d
exercise from *Mathematics in Lean*. {func}`~open_atp.examples.example_task` stages the
chosen asset into the pinned Mathlib skeleton ({func}`~open_atp.lean.create_project`),
yielding a complete {class}`~open_atp.lean.ProofTask`. Handing it to
{meth}`~open_atp.provers.base.AutomatedProver.prove` exercises the whole pipeline
(stage → generate → verify) end to end, so it doubles as a setup smoke test. The
exercise sources are shown on the {doc}`../examples` page.

```{eval-rst}
.. autofunction:: open_atp.examples.example_task

.. autofunction:: open_atp.examples.example_assets

.. autoclass:: open_atp.examples.EXAMPLE
   :members:
```
