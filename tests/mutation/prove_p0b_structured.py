#!/usr/bin/env python3
"""Prove the P0-B STRUCTURED-CONTRACT controls are load-bearing: break each, expect a FAIL.

Live job `oehhe741` refused after ~99s of paid model work because the Story Bible was
asked for a fenced JSON block in free text and nothing obliged the model to produce one.
The fix has four moving parts, and each one is a place a future edit can silently undo:

  1. TRANSPORT — `Worker.response_format` threaded to `chat.completions.create`, and
     ABSENT (not `None`) when unasked, so off-path calls stay byte-identical;
  2. SIDECAR — `canon_registry` threaded to its consumer instead of scraped out of prose,
     with an EMPTY dict counting as a real answer rather than an absence;
  3. CANDIDATE FILTER — best-of may only offer the judge candidates that can actually arm
     (self-hash, outline binding, bible binding, non-empty);
  4. SURGICAL CONTAINMENT — the ledger patch is skipped BEFORE its provider call when
     assist holds a bound envelope, not after it has been bought.

Every break below reverts exactly one of those and names the single test that must catch
it. Anything that SURVIVES is a control nothing is holding.

🔴 THIS FILE EXISTS BECAUSE AN EARLIER RUN CAUGHT A VACUOUS TEST. The containment mutant
   initially SURVIVED — not because containment was wrong, but because the containment
   test's bible prose never contained the banned term, so the surgical branch collected
   zero lines and returned before any provider call with or without the guard. The test
   was green and proved nothing. Keep that mutant: it is the one guarding the guard.

Run it from anywhere:  python3 tests/mutation/prove_p0b_structured.py
Override the checkout with PROVE_WT=/path/to/worktree.
"""
import os
import pathlib
import subprocess
import sys

WT = pathlib.Path(os.environ.get(
    "PROVE_WT", str(pathlib.Path(__file__).resolve().parents[2])))
CORE = WT / "python/orchestrator/core.py"
STATIC = WT / "python/orchestrator/static.py"
NAPI = WT / "python/narration_api.py"
RUNNER = WT / "python/canon_lite_qc_runner.py"

T_TRANSPORT = "tests/python/test_narasi_structured_transport.py"
T_SIDECAR = "tests/python/test_canon_registry_sidecar_threading.py"
T_PROD = "tests/python/test_canon_lite_p0b_production_combination.py"
T_QC = "tests/python/test_canon_lite_qc_provider.py"

# (label, file, literal to find, replacement, test file, test that must FAIL)
BREAKS = [
    # ── 1. transport ─────────────────────────────────────────────────────
    ("response_format never added to the provider call",
     CORE,
     '    if response_format is not None:\n        kwargs["response_format"] = response_format',
     '    if False:\n        kwargs["response_format"] = response_format',
     T_TRANSPORT, "test_response_format_reaches_the_provider_call_intact"),
    ("response_format added unconditionally — None leaks onto every off-path call",
     CORE,
     '    if response_format is not None:\n        kwargs["response_format"] = response_format',
     '    if True:\n        kwargs["response_format"] = response_format',
     T_TRANSPORT, "test_default_worker_omits_the_response_format_key_entirely"),
    ("the dict is coerced to a bool on the way out",
     CORE,
     '        kwargs["response_format"] = response_format',
     '        kwargs["response_format"] = bool(response_format)',
     T_TRANSPORT, "test_response_format_reaches_the_provider_call_intact"),
    ("run_worker stops forwarding the worker's response_format",
     CORE,
     "phase=worker.phase, response_format=worker.response_format,",
     "phase=worker.phase,",
     T_TRANSPORT, "test_response_format_reaches_the_provider_call_intact"),

    # ── 2. canon_registry sidecar ────────────────────────────────────────
    ("empty sidecar misread as 'nothing threaded' — bills a re-extraction",
     NAPI,
     "                if isinstance(_reg_threaded, dict):",
     "                if isinstance(_reg_threaded, dict) and _reg_threaded:",
     T_SIDECAR, "test_an_empty_sidecar_still_bypasses_the_scrape_and_the_paid_fallback"),
    ("threaded sidecar ignored entirely — every assist job pays to re-derive it",
     NAPI,
     '                _reg_threaded = result.get("canon_registry")',
     "                _reg_threaded = None",
     T_SIDECAR, "test_a_populated_sidecar_also_bypasses_the_paid_fallback"),

    # ── 3. candidate filter (one break per check it must make) ───────────
    ("filter stops checking binds_bible — a cross-paired envelope reaches the judge",
     STATIC,
     "                                        and src.binds_bible(p[0])\n",
     "",
     T_PROD, "test_the_judge_is_never_offered_a_candidate_that_cannot_arm[bible]"),
    ("filter stops checking binds_outline — an envelope for another book reaches the judge",
     STATIC,
     "                                        and src.binds_outline(_bo_outline)\n",
     "",
     T_PROD, "test_the_judge_is_never_offered_a_candidate_that_cannot_arm[outline]"),
    ("filter stops checking verify_sha256 — a tampered envelope reaches the judge",
     STATIC,
     "                            return bool(src.verify_sha256()\n",
     "                            return bool(True\n",
     T_PROD, "test_the_judge_is_never_offered_a_candidate_that_cannot_arm[sha]"),

    # ── 3b. the default adapter factory's credential binding ─────────────
    # Reverts to the exact shape live job `yp8f04rr` died on: the credential tested
    # inline and never bound, so the default factory's `api_key` resolves to nothing.
    ("credential read but never bound — the default factory raises NameError",
     RUNNER,
     '    api_key = str(env.get(_QC_API_KEY_ENV) or "").strip()\n    if not api_key:',
     '    if not str(env.get(_QC_API_KEY_ENV) or "").strip():',
     T_QC, "test_8_18_the_default_factory_passes_the_env_credential_to_the_adapter"),

    # ── 4. surgical containment ──────────────────────────────────────────
    ("containment guard disabled — the patch is bought and the envelope invalidated",
     STATIC,
     "                                          and _cl_wants_semantic and _cl_semantic_source is not None):",
     "                                          and False):",
     T_PROD, "test_the_real_surgical_branch_is_contained_before_it_spends"),
]

