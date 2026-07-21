---
tocdepth: 3
---

# `auth`

The credential vocabulary: what a prover authenticates with, whether it is present on
this host, and how long it stays valid. Every prover reports one
{class}`~open_atp.auth.AuthStatus` from
{meth}`~open_atp.provers.base.AutomatedProver.auth_status`, which the
`open-atp auth-status` command tabulates across the standard catalog.

Reading a credential never contacts its provider, so a present, unexpired one can
still be revoked or simply wrong.

## Status

```{eval-rst}
.. autoclass:: open_atp.auth.AuthStatus
```

## Classification

```{eval-rst}
.. autoclass:: open_atp.auth.AuthKind
   :members:

.. autoclass:: open_atp.auth.AuthState
   :members:

.. autodata:: open_atp.auth.EXPIRY_WARNING
```
