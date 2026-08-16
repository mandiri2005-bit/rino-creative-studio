#!/usr/bin/env python3
"""Mutation proof for the F4b bounded corrective retry.

    python3 tests/mutation/prove_f4b.py              # prove the controls
    python3 tests/mutation/prove_f4b.py --selftest   # prove THIS FILE can fail

Override the checkout with PROVE_WT=/path/to/worktree.

Hardening inherited from prove_f1_round2 / prove_f2 / prove_f3 / prove_f4a, and it is
the whole reason this file is worth anything:

  · Only pytest exit 0 and exit 1 are verdicts. Exit 2/3/4/5, a collection error, or an
    exit 1 with no "N failed" summary is INCONCLUSIVE — never a kill. A harness that
    scores `returncode != 0` as a kill cannot fail, so it proves nothing: the suite's
    outbound-network guard, an import error or a typo'd node id would all read as
    "mutant killed".
  · The baseline node must be GREEN before the mutation. A test that was already red
    would "kill" every mutant it is pointed at.
  · The source is restored byte-identically in a `finally`, and a failed restore aborts
    the run rather than leaving a dirty tree.
  · `--selftest` deliberately breaks the harness three ways and requires all three to be
    REFUSED.

⚠ PATTERN HAZARDS, all real and all paid for here:
  · Replacements are applied through a CALLABLE, never a string template. `re.sub`
    processes escapes in a string replacement, so a mutant containing `\\n` would inject
    a real newline and produce a SyntaxError — a crash, not a mutation, and the harness
    would score the resulting collection error as INCONCLUSIVE at best.
  · Several anchors must match EXACTLY ONCE. `"provider_calls": provider_calls,` and
    `schema_retry_*` now appear at more than one site (the summary builder, the stats
    return, the fallback dicts), so each pattern is anchored on its own indentation or
    on a neighbouring line. A pattern matching twice mutates only the first under
    `count=1` and would silently prove the wrong site — the harness refuses it.
"""
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _harness import IsolationError, isolated_run, restore_guard  # noqa: E402

WT = pathlib.Path(os.environ.get(
    "PROVE_WT", str(pathlib.Path(__file__).resolve().parents[2])))
LZ = WT / "python/laozhang_api.py"
AP = WT / "python/narasi_addressed_patch.py"
PROBE = WT / "python/tools/narasi_f4b_probe.py"
T = "tests/python/test_narasi_f4b_schema_retry.py"
PT = "tests/python/test_narasi_f4b_probe_artifact.py"
AT = "tests/python/test_narasi_addressed_patch.py"