# The regression this workstream actually shipped and had to be told about: containment
# that runs at the APPLY site instead of ahead of the provider call. It still leaves the
# bible unmutated, so only an assertion about SPENDING can catch it — which is why it is
# expressed as a two-part break rather than a single-line one.
TWO_PART = (
    "containment moved to the apply site — pays for a patch it always discards",
    STATIC,
    [("                                          and _cl_wants_semantic and _cl_semantic_source is not None):",
      "                                          and False):"),
     ("                                                    ctx.canonical_facts = _bible2\n",
      "                                                    if not (_cl_wants_semantic and _cl_semantic_source is not None):\n"
      "                                                        ctx.canonical_facts = _bible2\n")],
    T_PROD, "test_the_real_surgical_branch_is_contained_before_it_spends",
)


def _run(test_file, test_name):
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(WT / test_file), "-q", "--no-header",
         "-p", "no:warnings", "--tb=no", "-k", test_name.split("[")[0]],
        cwd=str(WT), capture_output=True, text=True)


def _apply(path, pairs):
    original = path.read_text(encoding="utf-8")
    mutated = original
    for find, repl in pairs:
        if mutated.count(find) != 1:
            return original, None
        mutated = mutated.replace(find, repl, 1)
    path.write_text(mutated, encoding="utf-8")
    return original, mutated


def main() -> int:
    cases = [(lbl, f, [(a, b)], tf, tn) for lbl, f, a, b, tf, tn in BREAKS]
    lbl, f, pairs, tf, tn = TWO_PART
    cases.append((lbl, f, pairs, tf, tn))

    killed = survived = missed = 0
    for label, path, pairs, test_file, test_name in cases:
        original, mutated = _apply(path, pairs)
        if mutated is None:
            print(f"PATTERN-MISS  {label}\n              anchor no longer matches {path.name} "
                  f"— the control was NEVER exercised; check the source before assuming drift")
            missed += 1
            continue
        try:
            res = _run(test_file, test_name)
        finally:
            path.write_text(original, encoding="utf-8")
        if res.returncode != 0:
            killed += 1
            print(f"KILLED        {label}")
        else:
            survived += 1
            print(f"SURVIVED      {label}\n              {test_name} passed against a broken "
                  f"control — that test is not holding this property")

    total = len(cases)
    print(f"\n{killed}/{total} killed, {survived} survived, {missed} pattern-miss")
    return 0 if killed == total else 1


if __name__ == "__main__":
    sys.exit(main())
