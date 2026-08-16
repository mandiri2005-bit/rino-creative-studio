#!/usr/bin/env python3
"""Prove the Stage-2 controls are load-bearing: break each property, expect a FAIL.

Same contract as prove_stage1.py. Everything is restored in `finally`.
Run it from anywhere:  python3 tests/mutation/prove_stage2.py
Override the checkout with PROVE_WT=/path/to/worktree.
"""
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _harness import IsolationError, isolated_run, restore_guard  # noqa: E402

# The repo this harness belongs to, derived from its own location. It used to be a
# hard-coded scratchpad path, which meant the proof lived outside the branch it was
# proving and died with the session. PROVE_WT still overrides, for running the
# harness against a different checkout.
WT = pathlib.Path(os.environ.get(
    "PROVE_WT", str(pathlib.Path(__file__).resolve().parents[2])))
REPAIR = WT / "python/canon_lite_l3_repair.py"
L2 = WT / "python/canon_lite_l2.py"
TEST = "tests/python/test_canon_lite_l3_repair_stage2.py"

BREAKS = [
    # ── the three acceptance conditions, one mutation each ───────────────
    ("condition 1 dropped — an ineffective candidate is banked as a repair",
     REPAIR,
     r"            if any\(v for v in staged_violations\n"
     r"                   if v\[2\] == index and v\[0\] in codes\):\n"
     r"                reason = REASON_INEFFECTIVE\n"
     r"                continue\n",
     "",
     "test_a_valid_but_ineffective_candidate_is_rejected"),
    ("condition 2 dropped — a repair may break something else",
     REPAIR,
     r"            if staged_violations - current_violations:\n"
     r"                reason = REASON_REGRESSED\n"
     r"                continue\n",
     "",
     "test_a_candidate_that_introduces_a_new_violation_is_rejected"),
    ("condition 3 dropped — a repair may destroy coverage instead of the defect",
     REPAIR,
     r"            if _coverage_regressed\(current_report, staged_report\):\n"
     r"                reason = REASON_COVERAGE_REGRESSED\n"
     r"                continue\n",
     "",
     "test_a_candidate_that_regresses_coverage_is_rejected"),
    ("coverage regression measured on violations only, not measurability",
     REPAIR,
     r"        if was\.coverage_state in _l2\._PREDICATE_MEASURED \\\n"
     r"                and result\.coverage_state not in _l2\._PREDICATE_MEASURED:\n"
     r"            return True\n",
     "",
     # Aimed at the UNIT case, not the integration one: in the integration test a
     # TIMEOUT also drops checked_units, so the surviving clause catches it and the
     # mutation reports a false pass. Two clauses, two cases, or neither is proven.
     "test_coverage_regression_detects_both_of_its_two_shapes"),

    # ── extractor identity: all four fields ──────────────────────────────
    ("extractor identity reduced to the content hash — index ignored",
     REPAIR,
     r"    if fresh\.chapter_index != chapter_index:\n        return False\n",
     "",
     "test_claims_that_do_not_bind_the_candidate_are_refused[chapter_index-0]"),
    ("extractor identity reduced — chapter id ignored",
     REPAIR,
     r"    if fresh\.chapter_id != chapter_id:\n        return False\n",
     "",
     "test_claims_that_do_not_bind_the_candidate_are_refused[chapter_id-ch1]"),
    ("extractor identity reduced — content hash ignored",
     REPAIR,
     r"    if fresh\.content_sha256 != _cl\.sha256_hex\(candidate\):\n        return False\n",
     "",
     "test_claims_that_do_not_bind_the_candidate_are_refused"
     "[content_sha256-dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd]"),
    ("extractor identity reduced — canon ignored",
     REPAIR,
     r"    return fresh\.canon_sha256 == expected_canon",
     "    return True",
     "test_claims_that_do_not_bind_the_candidate_are_refused"
     "[canon_sha256-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee]"),

    # ── the iteration ceiling ────────────────────────────────────────────
    ("iteration ceiling raised to three",
     L2,
     r"MAX_REPAIR_ROUNDS = 2",
     "MAX_REPAIR_ROUNDS = 3",
     "test_a_third_attempt_is_impossible"),
    ("ceiling duplicated instead of imported — the two can now drift",
     REPAIR,
     r"MAX_REPAIR_ITERATIONS = _l2\.MAX_REPAIR_ROUNDS",
     "MAX_REPAIR_ITERATIONS = 2",
     "test_the_report_ceiling_and_the_engine_ceiling_are_one_number"),

    # ── the minimal-edit boundary ────────────────────────────────────────
    ("guard boundary off by one — exactly at the limit now rejects",
     REPAIR,
     r"    return int\(ratio_ppm\) > int\(max_ppm\)",
     "    return int(ratio_ppm) >= int(max_ppm)",
     "test_the_guard_boundary_is_strictly_greater"),
    ("guard bypassed entirely — a rewrite lands as a repair",
     REPAIR,
     r"            if exceeds_edit_guard\(ratio, max_edit_ratio_ppm\):\n"
     r"                # Rejected WHOLE\. Trimming toward the boundary would leave the\n"
     r"                # reader with something that is neither the chapter nor the repair\.\n"
     r"                reason = REASON_GUARD_REJECTED\n"
     r"                continue\n",
     "",
     "test_a_candidate_exactly_at_the_boundary_is_accepted_and_one_over_is_not"),
    ("ratio measured in BYTES — the guard tightens for non-ASCII prose",
     REPAIR,
     r"        a, b = _nfc\(before\), _nfc\(after\)",
     "        a, b = before.decode('latin-1'), after.decode('latin-1')",
     "test_the_ratio_is_measured_in_nfc_characters_not_bytes"),

    # ── structure is never repaired ──────────────────────────────────────
    ("structure violations become repairable",
     REPAIR,
     r"REPAIRABLE_PREDICATES = frozenset\(_l2\.SEMANTIC_PREDICATES\)",
     "REPAIRABLE_PREDICATES = frozenset(_l2.ALL_PREDICATES)",
     "test_a_title_mismatch_is_never_targeted_for_repair"),
    ("document-global violations get routed to a chapter anyway",
     REPAIR,
     r"            if violation\.chapter_index < 0:\n                continue\n",
     "",
     "test_a_document_global_semantic_violation_is_not_routed_to_a_chapter"),

    # ── enforce stays out ────────────────────────────────────────────────
    ("enforce loses its specific refusal and falls through to the enum check",
     REPAIR,
     r'    if mode == "enforce":\n'
     r'        raise _schema_error\(\n'
     r'            "mode=enforce: L3-ENFORCE is a separate deferred project; "\n'
     r'            "this engine implements assist only"\)\n',
     "",
     "test_enforce_mode_is_refused"),
    ("the schema stops policing which mode may repair",
     L2,
     r'        if self\.repair_rounds != 0 and self\.mode != "assist":\n'
     r'            raise _schema_error\(\n'
     r'                f"repair_rounds: only assist may repair; \{self\.mode!r\} may not"\)\n',
     "",
     "test_only_assist_may_report_repair_rounds[enforce]"),
    ("the schema deny-lists shadow instead of allow-listing assist — enforce slips through",
     L2,
     r'        if self\.repair_rounds != 0 and self\.mode != "assist":',
     '        if self.repair_rounds != 0 and self.mode == "shadow":',
     "test_only_assist_may_report_repair_rounds[enforce]"),

    # ── the state model: the book moves while it is repaired ─────────────
    ("targets frozen at the first report — a settled chapter is repaired anyway",
     REPAIR,
     r'        codes = repairable_targets\(current_report\)\.get\(index, \(\)\)',
     "        codes = repairable_targets(_l2.build_report("
     "snapshot, canon, mode=mode, result=result, "
     "claims_by_index=dict(claims_by_index or {}))).get(index, ())",
     "test_a_repair_that_settles_a_later_chapter_stops_it_being_repaired"),
    ("state promoted piecemeal — the report lags the blocks",
     REPAIR,
     r"            \(current_blocks, current_claims, current_snapshot,\n"
     r"             current_result, current_report\) = promoted\n",
     "            current_blocks = promoted[0]\n",
     "test_a_repair_that_settles_a_later_chapter_stops_it_being_repaired"),
    ("new violations judged against the ORIGINAL report, not the current one",
     REPAIR,
     r"            if staged_violations - current_violations:",
     "            if staged_violations - _violation_set(_l2.build_report("
     "snapshot, canon, mode=mode, result=result, "
     "claims_by_index=dict(claims_by_index or {}))):",
     "test_a_repair_may_not_reintroduce_what_an_earlier_repair_removed"),
    ("coverage regression judged against the ORIGINAL report — earned coverage is spendable",
     REPAIR,
     r"            if _coverage_regressed\(current_report, staged_report\):",
     "            if _coverage_regressed(_l2.build_report("
     "snapshot, canon, mode=mode, result=result, "
     "claims_by_index=dict(claims_by_index or {})), staged_report):",
     "test_a_second_repair_may_not_spend_coverage_the_first_recovered"),

    # ── byte discipline ──────────────────────────────────────────────────
    # NOTE: "a rejected candidate reaches the manuscript" is held by TWO
    # independent mechanisms — `blocks[index]` is only written on acceptance, and
    # `changed` only counts accepted chapters — so no single edit breaks it. What
    # IS single-edit reachable is the bookkeeping lying about it, and that is what
    # the record's own invariant exists for.
    ("a rejected chapter is recorded as an accepted repair",
     REPAIR,
     r"            last_reason=reason, accepted=accepted_bytes is not None,",
     "            last_reason=reason, accepted=True,",
     "test_a_valid_but_ineffective_candidate_is_rejected"),
    ("the final report is built from the pre-repair state",
     REPAIR,
     r"    final = _l2\.build_report\(\n        current_snapshot, canon, mode=mode, result=current_result,\n        claims_by_index=current_claims,",
     "    final = _l2.build_report(\n        snapshot, canon, mode=mode, result=result,\n        claims_by_index=dict(claims_by_index or {}),",
     "test_a_genuine_targeted_repair_is_accepted_and_rebinds_the_manuscript"),
    ("a provider fault is retried instead of ending the chapter",
     REPAIR,
     r"                reason = REASON_PROVIDER_FAILED\n                break\n\n            candidate",
     "                reason = REASON_PROVIDER_FAILED\n                continue\n\n            candidate",
     "test_a_provider_that_raises_leaves_the_chapter_untouched"),
    ("verify_applied compares against the pre-repair manuscript",
     REPAIR,
     r"            if _cl\.sha256_hex\(text\.encode\(\"utf-8\"\)\) == run\.manuscript_sha256_after",
     '            if _cl.sha256_hex(text.encode("utf-8")) == run.manuscript_sha256_before',
     "test_verify_applied_is_what_proves_the_repair_was_delivered"),
]