BREAKS = [
    # ── the eligibility boundary ───────────────────────────────────────────
    ("1. `unknown_operation` loses its retry eligibility (the live failure goes unanswered)",
     [(LZ, r'(?m)^    "unknown_operation",\n\}\)$', "})")],
     T, "test_a_broken_contract_is_repaired_by_exactly_one_corrective_call[unknown_operation]"),

    ("2. a SEMANTIC guard is admitted to the retryable set (word_band becomes negotiable)",
     [(LZ, r'(?m)^    "unknown_operation",\n\}\)$',
       '    "unknown_operation",\n    "word_band",\n})')],
     T, "test_a_semantic_rejection_never_buys_a_second_call[word_band]"),

    # ── the bound ──────────────────────────────────────────────────────────
    ("3. the retry stops being bounded at one — a third call is issued",
     [(LZ, r"(?m)^                    candidate, reason, out_words = _evaluate\(retry_raw\)$",
       "                    candidate, reason, out_words = _evaluate(retry_raw)\n"
       "                    if reason is not None and reason in _STRUCTURAL_SCHEMA_RETRYABLE:\n"
       "                        _again_raw, _again_bad = await _exchange(\n"
       "                            system,\n"
       "                            _narasi_structural_corrective_user(\n"
       "                                user, reason, PATCH_SCHEMA_VERSION),\n"
       "                            0.0)\n"
       "                        if _again_bad is None:\n"
       "                            candidate, reason, out_words = _evaluate(_again_raw)")],
     T, "test_the_retry_is_capped_at_one_even_when_a_third_answer_would_land"),

    # ── the corrective prompt ──────────────────────────────────────────────
    ("4. the corrective contract loses its closed verb list",
     [(LZ, r'(?m)^        "Allowed exact operations:\\n"\n'
           r'        "insert_before: op, anchor_id, text\\n"\n'
           r'        "insert_after:  op, anchor_id, text\\n"\n'
           r'        "replace:       op, unit_id, text\\n"\n'
           r'        "move:          op, unit_id, exactly one of before_id/after_id\\n\\n"$',
       '        ""')],
     T, "test_the_corrective_prompt_restates_the_closed_contract"),

    ("5. the REJECTED RESPONSE is echoed back into the corrective prompt (the leak)",
     [(LZ, r"(?m)^                        user, _initial_reason, PATCH_SCHEMA_VERSION\),$",
       '                        user + "\\n[PREVIOUS]\\n" + raw_patch,\n'
       "                        _initial_reason, PATCH_SCHEMA_VERSION),")],
     T, "test_the_rejected_response_never_reaches_the_retry_prompt_or_anything_else"),

    ("6. the retry is prompted with the CANDIDATE instead of the original unit table",
     [(LZ, r"(?m)^                        user, _initial_reason, PATCH_SCHEMA_VERSION\),$",
       "                        raw_patch, _initial_reason, PATCH_SCHEMA_VERSION),")],
     T, "test_the_corrective_prompt_carries_the_original_unit_table_not_the_candidate"),

    ("7. the retry stops being deterministic (a second lottery ticket, not a correction)",
     [(LZ, r"(?m)^                    0\.0\)$", "                    0.1)")],
     T, "test_the_retry_is_deterministic"),

    # ── the counters ───────────────────────────────────────────────────────
    ("8. a landed retry no longer raises `accepted`",
     [(LZ, r"(?m)^        if _retried:\n            schema_retry_accepted \+= 1$",
       "        pass")],
     T, "test_the_landed_repair_changes_the_hash_and_nothing_else"),

    ("9. the retry counters are DERIVED from another counter instead of counted",
     [(LZ, r'(?m)^            "schema_retry_accepted": schema_retry_accepted,$',
       '            "schema_retry_accepted": accepted,')],
     T, "test_a_valid_first_response_costs_one_call_and_no_retry"),

    ("10. `provider_calls` is derived from `attempted` again — the retry's call vanishes",
     [(LZ, r'(?m)^            "provider_calls": provider_calls,$',
       '            "provider_calls": attempted,')],
     T, "test_the_second_response_is_metered_and_the_counters_are_exact"),

    ("11. the summary schema is left at v2 after three fields were added",
     [(LZ, r'(?m)^_STRUCTURAL_PATCH_SUMMARY_VERSION = "structural_patch_summary_v3"$',
       '_STRUCTURAL_PATCH_SUMMARY_VERSION = "structural_patch_summary_v2"')],
     T, "test_the_summary_is_exactly_the_v3_shape"),

    # `schema_retry_accepted > 0 => manuscript_changed` is enforced by COMPOSITION, not
    # by a line of its own: `retry_accepted <= accepted` plus the accepted-without-change
    # invariant. A dedicated third check could never fire and a mutation run proved it
    # (it survived, caught by its twin). Both halves of the composition are mutated
    # here instead, so the property has two real proofs rather than one fake one.
    ("12. the accepted-without-change invariant is removed (the false-success gate)",
     [(LZ, r"(?m)^    if accepted > 0 and manuscript_changed is False:$",
       "    if False:")],
     T, "test_a_retry_accepted_over_an_unchanged_manuscript_is_refused"),

    ("12b. `schema_retry_accepted <= chapters_accepted` is removed (the other half)",
     [(LZ, r"(?m)^    if retry_accepted > accepted:$", "    if False:")],
     T, "test_the_retry_counters_refuse_impossible_arithmetic[kwargs3]"),

    ("13. the retry partition invariant stops closing",
     [(LZ, r"(?m)^    if retry_accepted \+ retry_exhausted != retry_chapters:$",
       "    if False:")],
     T, "test_the_retry_counters_refuse_impossible_arithmetic[kwargs2]"),

    # ── one chapter, one final verdict ─────────────────────────────────────
    ("14. the INITIAL rejection is counted too — one chapter, two verdicts",
     [(LZ, r"(?m)^                _retried = True$",
       "                _rejected(reason, chapter_number, original_words)\n"
       "                _retried = True")],
     T, "test_a_successful_retry_records_no_rejection_at_all"),

    # ── accounting for the second call ─────────────────────────────────────
    ("15. the retry's response is never metered (free repair the ledger cannot see)",
     [(LZ, r"(?m)^                total_cr \+= int\(await _log_narasi_usage\(\n"
           r"                    tenant_id, user_id, rev_model, _response, job_id=job_uuid,\n"
           r"                    credit_row=credit_row\) or 0\)$",
       "                await _log_narasi_usage(\n"
       "                    tenant_id, user_id, rev_model, _response, job_id=job_uuid,\n"
       "                    credit_row=credit_row)\n"
       "                total_cr += 0 if _retried_meter_seen else 1")],
     T, "test_the_second_response_is_metered_and_the_counters_are_exact"),

    ("16. the shared legacy budget is debited with attempts again",
     [(LZ, r'(?m)^                    attempts_already_spent=int\(patch_stats\["provider_calls"\]\),$',
       '                    attempts_already_spent=int(patch_stats["attempted"]),')],
     T, "test_the_legacy_budget_is_debited_with_the_retrys_call_too[0]"),

    # ── Phase 0 ────────────────────────────────────────────────────────────
    ("17. `max_retries=0` goes back to fail-open",
     [(LZ, r"(?m)^                _client = _client\.with_options\(max_retries=0\)$",
       "                try:\n"
       "                    _client = _client.with_options(max_retries=0)\n"
       "                except Exception:\n"
       "                    pass")],
     T, "test_a_client_that_cannot_disable_sdk_retries_is_refused_before_any_transport"),

    ("18. a retry refused by the cap invents a provider verdict it never received",
     [(LZ, r"(?m)^                if retry_exchange_reason is not None:$",
       "                if False:")],
     T, "test_a_retry_refused_by_the_cap_creates_no_phantom_call"),

    ("19. a post-call fault on the retry stops closing the partition",
     [(LZ, r"(?m)^            if _retried:\n                schema_retry_exhausted \+= 1$",
       "            pass")],
     T, "test_the_counters_survive_a_post_call_fault_without_breaking_the_partition"),

    # ── metering: a response that ARRIVED is billable, usable or not ───────
    ("20. `choices` is judged before metering — an error-as-200 becomes free",
     [(LZ, r"(?m)^            try:\n"
           r"                total_cr \+= int\(await _log_narasi_usage\($",
       "            if not getattr(_response, \"choices\", None):\n"
       "                return None, \"response_empty\"\n"
       "            try:\n"
       "                total_cr += int(await _log_narasi_usage(")],
     T, "test_an_empty_second_response_that_carried_usage_is_still_metered"),

    # ── the final verdict must name its source, never an ordinal ──────────
    ("21. a refused retry reports the retry's own source instead of the initial schema",
     [(LZ, r'(?m)^                    _final_source = "initial_schema"$',
       '                    _final_source = "retry_candidate"')],
     T, "test_the_final_verdict_names_its_source_and_never_fabricates_an_attempt_number"),

    ("22. the final source is hardcoded instead of tracked",
     [(LZ, r"(?m)^                          final_source=_final_source\)$",
       '                          final_source="retry_candidate")')],
     T, "test_every_rejection_path_reports_the_artifact_that_produced_it[first_candidate]"),

    ("23. a post-call fault stops naming itself as the source",
     [(LZ, r'(?m)^                      final_source="internal_error"\)$',
       "                      )")],
     T, "test_a_post_call_fault_reports_itself_as_the_source"),

    # ── the F4b probe: the artifact must not under-report what it spent ───
    ("24. the probe keeps ONE response slot again — last write wins (the F4a defect)",
     [(PROBE, r"(?m)^            per_response\.append\(\{$",
       "            del per_response[:]\n            per_response.append({")],
     PT, "test_the_cost_is_the_sum_over_every_response_not_the_last_one"),

    ("25. the probe prices only the LAST response instead of summing",
     [(PROBE, r'(?m)^    tok_in = sum\(item\["prompt_tokens"\] for item in per_response\)$',
       '    tok_in = sum(item["prompt_tokens"] for item in per_response[-1:])')],
     PT, "test_distinct_usages_are_added_rather_than_doubled"),

    ("26. the probe's DoD is always met — an artifact that cannot say no",
     [(PROBE, r'(?m)^        "dod": \{"met": all\(checks\.values\(\)\), "checks": checks\},$',
       '        "dod": {"met": True, "checks": checks},')],
     PT, "test_a_run_where_nothing_lands_fails_the_dod_and_names_the_failures"),

    ("27. the probe claims the manuscript changed without comparing it",
     [(PROBE, r'(?m)^        "manuscript_changed": before != after,$',
       '        "manuscript_changed": True,')],
     PT, "test_a_run_where_nothing_lands_fails_the_dod_and_names_the_failures"),

    ("28. the F4b artifact reuses the F4a schema name for a different key set",
     [(PROBE, r'(?m)^ARTIFACT_SCHEMA_VERSION = "narasi_f4b_probe_artifact_v2"$',
       'ARTIFACT_SCHEMA_VERSION = "narasi_f4a_probe_artifact_v2"')],
     PT, "test_the_schema_version_is_distinct_from_the_f4a_artifact"),

    ("29. the probe's `live` is granted by the flag again instead of earned",
     [(PROBE,
       r'(?m)^    if not allow_network:\n'
       r'        recording_mode = "offline_dry_run"\n'
       r'    elif upstream >= 1 and responses >= 1 and reported == upstream:\n'
       r'        recording_mode = "live"\n'
       r'    else:\n'
       r'        recording_mode = "failed_probe"$',
       '    recording_mode = "live" if allow_network else "offline_dry_run"')],
     PT, "test_a_paid_run_that_never_reached_the_provider_is_not_live_evidence"),

    # Mutant 30 ("the CLI can raise the two-call bound") was DELETED, not weakened:
    # the duplicate CLI check it targeted is gone. `run_probe()` is the single door and
    # mutant 40 proves it — the CLI test now dies on that same mutant.
    ("31. the untouched-unit comparison always reads True",
     [(PROBE, r'(?m)^            out\["untouched_units_identical"\] = all\(\n'
              r'                units_before\[index\] == units_after\[index\]\n'
              r'                for index in range\(len\(units_before\)\) if index not in expected\)$',
       '            out["untouched_units_identical"] = True')],
     PT, "test_a_candidate_that_rewrites_a_second_unit_fails_the_dod"),

    ("31b. the comparison is scoped to what CHANGED again — the tautology restored",
     [(PROBE, r"(?m)^                for index in range\(len\(units_before\)\) if index not in expected\)$",
       "                for index in range(len(units_before)) if index not in set(changed))")],
     PT, "test_a_candidate_that_rewrites_a_second_unit_fails_the_dod"),

    ("31c. `only_expected_units_changed` counts edits instead of locating them",
     [(PROBE, r'(?m)^            out\["only_expected_units_changed"\] = set\(changed\) == expected$',
       '            out["only_expected_units_changed"] = len(changed) == len(expected)')],
     PT, "test_untouched_unit_identity_reads_false_when_an_unexpected_unit_moves"),

    # ── usage must be OBSERVED, never assumed ─────────────────────────────
    ("32. usage is assumed present, so a zero cost reads as complete coverage",
     [(PROBE, r'(?m)^    if usage is None or not \(has_prompt or has_completion\):\n'
              r'        status = "absent"\n'
              r'    elif has_prompt and has_completion:\n'
              r'        status = "complete"\n'
              r'    else:\n'
              r'        status = "partial"$',
       '    status = "complete"')],
     PT, "test_usage_that_the_provider_never_reported_is_not_called_recorded"),

    ("33. the DoD counts responses again instead of checking usage was reported",
     [(PROBE, r'(?m)^            and usage_source == "provider_reported"\),$', "            ),")],
     PT, "test_usage_that_the_provider_never_reported_is_not_called_recorded"),

    # ── the artifact must bind the code that produced it ──────────────────
    ("34. the artifact stops binding the runners that compute the evidence",
     [(PROBE, r'(?m)^            "narasi_f4b_probe_sha256": _sha256_file\(_HERE\),\n'
              r'            "narasi_f4a_probe_sha256": _sha256_file\(\n'
              r'                _HERE\.parent / "narasi_f4a_probe\.py"\),$', "")],
     PT, "test_the_artifact_has_exactly_the_declared_keys"),

    # ── the CLI must fail closed ──────────────────────────────────────────
    ("35. the CLI returns success even though the DoD was not met",
     [(PROBE, r"(?m)^    if not met:$", "    if False:")],
     PT, "test_the_cli_fails_closed_when_the_dod_is_not_met"),

    ("36. a network run that is not live evidence stops being named as such",
     [(PROBE, r'(?m)^    if args\.allow_network and mode != "live":$', "    if False:")],
     PT, "test_the_cli_fails_a_network_run_that_is_not_live_evidence"),

    # ── the target must carry the defect the finding describes ────────────
    ("37. the probe target goes back to one that already satisfies its own finding",
     [(PROBE, r'(?m)^    "Ia menolak menyerahkan haknya dan menyimpan map itu kembali\.\\n\\n"$',
       '    "Ia menyerahkan haknya sebelum rekaman diputar.\\n\\n"')],
     PT, "test_the_probe_target_genuinely_violates_the_beat_it_claims_to_repair"),

    ("38. the fix stops naming the unit to replace, so the expected position is ungrounded",
     [(PROBE, r'(?m)^        "Replace u002 so the surrender happens there, before the ruling in u003\. Do "\n'
              r'        "not touch any other unit\."\),$',
       '        "Improve the chapter."),')],
     PT, "test_the_expected_edit_position_is_grounded_in_the_prompt"),

    # ── partial usage must not be promoted to complete ────────────────────
    ("39. usage completeness accepts EITHER field again instead of both",
     [(PROBE, r"(?m)^    elif has_prompt and has_completion:$",
       "    elif has_prompt or has_completion:")],
     PT, "test_usage_missing_one_field_inside_a_response_is_partial_not_complete"),

    # ── the two-call bound belongs to the runner ──────────────────────────
    ("40. the cap bound leaves the runner and lives only in the CLI again",
     [(PROBE, r"(?m)^    if not 1 <= upstream_cap <= DEFAULT_UPSTREAM_CAP:$",
       "    if False:")],
     PT, "test_run_probe_itself_refuses_a_cap_outside_the_authorised_bound[5]"),

    # ── the verb-as-wrapper-key normalizer ────────────────────────────────
    #
    # 🔴 WHY THIS BLOCK EXISTS, AND WHY IT WAS ADDED LATE. `_normalize_verb_as_key_
    #    operation` was carried as a PROVISIONAL implementation nobody had audited, and
    #    F4b's mutation proof did not touch it. The live probe of 2026-08-15T17:16:24Z
    #    settled that: the model returned the WRAPPER-KEY shape (`operation_source=
    #    single_wrapper_key`, `token_length=7`, token hash = SHA-256 of "replace"), the
    #    normalizer unwrapped it, and the patch was accepted on the FIRST call. The
    #    repair that finally landed in production landed THROUGH this function — it is
    #    load-bearing, not a spare part.
    #
    #    Its tests already existed (test_narasi_addressed_patch.py, the
    #    `_normalize_verb_as_key_*` block). What did not exist was any proof that those
    #    tests can FAIL. Two failure directions are mutated separately: BYPASS (the
    #    live-observed shape stops being accepted) and ACCEPTANCE WIDENING (shapes the
    #    audit deliberately refused start being unwrapped). A widened normalizer is the
    #    more dangerous of the two: it quietly enlarges the vocabulary the four closed
    #    verbs exist to keep small, which is precisely what F4b refused to do by guess.

    ("41. BYPASS — the normalizer stops unwrapping and returns the raw operation",
     [(AP, r'(?m)^    return \{"op": verb, \*\*fields\}$', "    return raw_op")],
     AT, "test_normalize_verb_as_key_unwraps_the_exact_probe_observed_shape"),

    ("41b. BYPASS at the WIRING — the validator stops calling the normalizer at all",
     [(AP, r"(?m)^        raw_op = _normalize_verb_as_key_operation\(raw_op\)$",
       "        pass")],
     AT, "test_addressed_patch_accepts_the_exact_probe_observed_verb_as_key_shape"),

    ("42. WIDENING — the closed verb set grows a fifth member",
     [(AP, r'(?m)^_KNOWN_OP_VERBS = frozenset\(\{"insert_before", "insert_after", "replace", "move"\}\)$',
       '_KNOWN_OP_VERBS = frozenset({"insert_before", "insert_after", "replace", "move", "delete"})')],
     AT, "test_normalize_verb_as_key_never_fires_for_an_unrecognized_verb"),

    ("42b. WIDENING — the known-verb gate is dropped, so ANY wrapping key unwraps",
     [(AP, r'(?m)^    if verb not in _KNOWN_OP_VERBS or not isinstance\(fields, Mapping\) or "op" in fields:$',
       '    if not isinstance(fields, Mapping) or "op" in fields:')],
     AT, "test_normalize_verb_as_key_never_fires_for_an_unrecognized_verb"),

    ("42c. WIDENING — the ambiguous inner-`op` refusal is dropped (the audit's finding)",
     [(AP, r'(?m)^    if verb not in _KNOWN_OP_VERBS or not isinstance\(fields, Mapping\) or "op" in fields:$',
       '    if verb not in _KNOWN_OP_VERBS or not isinstance(fields, Mapping):')],
     AT, "test_normalize_verb_as_key_never_fires_when_the_inner_dict_already_has_op"),

    ("42d. WIDENING — a multi-key mapping is unwrapped instead of refused",
     [(AP, r"(?m)^    if not isinstance\(raw_op, Mapping\) or len\(raw_op\) != 1:$",
       "    if not isinstance(raw_op, Mapping):")],
     AT, "test_normalize_verb_as_key_never_fires_for_a_multi_key_mapping"),

    ("42e. WIDENING — a non-mapping value is unwrapped instead of refused",
     [(AP, r'(?m)^    if verb not in _KNOWN_OP_VERBS or not isinstance\(fields, Mapping\) or "op" in fields:$',
       '    if verb not in _KNOWN_OP_VERBS or "op" in fields:')],
     AT, "test_normalize_verb_as_key_never_fires_when_the_value_is_not_a_mapping"),

    ("43. the ambiguous known-verb wrapper stops routing to unknown_operation",
     [(AP, r"(?m)^                if _only_key not in _KNOWN_OP_VERBS:$", "                if True:")],
     AT, "test_addressed_patch_rejects_an_ambiguously_nested_known_verb_as_unknown_operation"),

    # ── deterministic fault injection: the closure probe ──────────────────
    #
    # 🔴 SIX PROPERTIES, SIX WAYS TO BREAK THEM. This mode exists to spend two billable
    #    calls on a claim, so every property it rests on has to be shown breakable:
    #    injection strictly after receive+meter · confined to the probe · production
    #    untouched · both calls physical · cap pinned at two · the artifact unable to
    #    hide or mislabel what it did.

    # 🔴 THE GUARD THAT USED TO SIT HERE WAS REDUNDANT AND THE MUTATION RUN PROVED IT.
    #    The injection gate once ALSO required "…and it has already been metered",
    #    restating an ordering production already guarantees. With both in place either
    #    could be deleted and every test stayed green — each covered the other. The
    #    restatement is gone; the ordering is verified from the recorded trace, and
    #    these mutants break it for real.
    ("44. the injection is moved to CAPTURE time — it fires before the response is "
     "metered AND falsifies the probe's own record of what the model answered",
     [(PROBE, r"(?m)^                raw = _real_resp_content\(response\) or \"\"$",
       '                raw = lz._resp_content(response) or ""'),
      (PROBE, r"(?m)^                and len\(per_response\) == 1\):$",
       "                and len(per_response) == 0):")],
     PT, "test_the_first_response_is_recorded_as_the_provider_actually_answered_it"),

    ("44b. …and the artifact then LIES about the ordering, because "
     "`applied_after_metering` is asserted instead of derived from the trace",
     [(PROBE, r"(?m)^                raw = _real_resp_content\(response\) or \"\"$",
       '                raw = lz._resp_content(response) or ""'),
      (PROBE, r"(?m)^                and len\(per_response\) == 1\):$",
       "                and len(per_response) == 0):"),
      (PROBE, r'(?m)^    applied_after_metering = \(\n'
              r'        "metered:1" in _seq and "injected:1" in _seq\n'
              r'        and _seq\.index\("metered:1"\) < _seq\.index\("injected:1"\)\)$',
       "    applied_after_metering = bool(injection[\"applied\"])")],
     PT, "test_the_injection_fires_only_after_the_first_response_was_metered"),

    ("46. the injection seam is not handed back and outlives the run",
     [(PROBE, r"(?m)^        lz\._resp_content = _real_resp_content$", "        pass")],
     PT, "test_the_injection_does_not_outlive_the_run"),

    ("47. the injection leaks into NORMAL runs (no longer gated on the flag)",
     [(PROBE, r'(?m)^        if \(fault_injection and not injection\["applied"\]$',
       '        if ((True) and not injection["applied"]')],
     PT, "test_a_normal_run_is_never_labelled_as_injected_and_carries_the_block_anyway"),

    ("48. an injected run is filed under the naturally-occurring probe kind",
     [(PROBE, r'(?m)^        "probe_kind": \("deterministic_fault_injection" if fault_injection\n'
              r'                       else "targeted_non_delivery_repair_landing"\),$',
       '        "probe_kind": "targeted_non_delivery_repair_landing",')],
     PT, "test_an_injected_run_is_named_as_one_everywhere_a_reader_looks"),

    ("48b. an injected run is filed under the naturally-occurring FILENAME",
     [(PROBE, r'(?m)^    stem = \("f4b-fault-injection-probe" if fault_injection\n'
              r'            else "f4b-repair-landing-probe"\)$',
       '    stem = "f4b-repair-landing-probe"')],
     PT, "test_an_injected_run_is_named_as_one_everywhere_a_reader_looks"),

    ("49. the artifact reports the injection as not applied — hiding it",
     [(PROBE, r'(?m)^            "applied": bool\(injection\["applied"\]\),$',
       '            "applied": False,')],
     PT, "test_an_injected_run_is_named_as_one_everywhere_a_reader_looks"),

    ("49b. the DoD stops requiring that the injection actually happened",
     [(PROBE, r'(?m)^        checks\["fault_injection_applied"\] = injection\["applied"\] is True$',
       '        checks["fault_injection_applied"] = True')],
     PT, "test_an_injection_that_could_not_happen_is_reported_as_not_applied"),

    ("50. the two-call cap can be relaxed for an injected run",
     [(PROBE, r"(?m)^    if fault_injection and upstream_cap != DEFAULT_UPSTREAM_CAP:$",
       "    if False:")],
     PT, "test_fault_injection_refuses_any_cap_other_than_two[1]"),

    ("51. the schema is left at v1 after the key set gained `fault_injection`",
     [(PROBE, r'(?m)^ARTIFACT_SCHEMA_VERSION = "narasi_f4b_probe_artifact_v2"$',
       'ARTIFACT_SCHEMA_VERSION = "narasi_f4b_probe_artifact_v1"')],
     PT, "test_the_v2_schema_is_declared_and_distinct_from_the_live_v1_artifact"),
]


