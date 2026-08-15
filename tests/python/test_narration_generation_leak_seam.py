"""F1 seam wiring — BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md.

`scrub_and_verify_generation_leak` is the function `_run_narration_job` (Engine B, the
only engine whose generation injection can leak — Engine A/Classic never touches canon
at all) calls at the gap between the L3-repair try/except and `_persist_chapters` /
`_finalize`. Tested here as a standalone unit against plain `result` dicts, exactly the
shape `_persist_chapters` and `_result_payload` already read (`result["book"]`/
`result["output"]`, `result["chapters"][*]["content"]`) — no need to fake the whole
job harness to prove the seam's actual logic.
"""

import canon_lite as cl
import narration_api as na


def _v9_canon():
    outline = [{"id": i + 1, "title": f"Bab {i + 1}", "summary": f"ringkasan {i + 1}"}
               for i in range(3)]
    cfg = cl.build_job_config_snapshot(outline_chapters=outline, target_language="id",
                                       narration_style="kdrama_serial")
    return cl.build_canon_lite_v1(outline_chapters=outline, job_config=cfg, anchors=[
        cl.CanonAnchorV1("anc1", "time", "five years of absence"),
        cl.CanonAnchorV1("anc3", "time", "early September"),
    ])


def test_scrubs_the_book_key_and_reports_clear():
    canon = _v9_canon()
    result = {"book": "the crisp [anc3] early September air", "chapters": []}
    clear, leaked = na.scrub_and_verify_generation_leak(result, canon)
    assert clear is True
    assert leaked == ()
    assert "[anc3]" not in result["book"]
    assert "the crisp early September air" == result["book"]


def test_scrubs_output_key_when_book_is_absent():
    """`resolve_manuscript_key` mirrors the delivery path's own `book` else `output`
    choice (canon_lite_l2.py) — scenarios C/D/E carry `output`."""
    canon = _v9_canon()
    result = {"output": "echoed [anc1] five years of absence", "chapters": []}
    clear, _ = na.scrub_and_verify_generation_leak(result, canon)
    assert clear is True
    assert "[anc1]" not in result["output"]


def test_scrubs_every_chapter_content_not_just_the_assembled_book():
    """`_persist_chapters` writes narasi_chapters from `result["chapters"][*]["content"]`
    independently of the assembled book string — a scrub that only touched the book
    would leave the per-chapter DB rows leaking even though the assembled markdown
    looked clean."""
    canon = _v9_canon()
    result = {
        "book": "clean assembled text",
        "chapters": [
            {"no": 1, "content": "the crisp [anc3] early September air"},
            {"no": 2, "content": "clean chapter two"},
        ],
    }
    clear, leaked = na.scrub_and_verify_generation_leak(result, canon)
    assert clear is True
    assert leaked == ()
    assert "[anc3]" not in result["chapters"][0]["content"]
    assert result["chapters"][1]["content"] == "clean chapter two"


def test_detect_is_the_authority_not_scrubs_side_effect(monkeypatch):
    """The seam's whole reason to exist, and the brief's own DoD: 'ketika scrub
    sengaja dimutasi agar gagal, publication seam harus menolak delivery.' Neuter
    ONLY `scrub_bound_markers` to a no-op — `detect_bound_markers` stays the REAL
    function — and prove the seam still reports clear=False and blocks. A caller
    that trusted scrub's own removal count instead of an independent rescan would
    report clear=True here and ship the leak."""
    canon = _v9_canon()
    monkeypatch.setattr(cl, "scrub_bound_markers", lambda text, _c: (text, 0))
    result = {"book": "the crisp [anc3] early September air", "chapters": []}
    clear, leaked = na.scrub_and_verify_generation_leak(result, canon)
    assert clear is False
    assert leaked == ("anc3",)
    assert result["book"] == "the crisp [anc3] early September air", \
        "scrub was neutered — the marker is still there, and delivery must refuse it"


