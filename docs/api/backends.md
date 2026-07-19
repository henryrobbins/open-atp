---
tocdepth: 3
---

# `backends`

A {class}`~open_atp.backends.base.ComputeBackend` runs a command over a working
directory inside a Lean+Mathlib sandbox. It is the single load-bearing primitive of
the platform, used both to run a coding agent and to run `lake env lean ...` for
verification. A {class}`~open_atp.backends.base.ComputeSession` keeps that sandbox
alive across several commands -- generation *then* verification against the same hot
filesystem -- without paying a second spin-up.

## Base

```{eval-rst}
.. autoclass:: open_atp.backends.base.ComputeBackend

.. autoclass:: open_atp.backends.base.CommandHandle

.. autoclass:: open_atp.backends.base.CommandResult
   :no-members:

.. autoclass:: open_atp.backends.base.ComputeSession
```

## Docker

```{eval-rst}
.. autoclass:: open_atp.backends.docker.DockerBackend
   :show-inheritance:

.. autoclass:: open_atp.backends.docker.DockerSession
   :show-inheritance:
```

## Modal

```{eval-rst}
.. autoclass:: open_atp.backends.modal.ModalBackend
   :show-inheritance:

.. autoclass:: open_atp.backends.modal.ModalSession
   :show-inheritance:
```
