# Mutation harnesses — proving the canon-lite controls are load-bearing

A passing test proves nothing until the thing it guards can be removed and the test
observed to fail. These harnesses do exactly that: each one breaks one property in
the source, runs the single control that should catch it, and expects a FAILURE.

```bash
python3 tests/mutation/prove_stage1.py    # L3-ASSIST Stage 1 (wiring)
python3 tests/mutation/prove_stage2.py    # L3-ASSIST Stage 2 (targeted repair)
```

Both exit `0` only when **every** mutation was caught, and both restore every file
they touched in a `finally` — including after a keyboard interrupt. A run that
prints `PATTERN-MISS` means a mutation's pattern no longer matches the source: the
control was never exercised, and that counts as a failure, not a skip.

Re-run them after any change to the files they mutate. A green pytest run means
nothing on its own.

Two traps these harnesses have already paid for, both of which produced false
passes before they were fixed:

* **A same-size mutation is invisible to Python's bytecode cache.** `.pyc`
  invalidation keys on (mtime seconds, size), so `= 2` → `= 3` changes neither when
  the rewrite lands inside the same second — the stale bytecode runs and the
  mutation "survives" without ever having executed. Both harnesses clear
  `__pycache__` before every run.
* **Redundant guards cannot both be proven by one control.** Where two independent
  mechanisms hold one property, no single-edit mutation breaks it. Those cases are
  either split into a control per mechanism, or the mutation is aimed at what a
  single edit genuinely can break — never left implying a proof that does not exist.