def test_detect_is_the_authority_across_a_chapter_content_row_too(monkeypatch):
    canon = _v9_canon()
    monkeypatch.setattr(cl, "scrub_bound_markers", lambda text, _c: (text, 0))
    result = {"book": "clean", "chapters": [{"no": 1, "content": "leftover [anc3] token"}]}
    clear, leaked = na.scrub_and_verify_generation_leak(result, canon)
    assert clear is False
    assert leaked == ("anc3",)


def test_unbound_bracket_from_a_different_canon_is_not_a_false_positive():
    """The mirror case, equally important: an id that is real and bound but for a
    DIFFERENT canon instance is NOT this run's marker — flagging it would be a false
    positive that blocks a perfectly clean book over an ordinary-looking bracket."""
    other_canon = cl.build_canon_lite_v1(
        outline_chapters=[{"id": 1, "title": "Bab 1", "summary": "x"}],
        job_config=cl.build_job_config_snapshot(
            outline_chapters=[{"id": 1, "title": "Bab 1", "summary": "x"}],
            target_language="id", narration_style="kdrama_serial"),
        anchors=[cl.CanonAnchorV1("anc9", "time", "unrelated")])
    result = {"book": "the crisp [anc3] early September air", "chapters": []}
    clear, leaked = na.scrub_and_verify_generation_leak(result, other_canon)
    assert clear is True
    assert leaked == ()
    assert result["book"] == "the crisp [anc3] early September air"


def test_none_canon_is_clear_and_a_pure_noop():
    result = {"book": "text with [anc1] in it", "chapters": [{"no": 1, "content": "x"}]}
    clear, leaked = na.scrub_and_verify_generation_leak(result, None)
    assert (clear, leaked) == (True, ())
    assert result["book"] == "text with [anc1] in it", "no canon — nothing to scrub against"


def test_scrubs_leak_from_fact_report_samples_not_just_book_and_chapters():
    """Adversarial audit of F1 (2026-08-14) proved, by executing real code, that a
    marker leaking into prose also lands verbatim in `fact_report` — populated by
    `_apply_v3_gates` BEFORE this seam runs, from raw pre-scrub sentence excerpts —
    and that field ships to the customer via `_result_payload()` even when `book`
    and every chapter's `content` are perfectly clean. This is the exact shape:
    `narasi_factscan.fact_scan()` copies ~150-char raw excerpts into
    `fact_report['classes'][name]['samples']`."""
    canon = _v9_canon()
    result = {
        "book": "clean assembled text",
        "chapters": [{"no": 1, "content": "clean chapter"}],
        "fact_report": {
            "classes": {
                "date": {"count": 1, "samples": [
                    "Dia menghilang selama [anc3] lima tahun sebelum kembali."]},
            },
        },
    }
    clear, leaked = na.scrub_and_verify_generation_leak(result, canon)
    assert clear is True
    assert leaked == ()
    assert "[anc3]" not in result["fact_report"]["classes"]["date"]["samples"][0]
    assert "lima tahun" in result["fact_report"]["classes"]["date"]["samples"][0], \
        "the rest of the excerpt must survive — only the marker is removed"


def test_scrubs_leak_from_phantom_name_report_sentence_field():
    canon = _v9_canon()
    result = {
        "book": "clean",
        "chapters": [],
        "phantom_name_report": {"names": [
            {"name": "Someone", "sentence": "context around [anc1] the mention"},
        ]},
    }
    clear, leaked = na.scrub_and_verify_generation_leak(result, canon)
    assert clear is True
    assert "[anc1]" not in result["phantom_name_report"]["names"][0]["sentence"]


def test_report_coverage_is_schema_agnostic_not_a_field_allowlist():
    """The point of the recursive rewrite: a report type that does not exist yet
    (no field-by-field version could have named it) is still covered, because every
    string anywhere under `result` is scrubbed — nesting depth and field name are
    both irrelevant."""
    canon = _v9_canon()
    result = {
        "book": "clean",
        "chapters": [],
        "some_future_gate_report_nobody_has_named_yet": {
            "nested": {"deeper": ["a list", "with [anc1] a leak inside"]},
        },
    }
    clear, leaked = na.scrub_and_verify_generation_leak(result, canon)
    assert clear is True
    assert "[anc1]" not in result["some_future_gate_report_nobody_has_named_yet"][
        "nested"]["deeper"][1]


