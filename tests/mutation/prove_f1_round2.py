#!/usr/bin/env python3
"""Mutation proof for the F1 re-audit (2026-08-15 round 2) controls.

Companion to prove_stage1.py. Run: python3 tests/mutation/prove_f1_round2.py
Override the checkout with PROVE_WT=/path/to/worktree.

Each entry breaks ONE control and names the test that must go red. A control whose
test still passes when the control is removed is not a control.
"""
import pathlib
import re
import shutil
import subprocess
import sys

import os
WT = pathlib.Path(os.environ.get(
    "PROVE_WT", str(pathlib.Path(__file__).resolve().parents[2])))
STATIC = WT / "python/orchestrator/static.py"
NAPI = WT / "python/narration_api.py"
ADAPTER = WT / "python/canon_lite_l3_adapter.py"
SEAM_T = "tests/python/test_narration_generation_leak_seam.py"
STAGE1_T = "tests/python/test_canon_lite_l3_assist_stage1.py"
ADAPTER_T = "tests/python/test_canon_lite_l3_adapter.py"

BREAKS = [
    # The shadow protection is TWO guards that cover for each other, so each is only
    # observable through a test that bypasses the other. The direct `_write_chapter`
    # unit test passes `canon=` itself, bypassing the call-site gate, and so sees the
    # function-level guard alone (mutant 1). The end-to-end test goes through the real
    # call site, where both apply — so only removing BOTH changes anything (mutant 1b).
    ("1. function-level guard removed (seen via the direct _write_chapter test)",
     [(STATIC, r"    _scrub_canon = canon if canon_text else None",
       "    _scrub_canon = canon")],
     STAGE1_T, "test_shadow_mode_never_scrubs_a_marker_out_of_user_visible_output"),

    ("1b. BOTH shadow guards removed (seen via real narrate_chapters end-to-end)",
     [(STATIC, r"    _scrub_canon = canon if canon_text else None",
       "    _scrub_canon = canon"),
      (STATIC, r"                canon=\(_cl_canon if _cl_assist else None\),",
       "                canon=_cl_canon,")],
     STAGE1_T, "test_shadow_narrate_chapters_end_to_end_never_rewrites_the_book"),

    ("2. legacy canonical hash silently redefined to the generation one",
     [(STATIC,
     r'                _cl_canonical_prompt_sha = _cl\.sha256_hex\(\n'
     r'                    _cl\.render_canon\(_cl_canon\)\.encode\("utf-8"\)\)',
     '                _cl_canonical_prompt_sha = _cl_gen_prompt_sha')],
     STAGE1_T, "test_the_legacy_canonical_prompt_hash_still_means_render_canon"),

    ("3. counter aggregation dropped (final seam only, the old bug)",
     [(NAPI,
     r'    result\["f1_scrub_operations_count"\] = \(\n'
     r'        int\(result\.get\("f1_generation_markers_removed"\) or 0\)\n'
     r'        \+ int\(result\.get\("f1_l3_candidate_markers_removed"\) or 0\)\n'
     r'        \+ _removed_count\[0\]\)',
     '    result["f1_scrub_operations_count"] = _removed_count[0]')],
     SEAM_T, "test_removed_count_aggregates_generation_and_l3_removals_not_just_this_seam"),

    ("3b. L3 session stops accumulating its scrub count",
     [(ADAPTER,
     r"                self\._markers_scrubbed \+= _n",
     "                pass")],
     ADAPTER_T, "test_repair_provider_accumulates_its_scrub_count_on_the_session"),

    ("4. verifier hash overwritten with the post-scrub hash again",
     [(NAPI,
     r'            _l3_telemetry\["delivered_manuscript_sha256"\] = _cl\.sha256_hex\(\n'
     r'                \(result\.get\(_manuscript_key\) or ""\)\.encode\("utf-8"\)\)',
     '            _l3_telemetry["manuscript_sha256"] = _cl.sha256_hex(\n'
     '                (result.get(_manuscript_key) or "").encode("utf-8"))')],
     SEAM_T, "test_a_late_change_never_overwrites_the_verifier_hash"),

    ("4b. outcome left `resolved` beside an UNPROVED binding",
     [(NAPI,
     r'            _l3_telemetry\["outcome"\] = L3_OUTCOME_UNRESOLVED',
     '            pass')],
     SEAM_T, "test_a_late_change_marks_the_outcome_unresolved_not_resolved"),

    ("4c. delivery no longer blocked on a late-mutation invariant failure",
     [(NAPI,
     r"            _invariant_ok = False",
     "            _invariant_ok = True")],
     SEAM_T, "test_a_late_change_blocks_delivery_even_though_the_rescan_is_clean"),

    ("6. generation subtotal counts undelivered (failed-worker) chapters again",
     [(STATIC,
     r"        for r in raw if r\.get\(\"ok\"\) and r\.get\(\"output\"\)\)",
     "        for r in raw)")],
     STAGE1_T, "test_the_generation_subtotal_counts_only_delivered_chapters"),

    ("5. proof check narrowed back to the assembled book only",
     [(NAPI,
     r"    if _delivery_representations\(result, _cl2\) != _delivery_before:",
     "    if (result.get(_manuscript_key), ()) != (_delivery_before[0], ()):")],
     SEAM_T, "test_a_chapter_row_change_alone_also_invalidates_the_proof"),
]


