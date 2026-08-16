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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _harness import IsolationError, isolated_run, restore_guard  # noqa: E402

WT = pathlib.Path(os.environ.get(
    "PROVE_WT", str(pathlib.Path(__file__).resolve().parents[2])))
CORE = WT / "python/orchestrator/core.py"
STATIC = WT / "python/orchestrator/static.py"
NAPI = WT / "python/narration_api.py"
RUNNER = WT / "python/canon_lite_qc_runner.py"
L2 = WT / "python/canon_lite_l2.py"
EXT = WT / "python/canon_lite_extractor.py"
QCP = WT / "python/canon_lite_qc_provider.py"
METER = WT / "python/canon_lite_qc_meter.py"
L3A = WT / "python/canon_lite_l3_adapter.py"

T_TRANSPORT = "tests/python/test_narasi_structured_transport.py"
T_SIDECAR = "tests/python/test_canon_registry_sidecar_threading.py"
T_PROD = "tests/python/test_canon_lite_p0b_production_combination.py"
T_QC = "tests/python/test_canon_lite_qc_provider.py"
T_L2 = "tests/python/test_canon_lite_l2.py"
T_METER = "tests/python/test_platform_qc_meter.py"
T_L3A = "tests/python/test_canon_lite_l3_adapter.py"

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

    # ── 3c. the session's canon renderer ─────────────────────────────────
    # Reverts to the shape live job `98o7l3o8` died on: `_cl` never imported, so
    # `_cl.render_canon` raises NameError while building the session and the seam
    # reports `l3_session_error` / `stage=no_session` instead of naming the fault.
    ("canon renderer never imported — the session build raises NameError",
     NAPI,
     "    import canon_lite as _cl\n    import canon_lite_l2 as _cl2",
     "    import canon_lite_l2 as _cl2",
     "tests/python/test_canon_lite_l3_production_path.py",
     "test_the_production_branch_repairs_and_binds_to_the_delivered_bytes"),

    # ── 3d. server-owned atom addresses and bound provenance ─────────────
    ("atom-table version drops out of the provenance digest",
     L2,
     '        "atom_table_version": ATOM_TABLE_VERSION,',
     '        "atom_table_version": "unversioned",',
     T_L2, "test_atom_table_version_is_part_of_the_digest"),
    ("atom-table binding disabled — payload can name a table the chapter never used",
     L2,
     "    if atom_table_sha != expected_atom_sha:\n"
     "        raise _schema_error(\"atom_table_sha256: does not bind the chapter atom table\",\n"
     "                            code=EXTRACT_REASON_ATOM_TABLE_MISMATCH)",
     "    if False:\n"
     "        raise _schema_error(\"atom_table_sha256: does not bind the chapter atom table\",\n"
     "                            code=EXTRACT_REASON_ATOM_TABLE_MISMATCH)",
     T_L2, "test_atom_table_digest_mismatch_refuses_before_claim_resolution"),
    ("request accepts atom rows built from different chapter bytes",
     EXT,
     "        if self.chapter_atoms != atoms or self.atom_table_sha256 != atom_sha:",
     "        if self.atom_table_sha256 != atom_sha:",
     T_QC, "test_8_20b_request_refuses_an_atom_table_from_different_bytes"),
    ("out-of-range atom indexes accepted instead of refused",
     L2,
     "        if atom_start < 0 or atom_end >= len(atoms):",
     "        if False:",
     T_L2, "test_invalid_atom_addresses_refuse_with_bounded_codes"),
    ("inverted atom range guard disabled",
     L2,
     "        if atom_start > atom_end:",
     "        if False:",
     T_L2, "test_invalid_atom_addresses_refuse_with_bounded_codes"),
    ("model address ignored — every claim silently binds to atom zero",
     L2,
     "        start = atoms[atom_start].byte_start\n        end = atoms[atom_end].byte_end",
     "        start = atoms[0].byte_start\n        end = atoms[0].byte_end",
     T_L2, "test_repeated_text_is_disambiguated_by_address_not_copied_context"),
    ("artifact drops the atom-table digest that produced its evidence offsets",
     L2,
     "        atom_table_sha256=atom_table_sha,",
     "        atom_table_sha256=content_sha,",
     T_L2, "test_a_well_formed_address_derives_original_byte_span_and_sha"),
    ("provider schema re-opens copied quote/context fields",
     QCP,
     '                "required": ["claim_type", "canon_ref", "atom_start", "atom_end"],',
     '                "required": ["claim_type", "canon_ref"],',
     T_QC, "test_8_8b_claim_wire_is_address_only"),
    ("atom wire drops exact whitespace atoms and cannot reconstruct chapter bytes",
     QCP,
     '        "chapter_atoms": [atom.to_wire_obj() for atom in request.chapter_atoms],',
     '        "chapter_atoms": [atom.to_wire_obj() for atom in request.chapter_atoms '
     'if not atom.text.isspace()],',
     T_QC, "test_8_20_chapter_atom_round_trip"),

    # ── 3d.1 retry structure ─────────────────────────────────────────────
    ("attempts 2-3 drop the schema and return to loose output",
     QCP,
     'QC_RESPONSE_FORMAT_SCHEDULE = ("json_schema", "json_schema", "json_schema")',
     'QC_RESPONSE_FORMAT_SCHEDULE = ("json_schema", "none", "none")',
     T_QC, "test_8_8_every_attempt_sends_the_same_closed_json_schema"),
    ("adapter-minted provider code flattened back to generic PROVIDER_FAILURE",
     EXT,
     '                error_code = _provider_error_code(exc) or _l2.COVERAGE_PROVIDER_FAILURE',
     '                error_code = _l2.COVERAGE_PROVIDER_FAILURE',
     T_QC, "test_8_19e_provider_code_survives_without_provider_text"),
    ("bounded retry reason is never threaded to the next request",
     EXT,
     '            retry_reason = _retry_reason(error_code)',
     '            retry_reason = QC_RETRY_REASON_NONE',
     T_QC, "test_8_19f_address_retry_changes_request_bytes_and_becomes_measured"),

    # ── 3d.2 durable meter identity and process-latch observability ──────
    ("meter ignores the explicit physical identity and collides with semantic chapter zero",
     METER,
     '        unit_index = getattr(request, "meter_unit_index", None)\n'
     '        attempt_ordinal = getattr(request, "meter_attempt_ordinal", None)',
     '        unit_index = getattr(request, "chapter_index", None)\n'
     '        attempt_ordinal = getattr(request, "attempt", None)',
     T_METER, "test_meter_uses_explicit_identity_not_semantic_chapter_identity"),
    ("durable replay makes a second physical provider call",
     METER,
     '            if outcome == "replay":',
     '            if False:',
     T_METER, "test_durable_replay_never_makes_a_second_physical_call"),
    ("process-local kill latch goes silent again",
     METER,
     '            if not _process_latch_logged:\n'
     '                log.error("platform_qc_meter process_latch_blocking=1")\n'
     '                _process_latch_logged = True',
     '            if False:\n'
     '                log.error("platform_qc_meter process_latch_blocking=1")\n'
     '                _process_latch_logged = True',
     T_METER, "test_process_latch_logs_once_and_performs_no_external_reads"),
    ("L3 re-extraction falls back into the initial-extraction meter namespace",
     L3A,
     "            meter_unit_index=L3_REEXTRACT_UNIT_BASE + chapter_index,",
     "            meter_unit_index=chapter_index,",
     T_L3A, "test_repair_after_an_already_metered_chapter_never_reresolves_one_attempt_id"),
    ("L3 re-extraction hardcodes attempt one and collides across repair iterations",
     L3A,
     "            meter_attempt_ordinal=attempt)",
     "            meter_attempt_ordinal=1)",
     T_L3A, "test_repair_after_an_already_metered_chapter_never_reresolves_one_attempt_id"),

    # ── 3e. the bounded telemetry that ends the silence ──────────────────
    ("failing extraction goes back to leaving no trace at all",
     EXT,
     '            log.warning("canon lite qc extract: attempt failed "\n'
     '                        "(unit_index=%d attempt_ordinal=%d error_code=%s)",\n'
     '                        index, attempt, error_code)',
     "            pass",
     T_QC, "test_8_19_a_failing_attempt_logs_exactly_one_bounded_line"),

    # ── 3f. the reason-code classifier that narrows INVALID_EXTRACTOR_OUTPUT ──
    # Reverts to the shape the P2 audit finding named: every parser-side rejection
    # collapsing to one coarse code regardless of which rule actually rejected it.
    ("reason-code lookup disabled — every parser rejection collapses to one bucket again",
     EXT,
     '    code = getattr(exc, "reason_code", None)',
     "    code = None",
     T_QC, "test_8_19d_a_canon_ref_rejection_differs_from_an_address_rejection"),

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
    # 🔴 THIS HARNESS ISOLATED NOTHING AT ALL. A mutation the same length as the text
    #    it replaces, written inside one mtime tick, is invisible to CPython's
    #    (size, mtime) cache key — so the run could import the PRE-mutation bytecode
    #    and score a mutant SURVIVED that never executed. Every subprocess now gets
    #    its own empty cache prefix from `_harness`, which no other run can read.
    with isolated_run() as env:
        return subprocess.run(
            [sys.executable, "-m", "pytest", str(WT / test_file), "-q", "--no-header",
             "-p", "no:warnings", "--tb=no", "-k", test_name.split("[")[0]],
            cwd=str(WT), capture_output=True, text=True, env=env)


