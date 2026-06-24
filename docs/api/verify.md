# `verify`

The output types and the shared verifier: compile a candidate project in a sandbox
and judge whether it compiles, is `sorry`-free, and is axiom-clean. Every prover
funnels its output through the {class}`~open_atp.verify.Verifier`.

```{eval-rst}
.. autoclass:: open_atp.verify.VerificationReport
   :exclude-members: compiles, sorry_free, axioms, compile_log, per_file, non_standard_axioms, verified

.. autoclass:: open_atp.verify.ProofResult
   :exclude-members: prover, verification, output_dir, completed_files, cost_usd, duration_s, metadata, error, wd, logs_dir, success

.. autodata:: open_atp.verify.STANDARD_AXIOMS

.. autofunction:: open_atp.verify.docker_verifier

.. autoclass:: open_atp.verify.Verifier
   :members: check_compatible, verify
```
