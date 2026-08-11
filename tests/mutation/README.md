# Mutation harnesses — proving the canon-lite controls are load-bearing

A passing test proves nothing until the thing it guards can be removed and the test
observed to fail. These harnesses do exactly that: each one breaks one property in
the source, runs the single control that should catch it, and expects a FAILURE.

```bash
python3 tests/mutation/prove_stage1.py    # L3-ASSIST Stage 1 (wiring)
python3 tests/mutation/prove_stage2.py    # L3-ASSIST Stage 2 (targeted repair)
python3 tests/mutation/prove_wiring.py    # L3-ASSIST delivery seam + adapter session
```

They exit `0` only when **every** mutation was caught. A run that prints
`PATTERN-MISS` means a mutation's pattern no longer matches the source: the control
was never exercised, and that counts as a failure, not a skip.

## ⚠️ A killed run leaves the tree mutated

Each file is restored in a `finally`, which covers an ordinary error or a Ctrl-C —
but **not** a `SIGKILL`, and that is exactly what a shell or agent timeout sends.
A harness killed mid-iteration leaves ONE file carrying ONE mutation. This has
already happened once: a two-minute command timeout killed `prove_wiring` and left
`canon_lite.py` gating `shadow` behind the assist allowlist.

The saving grace is that the harnesses detect it themselves on the next run: a
leftover mutation means that mutation's own PATTERN no longer matches, so the run
reports `PATTERN-MISS` and exits non-zero rather than quietly snapshotting the
mutated file as its baseline. **Never dismiss a `PATTERN-MISS` as a stale
pattern without checking the source first** — a real leftover looks identical to
a pattern that has drifted, and only one of them is your working tree lying to you.

Give these runs a generous timeout. `prove_wiring` alone runs 30 pytest
invocations.

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