def test_legitimate_brackets_survive_and_report_clear():
    canon = _v9_canon()
    result = {"book": "Meet me [Tuesday] at noon.", "chapters": []}
    clear, leaked = na.scrub_and_verify_generation_leak(result, canon)
    assert clear is True
    assert result["book"] == "Meet me [Tuesday] at noon."


# ===========================================================================
# 2026-08-15 re-audit REJECT finding: this seam runs AFTER `_canon_lite_l3_assist_repair`
# has already recorded `result["canon_lite_l3"]["delivery_binding"]`/`manuscript_sha256`
# (narration_api.py `_l3_record_outcome`, the sole write site). If this seam then actually
# CHANGES the manuscript field, the recorded binding/hash describe bytes that are no
# longer what ships — a stale MATCH. Golden invariant (brief §C, test "e"):
# `sha256(final_delivered_book) == canon_lite_l3.manuscript_sha256` whenever
# `delivery_binding == "MATCH"`. Never satisfied by "adjusting" the old hash — either the
# binding downgrades (this seam's fix) or a real re-verification re-establishes MATCH.
# ===========================================================================

def _l3_matched_result(book: str, *, sha256_of: str = None) -> dict:
    """A result shaped exactly like `_l3_record_outcome` leaves it after a real MATCH:
    `canon_lite_l3.manuscript_sha256` genuinely equals `sha256(book)` at construction
    time — the pre-scrub state this seam is about to receive."""
    import hashlib
    basis = sha256_of if sha256_of is not None else book
    return {
        "book": book,
        "chapters": [],
        "canon_lite_l3": {
            "mode": "assist", "outcome": "resolved", "stage": "complete",
            "delivery_binding": "MATCH",
            "manuscript_sha256": hashlib.sha256(basis.encode("utf-8")).hexdigest(),
        },
    }


def test_scrub_that_changes_the_book_downgrades_a_stale_match_binding():
    """CORRECTED 2026-08-15 (re-audit round 2): this originally also asserted that
    `manuscript_sha256` CHANGED. It must not — see
    `test_a_late_change_never_overwrites_the_verifier_hash`. What must change is the
    binding, which may no longer claim the pre-scrub bytes are what ships."""
    canon = _v9_canon()
    result = _l3_matched_result("the crisp [anc3] early September air")
    verified_hash = result["canon_lite_l3"]["manuscript_sha256"]
    clear, leaked = na.scrub_and_verify_generation_leak(result, canon)
    assert leaked == ()
    assert "[anc3]" not in result["book"]
    assert result["canon_lite_l3"]["delivery_binding"] != "MATCH"
    assert result["canon_lite_l3"]["manuscript_sha256"] == verified_hash
    assert clear is False


def test_the_separate_delivered_hash_equals_the_real_final_bytes():
    """The honest half of the pair: the post-scrub bytes DO get a hash recorded — in
    their own field, clearly labelled as the delivered (not verified) identity, and it
    genuinely matches what ships."""
    import hashlib
    canon = _v9_canon()
    result = _l3_matched_result("the crisp [anc3] early September air, [anc1] too")
    na.scrub_and_verify_generation_leak(result, canon)
    assert result["canon_lite_l3"]["delivered_manuscript_sha256"] == \
        hashlib.sha256(result["book"].encode("utf-8")).hexdigest()


