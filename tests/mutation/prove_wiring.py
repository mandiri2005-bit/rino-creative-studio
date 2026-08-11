#!/usr/bin/env python3
"""Prove the L3-ASSIST delivery-seam controls are load-bearing.

Same contract as prove_stage1.py / prove_stage2.py: break one property, run the
single control that should catch it, expect a FAILURE. Everything restored in
`finally`.

    python3 tests/mutation/prove_wiring.py
Override the checkout with PROVE_WT=/path/to/worktree.
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys

WT = pathlib.Path(os.environ.get(
    "PROVE_WT", str(pathlib.Path(__file__).resolve().parents[2])))
NAPI = WT / "python/narration_api.py"
STATIC = WT / "python/orchestrator/static.py"
ADAPTER = WT / "python/canon_lite_l3_adapter.py"
RUNNER = WT / "python/canon_lite_qc_runner.py"
ADAPTER_T = "tests/python/test_canon_lite_l3_adapter.py"
WIRING = "tests/python/test_canon_lite_l3_assist_wiring.py"
STAGE1 = "tests/python/test_canon_lite_l3_assist_stage1.py"

BREAKS = [
    # ── the canon has to reach the seam on the real path ─────────────────
    ("canon forwarded for shadow only — assist becomes a silent no-op",
     STATIC,
     r'           if _cl_mode in \("shadow", "assist"\) else \{\}\),',
     '           if _cl_mode == "shadow" else {}),',
     f"{STAGE1}::test_the_real_job_carries_the_canon_to_the_downstream_seam[assist]"),

    # ── the storage census ───────────────────────────────────────────────
    ("census narrowed to repaired chapters — a stale record is never noticed",
     NAPI,
     r"    for i in range\(n\):\n        rec = by_index\[i\]",
     "    for i in sorted({c.chapter_index for c in run.chapters if c.accepted}):\n"
     "        rec = by_index[i]",
     f"{WIRING}::test_a_polished_book_that_no_longer_matches_the_records_refuses_the_write"),
    ("body framing rule removed entirely",
     NAPI,
     r"        if _l3_ws_runs\(content\) != _l3_ws_runs\(body\):\n            return False\n",
     "",
     f"{WIRING}::test_a_candidate_that_pads_the_body_is_not_written_back"),
    ("the framing guard regains its precondition — off for any body with whitespace",
     NAPI,
     r"        if _l3_ws_runs\(content\) != _l3_ws_runs\(body\):",
     "        if content == content.strip() and body != body.strip():",
     f"{WIRING}::test_a_body_with_trailing_space_still_rejects_a_leading_blank_line"),
    ("only the trailing run is compared — a leading blank line slips through",
     NAPI,
     r"        if _l3_ws_runs\(content\) != _l3_ws_runs\(body\):",
     "        if _l3_ws_runs(content)[1] != _l3_ws_runs(body)[1]:",
     f"{WIRING}::test_a_candidate_that_pads_the_body_is_not_written_back"),
    ("only the leading run is compared — an appended newline slips through",
     NAPI,
     r"        if _l3_ws_runs\(content\) != _l3_ws_runs\(body\):",
     "        if _l3_ws_runs(content)[0] != _l3_ws_runs(body)[0]:",
     f"{WIRING}::test_a_trailing_space_body_still_rejects_an_appended_newline"),
    ("a failed sync no longer blocks the book substitution",
     NAPI,
     r"        if _after_snap is None or not _l3_sync_chapter_records\(\n"
     r"                result, run, snapshot, _after_snap\):",
     "        if False:",
     f"{WIRING}::test_a_polished_book_that_no_longer_matches_the_records_refuses_the_write"),

    # ── every path records a bounded outcome ─────────────────────────────
    ("the unsyncable path exits without recording anything",
     NAPI,
     r"            return _l3_record_outcome\(result, outcome=L3_OUTCOME_UNRESOLVED,\n"
     r"                                      stage=\"unsyncable\", run=run\)",
     '            return {"l3_status": "unsyncable"}',
     f"{WIRING}::test_every_assist_path_records_a_bounded_outcome[unsyncable]"),
    ("a repair that never concluded is recorded as clean",
     NAPI,
     r"        return _l3_record_outcome\(result, outcome=L3_OUTCOME_UNCHECKED,\n"
     r"                                  stage=\"repair_error\"\)",
     "        return _l3_record_outcome(result, outcome=L3_OUTCOME_CLEAN,\n"
     '                                  stage="repair_error")',
     f"{WIRING}::test_a_repair_pass_that_never_concluded_is_unchecked_not_clean"),
    ("an unknown L2 status defaults to clean instead of unchecked",
     NAPI,
     r"    return L3_OUTCOME_UNCHECKED\n\n\ndef _l3_record_outcome",
     "    return L3_OUTCOME_CLEAN\n\n\ndef _l3_record_outcome",
     f"{WIRING}::test_the_outcome_vocabulary_is_three_values_and_defaults_closed"),

    # ── the delivered bytes are the validated bytes ──────────────────────
    ("the chapter rows are left holding the pre-repair prose",
     NAPI,
     r"    for rec, body in staged:\n        rec\[\"content\"\] = body\n    return True",
     "    return True",
     f"{WIRING}::test_the_stored_row_and_the_delivered_payload_are_the_same_bytes"),
    ("the assist outcome never reaches the durable payload",
     NAPI,
     r"    _cl_l3 = result\.get\(\"canon_lite_l3\"\)\n    if _cl_l3:\n"
     r"        payload\[\"canon_lite_l3\"\] = _cl_l3\n",
     "",
     f"{WIRING}::test_the_stored_row_and_the_delivered_payload_are_the_same_bytes"),
    ("non-assist modes fall into the seam",
     NAPI,
     r'    if mode != "assist":\n        return None',
     '    if mode not in ("assist", "shadow", "off", "enforce"):\n        return None',
     f"{WIRING}::test_non_assist_modes_do_not_enter_the_seam[shadow]"),

    # ── the adapter session: one per job, budgeted, honestly bound ───────
    ("a session may mint its own provider — the second wave, silently",
     ADAPTER,
     r'        if metered_provider is None or not callable\(metered_provider\):\n'
     r'            # Refused rather than defaulted to a fresh one: a session that mints its\n'
     r'            # own provider is the second wave this module exists to prevent\.\n'
     r"            raise ValueError\(\"metered_provider is required and must be the job's own\"\)\n",
     "",
     f"{ADAPTER_T}::test_a_session_refuses_to_mint_its_own_provider"),
    ("the runner stops handing its metered session out — reuse becomes a rebuild",
     RUNNER,
     r"    if on_session is not None:\n        on_session\(metered\)\n",
     "",
     f"{ADAPTER_T}::test_the_runner_hands_its_metered_session_out_exactly_once"),
    ("the session budget is never spent — the ceiling stops existing",
     ADAPTER,
     r"        self\._calls \+= 1",
     "        pass",
     f"{ADAPTER_T}::test_the_session_budget_is_independent_of_the_engine_bound"),
    ("an exhausted budget faults the chapter instead of declining",
     ADAPTER,
     r"        except SessionBudgetExhausted:\n"
     r'            log\.warning\("canon lite l3: session budget exhausted; repair declined"\)\n'
     r"            return None\n",
     "        except SessionBudgetExhausted:\n            raise\n",
     f"{ADAPTER_T}::test_an_exhausted_budget_declines_a_repair_rather_than_faulting"),
    ("the budget ceiling is accepted unvalidated",
     ADAPTER,
     r"        if isinstance\(max_calls, bool\) or not isinstance\(max_calls, int\) \\\n"
     r"                or not \(1 <= max_calls <= MAX_SESSION_CALLS\):\n"
     r'            raise ValueError\(f"max_calls: expected int in 1\.\.\{MAX_SESSION_CALLS\}"\)\n',
     "",
     f"{ADAPTER_T}::test_the_budget_ceiling_is_bounded_at_construction[0]"),
    ("the content hash is stamped too — the binding stops being evidence",
     ADAPTER,
     r"        return _dc_replace\(artifact, chapter_index=chapter_index,\n"
     r"                           chapter_id=chapter_id\)",
     "        return _dc_replace(artifact, chapter_index=chapter_index,\n"
     "                           chapter_id=chapter_id,\n"
     "                           content_sha256=_cl.sha256_hex(block_bytes + b' '))",
     f"{ADAPTER_T}::test_index_and_id_are_stamped_but_the_hashes_are_measured"),
    ("a multi-block candidate is extracted anyway",
     ADAPTER,
     r"        if mini is None or mini\.chapter_count != 1:",
     "        if mini is None:",
     f"{ADAPTER_T}::test_a_candidate_that_is_not_one_block_is_refused"),

    # ── mode isolation ───────────────────────────────────────────────────
    ("the mode gate moves below the imports — non-assist loads the paid path",
     NAPI,
     r'    if mode != "assist":\n        return None\n\n    import canon_lite_l2 as _cl2\n'
     r'    import canon_lite_l3_repair as _cl3\n',
     "    import canon_lite_l2 as _cl2\n    import canon_lite_l3_repair as _cl3\n"
     '    if mode != "assist":\n        return None\n',
     f"{ADAPTER_T}::test_a_non_assist_job_imports_no_l3_module_and_spends_nothing[shadow]"),
]

BACKUPS = {p: p.read_bytes() for p in (NAPI, STATIC, ADAPTER, RUNNER)}
ok = True
try:
    for label, target, pat, rep, test in BREAKS:
        for p, data in BACKUPS.items():
            p.write_bytes(data)
        broken, n = re.subn(pat, rep, target.read_text())
        if n != 1:
            print(f"!! PATTERN-MISS ({n}) for: {label}")
            ok = False
            continue
        target.write_text(broken)
        for cache in WT.rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        r = subprocess.run(
            [sys.executable, "-m", "pytest", test, "-q", "--tb=no",
             "-p", "no:cacheprovider", "-p", "no:warnings"],
            cwd=str(WT), capture_output=True, text=True)
        caught = r.returncode != 0
        print(f"{'   caught  ' if caught else '!! SURVIVED'}  {label}")
        ok = ok and caught
finally:
    for p, data in BACKUPS.items():
        p.write_bytes(data)
    print("restored")
sys.exit(0 if ok else 1)
