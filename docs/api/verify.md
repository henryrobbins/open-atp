---
tocdepth: 3
---

# `verify`

The verification report and the shared verifier: compile a candidate project in a sandbox
and judge whether it compiles, is `sorry`-free, and is axiom-clean. Every prover
funnels its output through the {class}`~open_atp.verify.Verifier`.

## Verifier

```{eval-rst}
.. autoclass:: open_atp.verify.Verifier
```

## Report

```{eval-rst}
.. autoclass:: open_atp.verify.VerificationReport
```

## Standard Verifiers

```{eval-rst}
.. autofunction:: open_atp.verify.docker_verifier

.. autofunction:: open_atp.verify.modal_verifier
```

## Axioms

```{eval-rst}
.. autodata:: open_atp.verify.STANDARD_AXIOMS
```