def _pytest(test_file, test_name):
    return subprocess.run(
        [sys.executable, "-m", "pytest", f"{test_file}::{test_name}",
         "-q", "-p", "no:cacheprovider", "--no-header"],
        cwd=WT, capture_output=True, text=True)


def _classify(proc):
    """What pytest ACTUALLY said. Returns ("pass"|"fail", None) or (None, reason).

    🔴 THE ONE RULE THIS FILE EXISTS TO ENFORCE ON ITSELF. An earlier version scored a
       mutant as KILLED on `returncode != 0` alone — which is true for a missing test
       file, a mistyped node id, a collection error, or a usage error just as much as
       for a real failure. Pointing every test path at a nonexistent file still printed
       "9/9 mutants killed". A proof instrument that cannot fail proves nothing, and
       this one was reporting on the very changes it was supposed to be evidence for.

       pytest's exit codes: 0 = all passed, 1 = tests were run and some FAILED,
       2 = interrupted, 3 = internal error, 4 = usage error, 5 = no tests collected.
       Only 0 and 1 are verdicts; everything else is the harness being broken, and is
       reported as such rather than silently counted as a kill.
    """
    out = proc.stdout + proc.stderr
    if proc.returncode == 5 or "no tests ran" in out:
        return None, "no tests collected (missing file or wrong node id?)"
    if proc.returncode == 4:
        return None, "pytest usage error"
    if proc.returncode in (2, 3):
        return None, f"pytest aborted (exit {proc.returncode})"
    if "error" in out.lower() and re.search(r"\b\d+ errors?\b", out):
        return None, "collection/setup error, not a test failure"
    if proc.returncode == 1:
        if not re.search(r"\b\d+ failed\b", out):
            return None, "exit 1 with no 'N failed' summary"
        return "fail", None
    if proc.returncode == 0:
        if not re.search(r"\b\d+ passed\b", out):
            return None, "exit 0 with no 'N passed' summary"
        return "pass", None
    return None, f"unrecognized pytest exit {proc.returncode}"


def run(label, edits, test_file, test_name):
    # 1. The node must EXIST and PASS before we mutate anything. Without this, a
    #    typo'd node id is indistinguishable from a control that works.
    verdict, why = _classify(_pytest(test_file, test_name))
    if verdict != "pass":
        print(f"  !! BASELINE NOT GREEN ({why or verdict}) — {label}")
        return False

    backups = {}
    try:
        for path, pattern, repl in edits:
            if path not in backups:
                backups[path] = path.read_text(encoding="utf-8")
            mutated, n = re.subn(pattern, repl, path.read_text(encoding="utf-8"), count=1)
            if n != 1:
                print(f"  !! PATTERN NOT FOUND — {label}")
                return False
            path.write_text(mutated, encoding="utf-8")

        # 2. And with the control removed it must FAIL — as a test failure, not as
        #    any of the ways pytest can decline to run at all.
        verdict, why = _classify(_pytest(test_file, test_name))
        if verdict is None:
            print(f"  !! INCONCLUSIVE ({why}) — {label}")
            return False
        killed = (verdict == "fail")
        print(f"  {'KILLED ' if killed else 'SURVIVED'} — {label}")
        return killed
    finally:
        # 3. And the tree must be byte-identical afterwards. A harness that leaves a
        #    mutation behind poisons every later run, including the full suite.
        for path, original in backups.items():
            path.write_text(original, encoding="utf-8")
            if path.read_text(encoding="utf-8") != original:
                print(f"  !! RESTORE FAILED for {path} — tree is now dirty")
                raise SystemExit(2)


print("Mutation proof: F1 re-audit round 2 controls\n")
results = [run(*b) for b in BREAKS]
print(f"\n{sum(results)}/{len(results)} mutants killed")
sys.exit(0 if all(results) else 1)
