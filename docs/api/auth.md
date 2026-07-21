---
tocdepth: 2
---

# `auth`

The credential vocabulary: what a prover authenticates with, whether it is present on
this host, and how long it stays valid. Every prover reports one
{class}`~open_atp.auth.AuthStatus` from
{meth}`~open_atp.provers.base.AutomatedProver.auth_status`.

```{eval-rst}
.. autoclass:: open_atp.auth.AuthStatus
```

```{eval-rst}
.. autoclass:: open_atp.auth.AuthKind
   :members:

.. autoclass:: open_atp.auth.AuthState
   :members:

.. autodata:: open_atp.auth.EXPIRY_WARNING_THRESHOLD
```
