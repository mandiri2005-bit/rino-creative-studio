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
CANON = WT / "python/canon_lite.py"
METER = WT / "python/canon_lite_qc_meter.py"
L1_T = "tests/python/test_canon_lite_l1.py"
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

    # ── the tenant allowlist: the difference between a cohort and the fleet ──
    ("the tenant gate is removed from static.py — every job takes the repair path",
     STATIC,
     r'    if _cl_mode == "assist":\n'
     r'        # 🔴 ASSIST IS PER-TENANT\. The variable is process-wide, so without this a\n'
     r'        #    flip to `assist` puts every job this worker touches on the repair path\n'
     r'        #    at once — queued ones included, which the drain gate never covered\.\n'
     r'        #    An empty allowlist admits nobody, so the flag alone is a no-op\.\n'
     r'        _cl_tid = str\(tenant_id or ""\)\.strip\(\)\n'
     r'        _cl_allow = \{t\.strip\(\) for t in str\(\n'
     r'            os\.environ\.get\("NARASI_CANON_LITE_ASSIST_TENANTS", ""\) or ""\)\.split\(","\)\n'
     r'            if t\.strip\(\)\}\n'
     r'        if not _cl_tid or _cl_tid not in _cl_allow:\n'
     r'            _cl_mode = "off"\n',
     "",
     f"{STAGE1}::test_every_other_tenant_stays_on_the_legacy_path[t-someone-else]"),
    ("an empty allowlist admits everybody instead of nobody",
     STATIC,
     r'        if not _cl_tid or _cl_tid not in _cl_allow:',
     '        if _cl_allow and _cl_tid not in _cl_allow:',
     f"{STAGE1}::test_a_missing_allowlist_puts_even_the_canary_on_the_legacy_path"),
    ("the QC gate reads the DEPLOYMENT's mode — every tenant arms the wave",
     RUNNER,
     r"    if _cl\.resolve_effective_mode\(environ, tenant_id=tenant_id\) == _cl\.MODE_OFF:",
     "    if _cl.resolve_mode(environ) == _cl.MODE_OFF:",
     f"{ADAPTER_T}::test_the_qc_gate_decides_on_the_JOBS_mode_not_the_deployments[t-canary-t-other-False]"),
    ("the tenant is not threaded into the QC gate — the canary loses its wave",
     RUNNER,
     r"    if not metered_wave_permitted\(environ, tenant_id=tenant_id\):",
     "    if not metered_wave_permitted(environ):",
     f"{ADAPTER_T}::test_a_canary_passes_the_real_qc_gate_and_reaches_the_provider"),
    ("the seam stops threading the tenant to the wave",
     NAPI,
     r"            job_uuid=job_uuid, job_external_id=job_external_id,\n"
     r"            tenant_id=tenant_id,\n",
     "            job_uuid=job_uuid, job_external_id=job_external_id,\n",
     f"{ADAPTER_T}::test_the_gate_reads_the_effective_mode_not_the_global_one"),
    ("Phase A is narrowed to a tenant — an armed deployment reads OFF",
     METER,
     r"    effective_mode = canon_lite\.resolve_mode\(environ\)",
     "    effective_mode = canon_lite.resolve_effective_mode(environ)",
     f"{ADAPTER_T}::test_operator_phase_a_reads_the_global_mode_not_a_tenants"),
    ("the canonical resolver stops gating assist",
     CANON,
     r'    tid = str\(tenant_id or ""\)\.strip\(\)\n'
     r'    src = os\.environ if env is None else env\n'
     r'    return MODE_ASSIST if tid and tid in assist_tenants\(src\) else MODE_OFF',
     "    return MODE_ASSIST",
     f"{L1_T}::test_all_three_mode_gates_agree_including_the_tenant_allowlist[assist-t-canary-t-other-off]"),
    ("narration_api's copy stops gating assist — the seam disagrees with the worker",
     NAPI,
     r'    return "assist" if tid and tid in allow else "off"',
     '    return "assist"',
     f"{L1_T}::test_all_three_mode_gates_agree_including_the_tenant_allowlist[assist-t-canary-t-other-off]"),
    ("the allowlist gate leaks onto shadow as well",
     CANON,
     r"    if mode != MODE_ASSIST:\n        return mode",
     "    if mode not in (MODE_ASSIST, MODE_SHADOW):\n        return mode",
     f"{L1_T}::test_all_three_mode_gates_agree_including_the_tenant_allowlist[shadow-None-t-other-shadow]"),

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

BACKUPS = {p: p.read_bytes() for p in (NAPI, STATIC, ADAPTER, RUNNER, CANON, METER)}
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