def test_binding_that_already_matches_post_scrub_bytes_is_left_alone():
    """No leak, no change -- the ORIGINAL MATCH (and its hash) must survive untouched.
    Proves the fix is conditional on an ACTUAL byte change, not unconditional."""
    canon = _v9_canon()
    result = _l3_matched_result("perfectly clean prose, nothing bound here")
    original = dict(result["canon_lite_l3"])
    clear, leaked = na.scrub_and_verify_generation_leak(result, canon)
    assert clear is True
    assert leaked == ()
    assert result["canon_lite_l3"] == original


def test_a_mismatch_binding_is_not_touched_by_this_seam():
    """This seam's job is narrow: downgrade a STALE MATCH. A binding that already says
    MISMATCH (a real, unrelated L3 defect) is not this seam's concern to alter."""
    canon = _v9_canon()
    result = _l3_matched_result("the crisp [anc3] early September air")
    result["canon_lite_l3"]["delivery_binding"] = "MISMATCH"
    na.scrub_and_verify_generation_leak(result, canon)
    assert result["canon_lite_l3"]["delivery_binding"] == "MISMATCH"


def test_no_canon_lite_l3_telemetry_present_is_a_safe_noop_for_the_binding_fix():
    """Off/shadow mode (or assist that never ran L3) never populates `canon_lite_l3` at
    all -- the new binding-downgrade logic must not raise or fabricate the key."""
    canon = _v9_canon()
    result = {"book": "the crisp [anc3] early September air", "chapters": []}
    clear, _ = na.scrub_and_verify_generation_leak(result, canon)
    assert clear is True
    assert "canon_lite_l3" not in result


def test_removed_count_is_tracked_separately_from_residual_leak_detection():
    """Brief §D: accumulate `removed_count` separately from residual detection — a
    repaired marker (successfully scrubbed) must not be conflated with a residual one
    (still present after rescan) in whatever count a caller logs/persists. Bounded
    count only, never the raw prose/id it was found in (C12)."""
    canon = _v9_canon()
    result = {
        "book": "the crisp [anc3] early September air",
        "chapters": [{"no": 1, "content": "echoed [anc1] here too"}],
    }
    clear, leaked = na.scrub_and_verify_generation_leak(result, canon)
    assert clear is True
    assert leaked == ()
    assert result["f1_scrub_operations_count"] == 2


def test_removed_count_is_zero_on_a_clean_book_not_absent_or_none():
    canon = _v9_canon()
    result = {"book": "perfectly clean prose", "chapters": []}
    na.scrub_and_verify_generation_leak(result, canon)
    assert result["f1_scrub_operations_count"] == 0


def test_removed_count_aggregates_generation_and_l3_removals_not_just_this_seam():
    """🔴 THE HEALTHY JOB MUST NOT REPORT ZERO. Markers are removed at three seams,
    earliest-first by design; a job whose per-worker scrub caught everything leaves
    NOTHING for this final scan to find. Counting only this scan reports 0 removals
    for exactly the case where the defence worked — indistinguishable from a job that
    never produced a marker at all."""
    canon = _v9_canon()
    result = {
        "book": "clean by the time it got here",
        "chapters": [],
        "f1_generation_markers_removed": 4,   # summed per job in orchestrator/static.py
        "f1_l3_candidate_markers_removed": 2,           # from the L3 session
    }
    na.scrub_and_verify_generation_leak(result, canon)
    assert result["f1_final_seam_markers_removed"] == 0
    assert result["f1_scrub_operations_count"] == 6


def test_removed_count_sums_all_three_seams_when_each_contributed():
    canon = _v9_canon()
    result = {
        "book": "a straggler [anc3] made it this far",
        "chapters": [],
        "f1_generation_markers_removed": 3,
        "f1_l3_candidate_markers_removed": 1,
    }
    na.scrub_and_verify_generation_leak(result, canon)
    assert result["f1_final_seam_markers_removed"] == 1
    assert result["f1_scrub_operations_count"] == 5