def _pytest(test_file, test_name):
    with isolated_run() as env:
        return subprocess.run(
            [sys.executable, "-m", "pytest", f"{test_file}::{test_name}",
             "-q", "-p", "no:cacheprovider", "--no-header"],
            cwd=WT, capture_output=True, text=True, env=env)


def _classify(proc):
    """What pytest ACTUALLY said. Returns ("pass"|"fail", None) or (None, reason)."""
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
    verdict, why = _classify(_pytest(test_file, test_name))
    if verdict != "pass":
        print(f"  !! BASELINE NOT GREEN ({why or verdict}) — {label}")
        return False

    targets = list(dict.fromkeys(path for path, _p, _r in edits))
    for path in targets:
        if not path.exists():
            print(f"  !! SOURCE MISSING {path} — {label}")
            return False

    with restore_guard(*targets):
        for path, pattern, repl in edits:
            src = path.read_text(encoding="utf-8")
            mutated, n = re.subn(pattern, lambda _m, _r=repl: _r, src)
            if n != 1:
                print(f"  !! PATTERN MATCHED {n}x (need exactly 1) — {label}")
                return False
            path.write_text(mutated, encoding="utf-8")

        verdict, why = _classify(_pytest(test_file, test_name))
        if verdict is None:
            print(f"  !! INCONCLUSIVE ({why}) — {label}")
            return False
        killed = (verdict == "fail")
        print(f"  {'KILLED ' if killed else 'SURVIVED'} — {label}")
        return killed