SOURCES = (REPAIR, L2)
ok = True
try:
    # 🔴 A SAME-SIZE MUTATION IS INVISIBLE TO PYTHON'S BYTECODE CACHE: invalidation
    #    keys on (mtime_seconds, size), and `= 2` -> `= 3` changes neither when the
    #    rewrite lands inside one tick, so a stale `.pyc` is reused and the mutation
    #    "survives" without ever running. This file used to answer that by deleting
    #    every `__pycache__` in the checkout after each mutation — a second mechanism
    #    that hid the gaps in the first. It is now ONE mechanism, owned by `_harness`:
    #    each subprocess gets its own empty cache prefix, and `restore_guard` restores
    #    bytes, mode and mtime_ns and VERIFIES them (the old `finally` here restored
    #    without ever checking that the restore worked).
    with restore_guard(*SOURCES) as _originals:
        pristine = dict(zip(SOURCES, _originals))
        for label, target, pat, rep, test in BREAKS:
            for p, data in pristine.items():
                p.write_bytes(data)
            broken, n = re.subn(pat, rep, target.read_text())
            if n != 1:
                print(f"!! PATTERN-MISS ({n}) for: {label}")
                ok = False
                continue
            target.write_text(broken)
            with isolated_run() as env:
                r = subprocess.run(
                    [sys.executable, "-m", "pytest", f"{TEST}::{test}", "-q", "--tb=no",
                     "-p", "no:cacheprovider", "-p", "no:warnings"],
                    cwd=str(WT), capture_output=True, text=True, env=env)
            caught = r.returncode != 0
            print(f"{'   caught  ' if caught else '!! SURVIVED'}  {label}")
            ok = ok and caught
except IsolationError as exc:
    print(f"!! {exc}")
    ok = False
else:
    print("restored")
sys.exit(0 if ok else 1)