def test_the_counter_is_named_and_decomposed_as_operations_not_delivered_markers():
    """🔴 THE NAME IS THE CONTRACT. This number is a count of scrub OPERATIONS, and it
    genuinely cannot be read as "markers in the delivered book": the L3 subtotal counts
    markers scrubbed from repair CANDIDATES that the validator may then reject, and this
    seam walks every string under `result`, so one leaked marker present in both the
    assembled book and its chapter row is two operations. It was previously called
    `f1_scrub_removed_count` and documented as the "delivered total" — a canary reader
    would have over-read it. Publishing the three subtotals beside it is what makes the
    number decomposable instead of merely smaller-or-larger than expected."""
    canon = _v9_canon()
    result = {
        "book": "the crisp [anc3] air",
        "chapters": [{"no": 1, "content": "the crisp [anc3] air"}],  # SAME marker, 2 strings
        "f1_generation_markers_removed": 2,
        "f1_l3_candidate_markers_removed": 5,   # attempts; some candidates were rejected
    }
    na.scrub_and_verify_generation_leak(result, canon)
    assert result["f1_final_seam_markers_removed"] == 2, \
        "one leaked marker in two representations is two operations -- that is the point"
    assert result["f1_scrub_operations_count"] == 9
    # every subtotal survives for decomposition, and the misleading old name is gone
    assert result["f1_generation_markers_removed"] == 2
    assert result["f1_l3_candidate_markers_removed"] == 5
    assert "f1_scrub_removed_count" not in result


def test_every_subtotal_reaches_the_persisted_payload():
    canon = _v9_canon()
    result = {
        "book": "the crisp [anc3] air", "chapters": [],
        "f1_generation_markers_removed": 1,
        "f1_l3_candidate_markers_removed": 3,
    }
    na.scrub_and_verify_generation_leak(result, canon)
    payload = na._result_payload(result)
    assert payload["f1_scrub_operations_count"] == 5
    assert payload["f1_generation_markers_removed"] == 1
    assert payload["f1_l3_candidate_markers_removed"] == 3
    assert payload["f1_final_seam_markers_removed"] == 1


def test_removed_count_tolerates_absent_upstream_subtotals():
    """Shadow/off never sets the generation key; assist without an L3 session never
    sets the L3 one. Absent must read as 0, not raise and not poison the total."""
    canon = _v9_canon()
    result = {"book": "the crisp [anc3] air", "chapters": []}
    na.scrub_and_verify_generation_leak(result, canon)
    assert result["f1_scrub_operations_count"] == 1


def test_removed_count_reaches_the_persisted_result_payload_including_zero():
    """`_result_payload` whitelists fields explicitly — a new `result` key is not
    automatically persisted/sent without being registered there."""
    canon = _v9_canon()
    result = {"book": "the crisp [anc3] early September air", "chapters": []}
    na.scrub_and_verify_generation_leak(result, canon)
    payload = na._result_payload(result)
    assert payload["f1_scrub_operations_count"] == 1

    clean_result = {"book": "clean", "chapters": []}
    na.scrub_and_verify_generation_leak(clean_result, canon)
    clean_payload = na._result_payload(clean_result)
    assert clean_payload["f1_scrub_operations_count"] == 0, \
        "0 is a real, meaningful value here and must not be dropped as falsy"


def test_removed_count_is_absent_from_the_payload_when_the_seam_never_ran():
    """Off mode (or any caller that never had a canon) must not gain this key at
    all — same 'added only when there is one' rule as canon_lite_binding/canon_lite_l3."""
    result = {"book": "isi", "chapters": []}
    payload = na._result_payload(result)
    assert "f1_scrub_operations_count" not in payload