def selftest():
    print("Self-test: the harness must REFUSE these three broken configurations\n")
    good = [(LZ, r"(?m)^        if _retried:\n            schema_retry_accepted \+= 1$",
             "        pass")]
    cases = [
        ("missing test file",
         ("8. a landed retry no longer raises `accepted`", good,
          "tests/python/test_this_file_does_not_exist.py",
          "test_the_landed_repair_changes_the_hash_and_nothing_else")),
        ("wrong node id",
         ("8. a landed retry no longer raises `accepted`", good, T,
          "test_no_such_test_name_at_all")),
        ("pattern miss (e.g. after an innocent rename)",
         ("pattern that no longer exists",
          [(LZ, r"(?m)^        _THIS_SYMBOL_WAS_RENAMED \+= 1$", "        pass")],
          T, "test_the_landed_repair_changes_the_hash_and_nothing_else")),
    ]
    ok = True
    for name, args in cases:
        refused = not run(*args)
        print(f"  {'OK      ' if refused else 'BROKEN  '} — {name} "
              f"{'refused' if refused else 'was SCORED AS A KILL'}")
        ok = ok and refused
    print(f"\nself-test: {'PASS — the harness can fail' if ok else 'FAIL — this harness proves nothing'}")
    return ok


if __name__ == "__main__":
    try:
        if "--selftest" in sys.argv:
            sys.exit(0 if selftest() else 2)
        print("Mutation proof: F4b bounded corrective retry\n")
        results = [run(*b) for b in BREAKS]
    except IsolationError as exc:
        print(f"\n!! {exc}")
        sys.exit(2)
    print(f"\n{sum(results)}/{len(results)} mutants killed")
    sys.exit(0 if all(results) else 1)