def _apply(path, pairs):
    """Write the mutation. Returns the mutated text, or None if an anchor missed.

    Restoration is NOT this function's business: `restore_guard` in the caller owns
    it, and owns the verification too."""
    mutated = original = path.read_text(encoding="utf-8")
    for find, repl in pairs:
        if mutated.count(find) != 1:
            return None
        mutated = mutated.replace(find, repl, 1)
    if mutated != original:
        path.write_text(mutated, encoding="utf-8")
    return mutated


def main() -> int:
    cases = [(lbl, f, [(a, b)], tf, tn) for lbl, f, a, b, tf, tn in BREAKS]
    lbl, f, pairs, tf, tn = TWO_PART
    cases.append((lbl, f, pairs, tf, tn))

    killed = survived = missed = 0
    for label, path, pairs, test_file, test_name in cases:
        # `restore_guard` puts bytes, mode AND mtime_ns back and verifies all three;
        # the old `finally` here wrote the text back and checked nothing.
        with restore_guard(path):
            if _apply(path, pairs) is None:
                print(f"PATTERN-MISS  {label}\n              anchor no longer matches {path.name} "
                      f"— the control was NEVER exercised; check the source before assuming drift")
                missed += 1
                continue
            res = _run(test_file, test_name)
        # 🔴 A NON-ZERO EXIT IS NOT A KILL. pytest exits 4 when a node path is bad and 5
        #    when `-k` matches nothing — both non-zero, and both would have been counted as
        #    KILLED here while proving nothing at all. The embedded harness grew this guard
        #    first; the harness that actually runs standalone had none.
        if res.returncode in (4, 5) or "no tests ran" in res.stdout \
                or "not found:" in res.stderr:
            print(f"NO-WITNESS    {label}\n              {test_name} collected NOTHING "
                  f"(rc={res.returncode}) — the mutant was never exercised")
            missed += 1
            continue
        # 🔴 ...AND A NON-ZERO EXIT WITH NO FAILED TEST IS NOT ONE EITHER. The guard above
        #    catches a witness that never ran; it does NOT catch a mutant that broke
        #    COLLECTION of its own target file, which also exits non-zero and would have
        #    been printed as KILLED. That is not hypothetical: a call-site harness written
        #    against this same tree reported 4/4 KILLED on returncode alone while every
        #    mutant was in fact green — an unrelated collection error was supplying the
        #    exit status. A kill is a test that RAN and FAILED, and nothing else.
        #    (The embedded harness in `test_canon_lite_p0b_semantic_source.py` has
        #    asserted this all along; only the standalone one was missing it.)
        if res.returncode != 0 and " failed" not in res.stdout:
            print(f"NO-FAILURE    {label}\n              exited rc={res.returncode} without a "
                  f"test failure — the mutant broke collection rather than being caught")
            missed += 1
            continue
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
    try:
        sys.exit(main())
    except IsolationError as exc:
        print(f"\n!! {exc}")
        sys.exit(2)