def test_a_chapter_row_change_alone_also_invalidates_the_proof():
    """🔴 INVERTED 2026-08-15 (re-audit round 2). This test previously ASSERTED the
    defect: that a chapter-row-only scrub left `delivery_binding=MATCH` alive. It does
    not. `_persist_chapters` writes `narasi_chapters` from `chapters[*]["content"]`
    independently of the assembled book, `_l3_sync_chapter_records` rewrites BOTH as
    one all-or-nothing unit, and a reader can open either. The assembled book and the
    durable chapter rows are two representations of one delivered text — changing
    either after the verifier ran makes the old proof describe something else."""
    canon = _v9_canon()
    result = _l3_matched_result("clean assembled text")
    result["chapters"] = [{"no": 1, "content": "the crisp [anc3] early September air"}]
    verified_hash = result["canon_lite_l3"]["manuscript_sha256"]
    clear, leaked = na.scrub_and_verify_generation_leak(result, canon)
    assert "[anc3]" not in result["chapters"][0]["content"]
    assert result["book"] == "clean assembled text"     # the book really is untouched
    l3 = result["canon_lite_l3"]
    assert l3["delivery_binding"] != "MATCH"
    assert l3["manuscript_sha256"] == verified_hash, \
        "the VERIFIED hash must survive -- it describes what the verifier actually read"
    assert clear is False, "a proof invalidated this late is an invariant failure"


def test_a_late_change_never_overwrites_the_verifier_hash():
    """🔴 THE VERIFIER'S HASH IS NOT A SCRATCH FIELD. `manuscript_sha256` is
    `run.manuscript_sha256_after` — the hash of the bytes the continuity verdict was
    actually computed over. Replacing it with a post-scrub hash nothing verified
    produces telemetry that contradicts itself: a hash presented as verified next to
    a binding that says it is not. The post-scrub bytes get their OWN field."""
    import hashlib
    canon = _v9_canon()
    result = _l3_matched_result("the crisp [anc3] early September air")
    verified_hash = result["canon_lite_l3"]["manuscript_sha256"]
    na.scrub_and_verify_generation_leak(result, canon)
    l3 = result["canon_lite_l3"]
    assert l3["manuscript_sha256"] == verified_hash
    assert l3["delivered_manuscript_sha256"] == \
        hashlib.sha256(result["book"].encode("utf-8")).hexdigest()
    assert l3["delivered_manuscript_sha256"] != verified_hash


def test_a_late_change_marks_the_outcome_unresolved_not_resolved():
    """An outcome of `resolved` beside a binding of `UNPROVED` is the contradiction
    this closes: the repair may well have been computed correctly, but nothing
    verified the bytes now being delivered, so the run did not RESOLVE this book."""
    canon = _v9_canon()
    result = _l3_matched_result("the crisp [anc3] early September air")
    assert result["canon_lite_l3"]["outcome"] == "resolved"   # the pre-state
    na.scrub_and_verify_generation_leak(result, canon)
    assert result["canon_lite_l3"]["outcome"] == "unresolved"


def test_a_late_change_blocks_delivery_even_though_the_rescan_is_clean():
    """Brief §C.3: 'late manuscript mutation adalah invariant failure: block delivery
    kecuali verifier benar-benar dijalankan ulang terhadap final bytes.' The scrub
    SUCCEEDED here — the rescan finds nothing — and delivery is still refused, because
    both earlier defences (per-worker scrub, L3 candidate scrub) must have failed for
    a marker to reach this seam at all."""
    canon = _v9_canon()
    result = _l3_matched_result("the crisp [anc3] early September air")
    clear, leaked = na.scrub_and_verify_generation_leak(result, canon)
    assert leaked == (), "the scrub itself worked -- nothing is left"
    assert clear is False, "and delivery is still blocked: the proof no longer holds"
    assert result["f1_late_mutation_after_verification"] is True


def test_a_late_change_with_no_l3_proof_to_invalidate_still_delivers():
    """The mirror case. Shadow/off, or assist that never ran L3, has NO verified
    proof for a late scrub to invalidate — the scrub is simply F1's backstop doing
    its job, and blocking delivery there would fail clean books over nothing."""
    canon = _v9_canon()
    result = {"book": "the crisp [anc3] early September air", "chapters": []}
    clear, leaked = na.scrub_and_verify_generation_leak(result, canon)
    assert (clear, leaked) == (True, ())
    assert "[anc3]" not in result["book"]
    assert "f1_late_mutation_after_verification" not in result
