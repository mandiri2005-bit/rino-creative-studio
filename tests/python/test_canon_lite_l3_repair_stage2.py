"""L3-ASSIST Stage 2 — TARGETED REPAIR.

The acceptance rule is the whole module, so it is what this file is built around.
A candidate is accepted only when all three hold on the RE-EVALUATED book:

  1. every targeted violation for that chapter is gone;
  2. no new violation appeared, anywhere;
  3. no predicate became less measurable than it already was.

🔴 EACH ONE HAS A CANDIDATE THAT SATISFIES THE OTHER TWO AND FAILS IT. That is the
   only way to show the three are three, and not one condition written out three
   times: a valid-but-ineffective candidate passes (2) and (3), a
   regression-introducing candidate passes (1) and (3), and a coverage-destroying
   candidate passes (1) and (2) while leaving the defect in the book.

🔴 AND EVERY REJECTION IS CHECKED AT THE BYTES, not just at the outcome label. A
   run that reports `unresolved` while having quietly written the candidate into
   the manuscript would satisfy every assertion about outcomes.
"""
import asyncio
import dataclasses
import os
import pathlib
import sys
import types
import unicodedata

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import canon_lite as cl                       # noqa: E402
import canon_lite_l2 as l2                    # noqa: E402
import canon_lite_l3_repair as rp             # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures — one entity, two chapters, one contradiction in chapter 2.
# ---------------------------------------------------------------------------

GOOD_NAME = "Suranto"
BAD_NAME = "Hartono"
BOOK = (f"## Bab 1\nNamanya {GOOD_NAME} di desa itu.\n\n"
        f"## Bab 2\nNamanya {BAD_NAME} di kota itu.\n")


def _canon(*, entities=(), events=()):
    chapters = tuple(
        cl.CanonChapterV1(chapter_id=f"ch{i+1}", order=i + 1,
                          expected_title=cl.UNKNOWN)
        for i in range(2))
    return cl._finalize({
        "schema_version": cl.SCHEMA_VERSION,
        "outline_sha256": "a" * 64, "generation_config_sha256": "b" * 64,
        "target_language": "id", "chapters": chapters,
        "entities": tuple(entities), "anchors": (), "one_time_events": tuple(events),
        "reveals": (), "flashback_exceptions": (),
        "fact_source_policy": "fiction_generated",
        "advisory_bible_sha256": cl.UNKNOWN,
    })


def _entity_canon():
    return _canon(entities=(cl.CanonEntityV1(
        entity_id="e1", canonical_name=GOOD_NAME, aliases=(), alias_source="none"),))


def _coverage(entity_state=l2.COVERAGE_CHECKED):
    """Coverage is PER PREDICATE, and CHECKED is only honest where claims exist.

    Only `entity_mention` claims are produced here, so the other two predicates
    report NO_CLAIMS_FOUND — the schema rejects CHECKED with zero claims, and it
    is right to: "checked" with nothing to check is the absence-of-evidence
    laundering C7 exists to stop.
    """
    states = {
        l2.PREDICATE_ENTITY_NAME: entity_state,
        l2.PREDICATE_FIXED_LITERAL: l2.COVERAGE_NO_CLAIMS_FOUND,
        l2.PREDICATE_ONE_TIME_EVENT: l2.COVERAGE_NO_CLAIMS_FOUND,
    }
    return tuple(l2.PredicateCoverageV1(predicate=p, state=states[p])
                 for p in l2.SEMANTIC_PREDICATES)


def _claims_for(block_bytes, *, index, chapter_id, canon, needle,
                coverage_state=l2.COVERAGE_CHECKED, canon_ref="e1"):
    """One entity_mention claim pointing at `needle` inside this exact block."""
    start = block_bytes.find(needle.encode("utf-8"))
    assert start >= 0, (needle, block_bytes)
    end = start + len(needle.encode("utf-8"))
    claims = () if coverage_state != l2.COVERAGE_CHECKED else (l2.ObservedClaimV1(
        claim_type=l2.CLAIM_ENTITY_MENTION, canon_ref=canon_ref,
        evidence_start=start, evidence_end=end,
        evidence_sha256=cl.sha256_hex(block_bytes[start:end])),)
    return l2.ChapterClaimsV1(
        schema_version=l2.CLAIMS_SCHEMA_VERSION, chapter_index=index,
        chapter_id=chapter_id, content_sha256=cl.sha256_hex(block_bytes),
        canon_sha256=(canon.canon_sha256 if canon is not None else cl.UNKNOWN),
        atom_table_sha256=l2.build_chapter_atom_table(block_bytes)[1],
        extractor_version=cl.L2_EXTRACTOR_VERSION, model_version="m1",
        prompt_sha256="c" * 64, predicate_set_version=l2.PREDICATE_SET_VERSION,
        coverage=_coverage(coverage_state), claims=claims)


def _setup(book=BOOK, canon=None):
    canon = canon if canon is not None else _entity_canon()
    snap = l2.materialize_final_snapshot({"book": book})
    claims = {
        0: _claims_for(snap.block_bytes(0), index=0, chapter_id=snap.blocks[0].chapter_id,
                       canon=canon, needle=GOOD_NAME),
        1: _claims_for(snap.block_bytes(1), index=1, chapter_id=snap.blocks[1].chapter_id,
                       canon=canon, needle=BAD_NAME),
    }
    return snap, canon, claims, {"book": book}


def _fixed_block(snap, index=1):
    """Chapter 2 with the contradicted name corrected — a genuine targeted repair."""
    return snap.block_bytes(index).replace(BAD_NAME.encode(), GOOD_NAME.encode())


class _Extractor:
    """Honest extractor: re-reads whatever bytes it is handed."""

    def __init__(self, needle=GOOD_NAME, *, coverage_state=l2.COVERAGE_CHECKED,
                 override=None):
        self.calls = []
        self._needle = needle
        self._coverage = coverage_state
        self._override = override or {}

    async def __call__(self, *, chapter_index, chapter_id, block_bytes, canon, attempt):
        self.calls.append(chapter_index)
        needle = self._needle if self._needle.encode() in block_bytes else BAD_NAME
        art = _claims_for(block_bytes, index=chapter_index, chapter_id=chapter_id,
                          canon=canon, needle=needle,
                          coverage_state=self._coverage)
        if self._override:
            art = l2.ChapterClaimsV1(**{
                **{f: getattr(art, f) for f in art.__dataclass_fields__},
                **self._override})
        return art


class _Provider:
    """Returns a scripted candidate per attempt, and counts every call."""

    def __init__(self, *candidates):
        self.calls = []
        self._candidates = list(candidates)

    async def __call__(self, *, chapter_index, chapter_id, block_bytes,
                       violation_codes, canon, attempt):
        self.calls.append((chapter_index, attempt))
        value = self._candidates[min(attempt - 1, len(self._candidates) - 1)]
        return value(block_bytes) if callable(value) else value


def _run(snap, canon, claims, result, provider, extractor, **kw):
    return asyncio.run(rp.repair_manuscript(
        snap, canon, mode="assist", result=result, claims_by_index=claims,
        repair_provider=provider, extract_chapter=extractor, **kw))


# ── 0. the fixture really does produce a repairable violation ───────────────
def test_the_baseline_has_exactly_one_repairable_target():
    snap, canon, claims, result = _setup()
    report = l2.build_report(snap, canon, mode="assist", result=result,
                             claims_by_index=claims)
    assert report.continuity_status == l2.STATUS_VIOLATIONS
    assert rp.repairable_targets(report) == {1: (l2.VIOLATION_ENTITY_NAME,)}


# ── 1. the happy path, end to end ───────────────────────────────────────────
def test_a_genuine_targeted_repair_is_accepted_and_rebinds_the_manuscript():
    snap, canon, claims, result = _setup()
    run = _run(snap, canon, claims, result,
               _Provider(lambda b: _fixed_block(snap)), _Extractor())

    assert run.chapters[0].outcome == rp.OUTCOME_REPAIRED
    assert run.chapters[0].last_reason == rp.REASON_NONE
    assert run.chapters[0].iterations == 1
    assert run.rounds == 1
    assert run.changed
    # The bytes really moved, and the hash is of bytes that exist.
    assert BAD_NAME not in run.repaired_manuscript.decode()
    assert cl.sha256_hex(run.repaired_manuscript) == run.manuscript_sha256_after
    assert run.report.manuscript_sha256 == run.manuscript_sha256_after
    assert run.report.repair_rounds == 1
    # ...and the re-check no longer sees the violation.
    assert not any(v for r in run.report.results for v in r.violations
                   if v.code == l2.VIOLATION_ENTITY_NAME)


def test_verify_applied_is_what_proves_the_repair_was_delivered():
    """Every hash in the run describes what the engine produced. Only this reads
    the delivery path."""
    snap, canon, claims, result = _setup()
    run = _run(snap, canon, claims, result,
               _Provider(lambda b: _fixed_block(snap)), _Extractor())
    assert rp.verify_applied(run, result) == l2.BINDING_MISMATCH   # not substituted
    assert rp.verify_applied(
        run, {"book": run.repaired_manuscript.decode()}) == l2.BINDING_MATCH


# ── 2. condition (1): valid, well-formed, and fixes nothing ─────────────────
def test_a_valid_but_ineffective_candidate_is_rejected():
    """🔴 PASSES (2) AND (3) PERFECTLY. It introduces no violation and destroys no
    coverage — it simply leaves the contradiction where it was. Accepting on
    'nothing got worse' would bank a repair that repaired nothing, and the book
    would ship with the defect and a `repaired` label on it."""
    snap, canon, claims, result = _setup()
    # Edits prose, keeps the wrong name.
    ineffective = snap.block_bytes(1).replace(b"di kota itu", b"di kota besar")
    run = _run(snap, canon, claims, result,
               _Provider(lambda b: ineffective), _Extractor())

    rec = run.chapters[0]
    assert rec.outcome == rp.OUTCOME_UNRESOLVED
    assert rec.last_reason == rp.REASON_INEFFECTIVE
    assert not rec.accepted
    assert rec.after_sha256 == rec.before_sha256
    assert not run.changed
    assert run.repaired_manuscript == snap.manuscript_bytes
    assert BAD_NAME in run.repaired_manuscript.decode()


# ── 3. condition (2): fixes the target, breaks something else ───────────────
def test_a_candidate_that_introduces_a_new_violation_is_rejected():
    """🔴 PASSES (1) AND (3). The targeted entity violation is genuinely gone and
    every predicate is still measured — but the fix contradicted a fixed literal
    that was fine before. Judging only the code we set out to repair would call
    this a success and hand the reader a differently broken book.

    The new violation is a DIFFERENT CODE on purpose. Violations are identified
    by (code, predicate, chapter_index), so a "new" violation with the same code
    in the same chapter is indistinguishable from the original one surviving —
    and that case is condition (1)'s job, not condition (2)'s.
    """
    canon = cl._finalize({
        "schema_version": cl.SCHEMA_VERSION, "outline_sha256": "a" * 64,
        "generation_config_sha256": "b" * 64, "target_language": "id",
        "chapters": tuple(
            cl.CanonChapterV1(chapter_id=f"ch{i+1}", order=i + 1,
                              expected_title=cl.UNKNOWN) for i in range(2)),
        "entities": (cl.CanonEntityV1(entity_id="e1", canonical_name=GOOD_NAME,
                                      aliases=(), alias_source="none"),),
        "anchors": (cl.CanonAnchorV1(anchor_id="a1", kind="time",
                                     literal="tahun 1830"),),
        "one_time_events": (), "reveals": (), "flashback_exceptions": (),
        "fact_source_policy": "fiction_generated",
        "advisory_bible_sha256": cl.UNKNOWN,
    })
    snap, canon, claims, result = _setup(canon=canon)
    fixed = _fixed_block(snap)

    class _Breaking(_Extractor):
        async def __call__(self, *, chapter_index, chapter_id, block_bytes, canon, attempt):
            self.calls.append(chapter_index)
            base = _claims_for(block_bytes, index=chapter_index,
                               chapter_id=chapter_id, canon=canon, needle=GOOD_NAME)
            # The entity is right now; but the chapter is reported as asserting a
            # date literal that contradicts the anchor.
            start = block_bytes.find(b"kota")
            extra = l2.ObservedClaimV1(
                claim_type=l2.CLAIM_FIXED_LITERAL, canon_ref="a1",
                evidence_start=start, evidence_end=start + 4,
                evidence_sha256=cl.sha256_hex(block_bytes[start:start + 4]))
            coverage = tuple(
                l2.PredicateCoverageV1(predicate=c.predicate,
                                       state=l2.COVERAGE_CHECKED)
                if c.predicate == l2.PREDICATE_FIXED_LITERAL else c
                for c in base.coverage)
            return l2.ChapterClaimsV1(**{
                **{f: getattr(base, f) for f in base.__dataclass_fields__},
                "claims": base.claims + (extra,), "coverage": coverage})

    run = _run(snap, canon, claims, result, _Provider(lambda b: fixed), _Breaking())
    rec = run.chapters[0]
    assert rec.outcome == rp.OUTCOME_UNRESOLVED
    assert rec.last_reason == rp.REASON_REGRESSED
    assert not run.changed
    assert run.repaired_manuscript == snap.manuscript_bytes
    assert BAD_NAME in run.repaired_manuscript.decode()


# ── 4. condition (3): fixes the target by making it unmeasurable ────────────
def test_a_candidate_that_regresses_coverage_is_rejected():
    """🔴 PASSES (1) AND (2), AND IS THE WORST OF THE THREE. The violation
    disappears from the report because the predicate stopped being measurable at
    all — not because the contradiction left the book. Violation counts go down,
    which is exactly what a naive success check looks at."""
    snap, canon, claims, result = _setup()
    run = _run(snap, canon, claims, result,
               _Provider(lambda b: _fixed_block(snap)),
               _Extractor(coverage_state=l2.COVERAGE_TIMEOUT))

    rec = run.chapters[0]
    assert rec.outcome == rp.OUTCOME_UNRESOLVED
    assert rec.last_reason == rp.REASON_COVERAGE_REGRESSED
    assert not run.changed
    assert run.repaired_manuscript == snap.manuscript_bytes


# ── 5. the extractor must be answering about THIS chapter ───────────────────
@pytest.mark.parametrize("field,value", [
    ("chapter_index", 0),
    ("chapter_id", "ch1"),
    ("content_sha256", "d" * 64),
    ("canon_sha256", "e" * 64),
])
def test_claims_that_do_not_bind_the_candidate_are_refused(field, value):
    """🔴 ALL FOUR FIELDS, NOT JUST THE CONTENT HASH. An extractor answering about
    a different chapter, or under a different canon, has answered a question
    nobody asked — and its verdict would be spliced in as if it were about the
    chapter being repaired."""
    snap, canon, claims, result = _setup()
    run = _run(snap, canon, claims, result,
               _Provider(lambda b: _fixed_block(snap)),
               _Extractor(override={field: value}))

    rec = run.chapters[0]
    assert rec.outcome == rp.OUTCOME_UNRESOLVED
    assert rec.last_reason == rp.REASON_EXTRACTOR_IDENTITY, field
    assert not run.changed


# ── 6. two attempts, and never a third ──────────────────────────────────────
def test_a_second_attempt_runs_after_the_first_is_rejected():
    snap, canon, claims, result = _setup()
    ineffective = snap.block_bytes(1).replace(b"di kota itu", b"di kota besar")
    provider = _Provider(lambda b: ineffective, lambda b: _fixed_block(snap))
    run = _run(snap, canon, claims, result, provider, _Extractor())

    assert [a for _, a in provider.calls] == [1, 2]
    assert run.chapters[0].outcome == rp.OUTCOME_REPAIRED
    assert run.chapters[0].iterations == 2
    assert run.rounds == 2
    assert run.changed


def test_a_third_attempt_is_impossible():
    """🔴 THE CEILING IS THE LOOP BOUND, and this is what says so. Two failures
    and the original bytes stand — no third call, no matter how close the second
    came."""
    snap, canon, claims, result = _setup()
    ineffective = snap.block_bytes(1).replace(b"di kota itu", b"di kota besar")
    provider = _Provider(lambda b: ineffective)      # always ineffective
    run = _run(snap, canon, claims, result, provider, _Extractor())

    assert len(provider.calls) == rp.MAX_REPAIR_ITERATIONS == 2, provider.calls
    assert [a for _, a in provider.calls] == [1, 2]
    rec = run.chapters[0]
    assert rec.iterations == 2
    assert rec.outcome == rp.OUTCOME_UNRESOLVED
    assert rec.last_reason == rp.REASON_INEFFECTIVE
    assert not run.changed
    assert run.repaired_manuscript == snap.manuscript_bytes
    assert run.rounds <= rp.MAX_REPAIR_ITERATIONS


def test_the_report_ceiling_and_the_engine_ceiling_are_one_number():
    """🔴 ASSERTED AT THE SOURCE, BECAUSE THE VALUE COMPARISON CANNOT SEE IT.
    CPython interns small ints, so a hard-coded `2` here would satisfy both `==`
    and `is` against the imported 2 — the two ceilings would be separate numbers
    that happen to agree today, which is exactly the drift this guards."""
    assert rp.MAX_REPAIR_ITERATIONS == l2.MAX_REPAIR_ROUNDS
    source = (pathlib.Path(__file__).resolve().parents[2] / "python" /
              "canon_lite_l3_repair.py").read_text()
    assert "MAX_REPAIR_ITERATIONS = _l2.MAX_REPAIR_ROUNDS" in source, \
        "the engine ceiling is not imported from the report's ceiling"


def test_coverage_regression_detects_both_of_its_two_shapes():
    """The two clauses catch different failures, so each needs its own case.

    A predicate can regress by becoming UNMEASURABLE at the same unit count, or
    by measuring fewer units while still reporting a measured state. One clause
    is blind to each.
    """
    def _report(state, checked):
        return types.SimpleNamespace(results=(l2.PredicateResultV1(
            predicate=l2.PREDICATE_ENTITY_NAME, scope=l2.SCOPE_CHAPTER,
            coverage_state=state, checked_units=checked, total_units=2,
            violations=()),))

    measured = _report(l2.COVERAGE_NO_VIOLATIONS_FOUND, 2)
    # (a) same units, no longer measurable.
    assert rp._coverage_regressed(measured, _report(l2.COVERAGE_TIMEOUT, 2))
    # (b) still measurable, fewer units.
    assert rp._coverage_regressed(
        measured, _report(l2.COVERAGE_NO_VIOLATIONS_FOUND, 1))
    # ...and no regression is no regression.
    assert not rp._coverage_regressed(
        measured, _report(l2.COVERAGE_NO_VIOLATIONS_FOUND, 2))


# ── 7. the minimal-edit boundary, at the exact numbers ──────────────────────
@pytest.mark.parametrize("ratio,expected", [
    (149_999, False),
    (150_000, False),      # exactly at the limit is INSIDE it
    (150_001, True),       # one ppm over is out
])
def test_the_guard_boundary_is_strictly_greater(ratio, expected):
    assert rp.exceeds_edit_guard(ratio, rp.DEFAULT_MAX_EDIT_RATIO_PPM) is expected


def test_a_candidate_exactly_at_the_boundary_is_accepted_and_one_over_is_not():
    """The same candidate, judged against a limit at and just below its ratio."""
    snap, canon, claims, result = _setup()
    fixed = _fixed_block(snap)
    ratio = rp.edit_ratio_ppm(snap.block_bytes(1), fixed)
    assert 0 < ratio < 1_000_000

    at_limit = _run(snap, canon, claims, result, _Provider(lambda b: fixed),
                    _Extractor(), max_edit_ratio_ppm=ratio)
    assert at_limit.chapters[0].outcome == rp.OUTCOME_REPAIRED
    assert at_limit.chapters[0].edit_ratio_ppm == ratio

    one_under = _run(snap, canon, claims, result, _Provider(lambda b: fixed),
                     _Extractor(), max_edit_ratio_ppm=ratio - 1)
    assert one_under.chapters[0].outcome == rp.OUTCOME_UNRESOLVED
    assert one_under.chapters[0].last_reason == rp.REASON_GUARD_REJECTED
    assert one_under.chapters[0].edit_ratio_ppm == 0    # nothing was edited
    assert not one_under.changed


def test_the_ratio_is_measured_in_nfc_characters_not_bytes():
    """🔴 BYTES WOULD PENALISE NON-ASCII PROSE. The same one-character edit costs
    more bytes in accented Indonesian or in CJK than in ASCII, so a byte ratio
    makes the guard tighter for exactly the prose this product ships.

    Both cases below are chosen so a byte measurement gives a DIFFERENT answer.
    A uniform-width script would not discriminate: 1 char of 4 and 3 bytes of 12
    are the same ratio, and a byte implementation would pass.
    """
    # (a) MIXED WIDTH: 3 ASCII + 1 CJK = 4 characters but 6 bytes. Replacing the
    #     CJK char is 1/4 of the characters and 3/6 of the bytes.
    mixed_before, mixed_after = "abc第".encode(), "abc二".encode()
    assert len(mixed_before) == 6 and len("abc第") == 4
    assert rp.edit_ratio_ppm(mixed_before, mixed_after) == \
        rp.edit_ratio_ppm(b"abcd", b"abce") == 250_000

    # (b) NFD vs NFC: the SAME text in two normal forms. Zero characters differ;
    #     the byte sequences are not even the same length.
    nfd, nfc = "café".encode(), "café".encode()
    assert nfd != nfc
    assert rp.edit_ratio_ppm(nfd, nfc) == 0, \
        "a pure normalisation difference was charged as an edit"


# ── 8. structure is never a target ──────────────────────────────────────────
def test_a_title_mismatch_is_never_targeted_for_repair():
    """🔴 THE TEMPTING ONE: it names a single chapter and looks trivially
    fixable. It is a STRUCTURAL finding — the manuscript does not match the
    accepted outline — and rewriting prose until the symptom clears would hide an
    assembly defect behind a `clean` report."""
    book = "## Bab Salah\nisi satu.\n\n## Bab Dua\nisi dua.\n"
    canon = _canon()
    snap = l2.materialize_final_snapshot({"book": book})
    canon_titled = cl._finalize({
        "schema_version": cl.SCHEMA_VERSION, "outline_sha256": "a" * 64,
        "generation_config_sha256": "b" * 64, "target_language": "id",
        "chapters": tuple(
            cl.CanonChapterV1(chapter_id=f"ch{i+1}", order=i + 1,
                              expected_title=t)
            for i, t in enumerate(["Bab Benar", "Bab Dua"])),
        "entities": (), "anchors": (), "one_time_events": (), "reveals": (),
        "flashback_exceptions": (), "fact_source_policy": "fiction_generated",
        "advisory_bible_sha256": cl.UNKNOWN,
    })
    report = l2.build_report(snap, canon_titled, mode="assist",
                             result={"book": book})
    codes = {v.code for r in report.results for v in r.violations}
    assert l2.VIOLATION_CHAPTER_TITLE in codes, "the fixture produced no title mismatch"
    assert rp.repairable_targets(report) == {}, \
        "repair targeted a structure violation"

    provider = _Provider(lambda b: b)
    run = _run(snap, canon_titled, {}, {"book": book}, provider, _Extractor())
    assert provider.calls == [], "a structure violation reached the repair provider"
    assert run.chapters == () and run.rounds == 0
    assert not run.changed


def test_a_document_global_semantic_violation_is_not_routed_to_a_chapter():
    """🔴 `chapter_index == -1` NAMES NO CHAPTER TO REWRITE.

    Structure violations are filtered by predicate, so that filter alone would
    hide this one: a SEMANTIC violation reported document-globally would be
    routed to chapter -1 and the engine would index past the end of the block
    list. The two filters catch different things and both have to be there.
    """
    snap, canon, claims, result = _setup()
    report = l2.build_report(snap, canon, mode="assist", result=result,
                             claims_by_index=claims)
    globalised = dataclasses.replace(report, results=tuple(
        dataclasses.replace(r, violations=tuple(
            dataclasses.replace(v, chapter_index=-1) for v in r.violations))
        for r in report.results))
    assert any(v.chapter_index == -1 for r in globalised.results
               for v in r.violations), "the fixture built no global violation"
    assert rp.repairable_targets(globalised) == {}, \
        "a document-global violation was routed to a chapter"


# ── 9. enforce is refused, not quietly treated as assist ────────────────────
def test_enforce_mode_is_refused():
    """L3-ENFORCE is a separate deferred project. An enforce caller must find out
    this engine has no enforce behaviour, not receive assist behaviour under an
    enforce label."""
    snap, canon, claims, result = _setup()
    provider = _Provider(lambda b: _fixed_block(snap))
    with pytest.raises(rp.RepairError) as excinfo:
        asyncio.run(rp.repair_manuscript(
            snap, canon, mode="enforce", result=result, claims_by_index=claims,
            repair_provider=provider, extract_chapter=_Extractor()))
    # 🔴 THE REFUSAL MUST BE THE SPECIFIC ONE. Falling through to the generic
    #    "not in REPAIR_MODES" enum error would still refuse, and would still
    #    mention enforce — while losing the one thing the caller needs to know:
    #    that L3-ENFORCE is deferred, not merely unlisted.
    assert "L3-ENFORCE" in str(excinfo.value), str(excinfo.value)
    assert provider.calls == [], "enforce reached the repair provider before refusing"


def test_shadow_mode_is_refused_too():
    snap, canon, claims, result = _setup()
    with pytest.raises(rp.RepairError):
        asyncio.run(rp.repair_manuscript(
            snap, canon, mode="shadow", result=result, claims_by_index=claims,
            repair_provider=_Provider(lambda b: b), extract_chapter=_Extractor()))


def test_a_report_may_never_carry_repair_rounds_under_shadow():
    """The schema half of the same rule — the engine refusing shadow is not much
    use if a shadow report can still claim it repaired something."""
    snap = l2.materialize_final_snapshot({"book": BOOK})
    with pytest.raises(Exception):
        l2.build_report(snap, None, mode="shadow", result={"book": BOOK},
                        repair_rounds=1)


# ── 10. provider faults are outcomes, never exceptions ──────────────────────
def test_a_provider_that_raises_leaves_the_chapter_untouched():
    class _Boom(_Provider):
        async def __call__(self, **kw):
            self.calls.append((kw["chapter_index"], kw["attempt"]))
            raise RuntimeError("provider exploded")

    snap, canon, claims, result = _setup()
    provider = _Boom()
    run = _run(snap, canon, claims, result, provider, _Extractor())
    rec = run.chapters[0]
    assert rec.outcome == rp.OUTCOME_UNRESOLVED
    assert rec.last_reason == rp.REASON_PROVIDER_FAILED
    assert len(provider.calls) == 1, "a raising provider was retried"
    assert not run.changed


def test_a_raising_reextractor_is_distinguished_from_a_raising_repair_provider():
    """🔴 THE SPLIT THIS COMMIT EXISTS FOR. Live canary `19444d6` reported
    `unresolved_reasons: {'provider_failed': 3}` on all three chapters while the repair
    GENERATION call had not failed at all — the failure was in RE-EXTRACTING the
    candidate to verify it, a different subsystem entirely. Before this fix both raised
    the identical `REASON_PROVIDER_FAILED`, so nothing distinguished "the writer failed"
    from "the checker failed"."""
    class _BoomExtractor(_Extractor):
        async def __call__(self, **kw):
            self.calls.append(kw["chapter_index"])
            raise RuntimeError("qc_provider_timeout")  # simulates a real bounded fault

    snap, canon, claims, result = _setup()
    extractor = _BoomExtractor()
    run = _run(snap, canon, claims, result, _Provider(lambda b: _fixed_block(snap)),
              extractor)
    rec = run.chapters[0]

    assert rec.outcome == rp.OUTCOME_UNRESOLVED
    assert rec.last_reason == rp.REASON_REEXTRACTION_FAILED, (
        f"got {rec.last_reason!r} — a re-extraction fault must not collapse into the "
        f"generic {rp.REASON_PROVIDER_FAILED!r} the repair-GENERATION path uses")
    assert rec.last_reason != rp.REASON_PROVIDER_FAILED
    assert len(extractor.calls) == 1, "a raising extractor was retried"
    assert not run.changed


@pytest.mark.parametrize("bad", [None, "", b"", b"   ", "not bytes",
                                 b"\xff\xfe not utf8", b"## Bab Lain\nisi.\n"])
def test_an_unusable_candidate_is_rejected_without_touching_the_bytes(bad):
    """The last case is the important one: a candidate that rewrites the HEADING
    is a structural edit arriving through the semantic door."""
    snap, canon, claims, result = _setup()
    run = _run(snap, canon, claims, result, _Provider(bad), _Extractor())
    rec = run.chapters[0]
    assert rec.outcome == rp.OUTCOME_UNRESOLVED
    assert rec.last_reason == rp.REASON_INVALID_CANDIDATE
    assert not run.changed
    assert run.repaired_manuscript == snap.manuscript_bytes


@pytest.mark.parametrize(
    ("bad", "stage"),
    [
        (None, "pre.type"),
        ("not bytes", "pre.type"),
        (b"   ", "pre.empty"),
        (b"\xff\xfe", "pre.utf8"),
        (b"## Bab Lain\nisi.\n", "pre.heading"),
    ],
)
def test_invalid_candidate_pre_subtype_maps_one_to_one_to_validator_branch(caplog, bad, stage):
    snap, canon, claims, result = _setup()
    with caplog.at_level("WARNING", logger="canon_lite_l3_repair"):
        run = _run(snap, canon, claims, result, _Provider(bad), _Extractor())
    assert run.chapters[0].last_reason == rp.REASON_INVALID_CANDIDATE
    rows = [record.message for record in caplog.records if "l3 invalid candidate" in record.message]
    assert rows
    assert all(f"stage={stage}" in row for row in rows)
    assert all("chapter_index=1" in row and "attempt=" in row for row in rows)
    assert all(GOOD_NAME not in row and BAD_NAME not in row for row in rows)


def test_invalid_candidate_stage_vocabulary_is_closed():
    with pytest.raises(AssertionError, match="unknown invalid-candidate stage"):
        rp._log_invalid_candidate(
            "post.typo", chapter_index=0, attempt=1,
            original=b"## Bab 1\ntext.\n", candidate=b"## Bab 1\nchanged.\n")


@pytest.mark.parametrize(
    "stage", ["post.materialize", "post.block_count", "post.chapter_identity"])
def test_invalid_candidate_post_subtypes_map_to_real_materialization_branches(
        monkeypatch, caplog, stage):
    snap, canon, claims, result = _setup()
    real_materialize = rp._l2.materialize_final_snapshot

    def bad_materialize(*args, **kwargs):
        if stage == "post.materialize":
            raise ValueError("bounded synthetic failure")
        staged = real_materialize(*args, **kwargs)
        assert staged is not None
        if stage == "post.block_count":
            return types.SimpleNamespace(blocks=staged.blocks[:-1])
        wrong = tuple(
            types.SimpleNamespace(chapter_id=f"wrong-{index}")
            for index, _block in enumerate(staged.blocks)
        )
        return types.SimpleNamespace(blocks=wrong)

    monkeypatch.setattr(rp._l2, "materialize_final_snapshot", bad_materialize)
    with caplog.at_level("WARNING", logger="canon_lite_l3_repair"):
        run = _run(
            snap, canon, claims, result,
            _Provider(lambda _b: _fixed_block(snap)), _Extractor())
    assert run.chapters[0].last_reason == rp.REASON_INVALID_CANDIDATE
    assert not run.changed
    rows = [record.message for record in caplog.records
            if "l3 invalid candidate" in record.message]
    assert rows and all(f"stage={stage}" in row for row in rows)


# ── 11. bookkeeping that must not be able to lie ────────────────────────────
def test_a_rejected_chapter_reports_a_zero_edit_ratio():
    """A non-zero ratio on a rejected repair would describe an edit that never
    reached the manuscript."""
    snap, canon, claims, result = _setup()
    run = _run(snap, canon, claims, result,
               _Provider(lambda b: _fixed_block(snap)),
               _Extractor(), max_edit_ratio_ppm=1)
    assert run.chapters[0].edit_ratio_ppm == 0
    assert run.chapters[0].before_sha256 == run.chapters[0].after_sha256


def test_nothing_to_repair_leaves_everything_exactly_as_it_was():
    snap, canon, claims, result = _setup(canon=_canon())     # no semantic authority
    provider = _Provider(lambda b: b"nope")
    run = _run(snap, canon, claims, result, provider, _Extractor())
    assert provider.calls == []
    assert run.chapters == ()
    assert run.rounds == 0
    assert run.manuscript_sha256_after == run.manuscript_sha256_before
    assert run.repaired_manuscript == snap.manuscript_bytes
    assert run.report.repair_rounds == 0
    assert run.report.affected_chapter_ids == ()


def test_telemetry_is_hashes_counts_and_bounded_labels_only():
    snap, canon, claims, result = _setup()
    run = _run(snap, canon, claims, result,
               _Provider(lambda b: _fixed_block(snap)), _Extractor())
    telemetry = rp.run_telemetry(run)
    blob = repr(telemetry)
    for leak in (GOOD_NAME, BAD_NAME, "Namanya", "desa", "kota"):
        assert leak not in blob, f"{leak!r} leaked into telemetry"
    assert telemetry["chapters_repaired"] == 1
    assert telemetry["rounds"] == 1

# ── 12. the book MOVES while it is being repaired ───────────────────────────
#
# 🔴 A FROZEN TARGET LIST AND A FROZEN BASELINE ARE WRONG IN BOTH DIRECTIONS, and
#    both directions are cheap to miss because the run still reports success.

def _multi_claims(block_bytes, *, index, chapter_id, canon, specs):
    """specs = [(claim_type, canon_ref, needle)]. Coverage follows the claims."""
    claims = []
    for claim_type, ref, needle in specs:
        start = block_bytes.find(needle.encode("utf-8"))
        assert start >= 0, (needle, block_bytes)
        end = start + len(needle.encode("utf-8"))
        claims.append(l2.ObservedClaimV1(
            claim_type=claim_type, canon_ref=ref, evidence_start=start,
            evidence_end=end,
            evidence_sha256=cl.sha256_hex(block_bytes[start:end])))
    present = {l2._CLAIM_PREDICATE[c.claim_type] for c in claims}
    coverage = tuple(
        l2.PredicateCoverageV1(
            predicate=p,
            state=(l2.COVERAGE_CHECKED if p in present
                   else l2.COVERAGE_NO_CLAIMS_FOUND))
        for p in l2.SEMANTIC_PREDICATES)
    return l2.ChapterClaimsV1(
        schema_version=l2.CLAIMS_SCHEMA_VERSION, chapter_index=index,
        chapter_id=chapter_id, content_sha256=cl.sha256_hex(block_bytes),
        canon_sha256=(canon.canon_sha256 if canon is not None else cl.UNKNOWN),
        atom_table_sha256=l2.build_chapter_atom_table(block_bytes)[1],
        extractor_version=cl.L2_EXTRACTOR_VERSION, model_version="m1",
        prompt_sha256="c" * 64, predicate_set_version=l2.PREDICATE_SET_VERSION,
        coverage=coverage, claims=tuple(claims))


class _Scripted:
    """Per-chapter extractor script, so each chapter's re-read can differ."""

    def __init__(self, script):
        self.calls = []
        self._script = script

    async def __call__(self, *, chapter_index, chapter_id, block_bytes, canon, attempt):
        self.calls.append(chapter_index)
        return self._script[chapter_index](block_bytes, chapter_id, canon)


EVENT_BOOK = ("## Bab 1\nNamanya Hartono saat gerhana tiba.\n\n"
              "## Bab 2\nNamanya Suranto saat gerhana usai.\n")


def _event_canon():
    return cl._finalize({
        "schema_version": cl.SCHEMA_VERSION, "outline_sha256": "a" * 64,
        "generation_config_sha256": "b" * 64, "target_language": "id",
        "chapters": tuple(
            cl.CanonChapterV1(chapter_id=f"ch{i+1}", order=i + 1,
                              expected_title=cl.UNKNOWN) for i in range(2)),
        "entities": (cl.CanonEntityV1(entity_id="e1", canonical_name=GOOD_NAME,
                                      aliases=(), alias_source="none"),),
        "anchors": (),
        "one_time_events": (cl.CanonEventV1(event_id="ev1", occurs_chapter_order=1,
                                            label="pintu pecah"),),
        "reveals": (), "flashback_exceptions": (),
        "fact_source_policy": "fiction_generated",
        "advisory_bible_sha256": cl.UNKNOWN,
    })


def test_a_repair_that_settles_a_later_chapter_stops_it_being_repaired():
    """🔴 REPAIR A REMOVES CHAPTER B'S VIOLATION, SO B MUST NOT BE TOUCHED.

    Chapter 2's one-time event is a duplicate only while chapter 1 also tells it.
    Fix chapter 1 and chapter 2 becomes the FIRST telling — its violation is gone
    without anyone editing it. Against a frozen target list the provider is still
    paid to repair chapter 2, and against a frozen violation set any edit it
    returns satisfies "the targeted violation is gone" for free: an arbitrary
    rewrite lands in the book, recorded as a repair.
    """
    canon = _event_canon()
    snap = l2.materialize_final_snapshot({"book": EVENT_BOOK})
    claims = {
        0: _multi_claims(snap.block_bytes(0), index=0,
                         chapter_id=snap.blocks[0].chapter_id, canon=canon,
                         specs=[(l2.CLAIM_ENTITY_MENTION, "e1", BAD_NAME),
                                (l2.CLAIM_ONE_TIME_EVENT, "ev1", "gerhana")]),
        1: _multi_claims(snap.block_bytes(1), index=1,
                         chapter_id=snap.blocks[1].chapter_id, canon=canon,
                         specs=[(l2.CLAIM_ENTITY_MENTION, "e1", GOOD_NAME),
                                (l2.CLAIM_ONE_TIME_EVENT, "ev1", "gerhana")]),
    }
    result = {"book": EVENT_BOOK}
    baseline = l2.build_report(snap, canon, mode="assist", result=result,
                               claims_by_index=claims)
    assert rp.repairable_targets(baseline) == {
        0: (l2.VIOLATION_ENTITY_NAME,), 1: (l2.VIOLATION_ONE_TIME_EVENT,)}, \
        "the fixture did not produce a target in each chapter"

    # Repairing chapter 1 removes its telling of the event along with the name.
    fixed0 = snap.block_bytes(0).replace(BAD_NAME.encode(), GOOD_NAME.encode()) \
                                .replace(" saat gerhana tiba".encode(), b"")
    arbitrary1 = snap.block_bytes(1).replace(b"di", b"DI")

    provider = _Provider(lambda b: fixed0 if BAD_NAME.encode() in b else arbitrary1)
    extractor = _Scripted({
        0: lambda b, cid, c: _multi_claims(
            b, index=0, chapter_id=cid, canon=c,
            specs=[(l2.CLAIM_ENTITY_MENTION, "e1", GOOD_NAME)]),
        1: lambda b, cid, c: _multi_claims(
            b, index=1, chapter_id=cid, canon=c,
            specs=[(l2.CLAIM_ENTITY_MENTION, "e1", GOOD_NAME),
                   (l2.CLAIM_ONE_TIME_EVENT, "ev1", "gerhana")]),
    })

    run = asyncio.run(rp.repair_manuscript(
        snap, canon, mode="assist", result=result, claims_by_index=claims,
        repair_provider=provider, extract_chapter=extractor,
        # Deleting a clause is a large fraction of a two-line fixture chapter. The
        # guard has its own controls; widening it here keeps this test about the
        # one property it is for.
        max_edit_ratio_ppm=1_000_000))

    assert [i for i, _ in provider.calls] == [0], (
        f"the provider was paid to repair a chapter whose violation was already "
        f"gone: {provider.calls}")
    by_index = {r.chapter_index: r for r in run.chapters}
    assert by_index[0].outcome == rp.OUTCOME_REPAIRED
    assert by_index[1].outcome == rp.OUTCOME_NOTHING_TO_REPAIR
    assert by_index[1].iterations == 0 and by_index[1].attempted_codes == ()
    # ...and the arbitrary edit never reached the book.
    assert b"DI" not in run.repaired_manuscript
    assert by_index[1].after_sha256 == by_index[1].before_sha256


COVER_BOOK = ("## Bab 1\nNamanya Hartono di pasar.\n\n"
              "## Bab 2\nNamanya Hartono di pantai.\n")


def test_a_second_repair_may_not_spend_coverage_the_first_recovered():
    """🔴 THE REGRESSION A FROZEN BASELINE CANNOT SEE.

    At the start the one-time-event predicate is unmeasurable — chapter 1 cites an
    event that is not in the canon. Repairing chapter 1 drops that citation and
    the predicate becomes measurable. Chapter 2's candidate then re-introduces
    one, dragging coverage back down to where it began.

    Judged against the CURRENT book that is a plain regression. Judged against the
    ORIGINAL report it is invisible — the predicate was unmeasured there too — so
    the candidate is accepted and the repair pass hands back exactly the coverage
    it had just earned.
    """
    canon = _event_canon()
    snap = l2.materialize_final_snapshot({"book": COVER_BOOK})
    claims = {
        0: _multi_claims(snap.block_bytes(0), index=0,
                         chapter_id=snap.blocks[0].chapter_id, canon=canon,
                         specs=[(l2.CLAIM_ENTITY_MENTION, "e1", BAD_NAME),
                                (l2.CLAIM_ONE_TIME_EVENT, "ev9", "pasar")]),
        1: _multi_claims(snap.block_bytes(1), index=1,
                         chapter_id=snap.blocks[1].chapter_id, canon=canon,
                         specs=[(l2.CLAIM_ENTITY_MENTION, "e1", BAD_NAME)]),
    }
    result = {"book": COVER_BOOK}
    baseline = l2.build_report(snap, canon, mode="assist", result=result,
                               claims_by_index=claims)
    events = next(r for r in baseline.results
                  if r.predicate == l2.PREDICATE_ONE_TIME_EVENT)
    assert events.coverage_state == l2.COVERAGE_UNKNOWN_CANON_REFERENCE
    assert events.checked_units == 0, "the fixture starts with coverage already up"
    assert set(rp.repairable_targets(baseline)) == {0, 1}

    fix = lambda b: b.replace(BAD_NAME.encode(), GOOD_NAME.encode())
    provider = _Provider(fix)
    extractor = _Scripted({
        # chapter 1 repaired: the bogus event citation goes with it.
        0: lambda b, cid, c: _multi_claims(
            b, index=0, chapter_id=cid, canon=c,
            specs=[(l2.CLAIM_ENTITY_MENTION, "e1", GOOD_NAME)]),
        # chapter 2's candidate brings one back.
        1: lambda b, cid, c: _multi_claims(
            b, index=1, chapter_id=cid, canon=c,
            specs=[(l2.CLAIM_ENTITY_MENTION, "e1", GOOD_NAME),
                   (l2.CLAIM_ONE_TIME_EVENT, "ev9", "pantai")]),
    })

    run = asyncio.run(rp.repair_manuscript(
        snap, canon, mode="assist", result=result, claims_by_index=claims,
        repair_provider=provider, extract_chapter=extractor))

    by_index = {r.chapter_index: r for r in run.chapters}
    assert by_index[0].outcome == rp.OUTCOME_REPAIRED, "the first repair did not land"
    assert by_index[1].outcome == rp.OUTCOME_UNRESOLVED
    assert by_index[1].last_reason == rp.REASON_COVERAGE_REGRESSED
    # The coverage the first repair earned is still there at the end.
    final_events = next(r for r in run.report.results
                        if r.predicate == l2.PREDICATE_ONE_TIME_EVENT)
    assert final_events.coverage_state in (l2.COVERAGE_CHECKED,
                                           l2.COVERAGE_NO_VIOLATIONS_FOUND)
    assert final_events.checked_units == 2


# ── 13. the schema, not just the engine, refuses enforce ────────────────────
@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_only_assist_may_report_repair_rounds(mode):
    """🔴 THE ENGINE REFUSING ENFORCE IS NOT ENOUGH.

    A report is written, read and audited a long way from the engine that built
    it. While L3-ENFORCE does not exist, `mode="enforce", repair_rounds=1` is a
    claim that a project which was never built performed repairs — and only the
    schema is positioned to make that claim impossible. Allow-list, so that
    admitting enforce later has to be a deliberate edit here.
    """
    snap = l2.materialize_final_snapshot({"book": BOOK})
    with pytest.raises(Exception) as excinfo:
        l2.build_report(snap, None, mode=mode, result={"book": BOOK},
                        repair_rounds=1)
    assert "repair_rounds" in str(excinfo.value)
    # ...and 0 rounds stays legal for every mode.
    assert l2.build_report(snap, None, mode=mode, result={"book": BOOK},
                           repair_rounds=0).repair_rounds == 0


REINTRO_BOOK = ("## Bab 1\nNamanya Hartono saat gerhana tiba.\n\n"
                "## Bab 2\nNamanya Hartono saat gerhana usai.\n")


def test_a_repair_may_not_reintroduce_what_an_earlier_repair_removed():
    """🔴 "NEW" MEANS NEW TO THE BOOK AS IT STANDS, NOT ABSENT FROM THE FIRST REPORT.

    Repair A removes chapter 1's telling of the one-time event, which clears the
    duplication in chapter 2. Chapter 2's own candidate then tells it twice —
    putting back the very violation A had just removed.

    Against the current book that is a new violation and the candidate is refused.
    Against the ORIGINAL report it is not new at all — it was in the first
    report — so the candidate is accepted and the pass undoes its own work while
    recording two successful repairs.
    """
    canon = _event_canon()
    snap = l2.materialize_final_snapshot({"book": REINTRO_BOOK})
    claims = {
        i: _multi_claims(snap.block_bytes(i), index=i,
                         chapter_id=snap.blocks[i].chapter_id, canon=canon,
                         specs=[(l2.CLAIM_ENTITY_MENTION, "e1", BAD_NAME),
                                (l2.CLAIM_ONE_TIME_EVENT, "ev1", "gerhana")])
        for i in range(2)
    }
    result = {"book": REINTRO_BOOK}
    baseline = l2.build_report(snap, canon, mode="assist", result=result,
                               claims_by_index=claims)
    assert rp.repairable_targets(baseline)[1] == (
        l2.VIOLATION_ENTITY_NAME, l2.VIOLATION_ONE_TIME_EVENT)

    fix = lambda b: b.replace(BAD_NAME.encode(), GOOD_NAME.encode())
    provider = _Provider(fix)
    extractor = _Scripted({
        0: lambda b, cid, c: _multi_claims(
            b, index=0, chapter_id=cid, canon=c,
            specs=[(l2.CLAIM_ENTITY_MENTION, "e1", GOOD_NAME)]),
        # Chapter 2's candidate tells the one-time event twice.
        1: lambda b, cid, c: _multi_claims(
            b, index=1, chapter_id=cid, canon=c,
            specs=[(l2.CLAIM_ENTITY_MENTION, "e1", GOOD_NAME),
                   (l2.CLAIM_ONE_TIME_EVENT, "ev1", "gerhana"),
                   (l2.CLAIM_ONE_TIME_EVENT, "ev1", "usai")]),
    })

    run = asyncio.run(rp.repair_manuscript(
        snap, canon, mode="assist", result=result, claims_by_index=claims,
        repair_provider=provider, extract_chapter=extractor,
        max_edit_ratio_ppm=1_000_000))

    by_index = {r.chapter_index: r for r in run.chapters}
    assert by_index[0].outcome == rp.OUTCOME_REPAIRED, "the first repair did not land"
    # Chapter 2's entity target was still live, so it WAS attempted...
    assert by_index[1].attempted_codes == (l2.VIOLATION_ENTITY_NAME,), \
        "chapter 2's live target was not recomputed after the first repair"
    # ...and refused for putting back what the first repair removed.
    assert by_index[1].outcome == rp.OUTCOME_UNRESOLVED
    assert by_index[1].last_reason == rp.REASON_REGRESSED
    assert by_index[1].after_sha256 == by_index[1].before_sha256


def test_a_candidate_that_changes_the_block_count_is_refused(caplog):
    """🔴 EVERY JUDGEMENT BELOW INDEXES CHAPTERS BY POSITION.

    The heading check only guards the candidate's FIRST line, so a candidate can
    keep its own heading and still append a second one. The manuscript then
    materializes with three blocks where the report has two, and from that point
    on `staged_violations` at index 1 refers to a different chapter than the
    baseline's index 1 — every comparison silently changes meaning. Rejecting is
    the only safe answer; there is no correct way to re-align them.
    """
    snap, canon, claims, result = _setup()
    grown = _fixed_block(snap) + b"\n## Bab 3\nbab siluman.\n"
    # It passes the heading check: the block still STARTS with its own heading.
    assert grown.decode().startswith("## Bab 2")

    provider = _Provider(lambda b: grown)
    with caplog.at_level("WARNING", logger="canon_lite_l3_repair"):
        run = _run(snap, canon, claims, result, provider, _Extractor(),
                   max_edit_ratio_ppm=1_000_000)

    rec = run.chapters[0]
    assert rec.outcome == rp.OUTCOME_UNRESOLVED
    assert rec.last_reason == rp.REASON_INVALID_CANDIDATE
    assert not run.changed
    assert run.repaired_manuscript == snap.manuscript_bytes
    assert b"bab siluman" not in run.repaired_manuscript
    # Rejected on both attempts, not silently accepted on the second.
    assert len(provider.calls) == rp.MAX_REPAIR_ITERATIONS
    assert any("stage=post.block_count" in record.message for record in caplog.records)


def test_the_final_report_invariant_compares_the_whole_verdict():
    """The invariant is a real guard, so it has to be reachable.

    A rebuild that produced a different verdict must raise rather than return a
    report nobody's acceptance was judged against.
    """
    snap, canon, claims, result = _setup()
    calls = {"n": 0}
    real = l2.build_report

    def _drifting(*a, **kw):
        # The LAST build_report call is the final rebuild; make it disagree by
        # dropping the claims, which changes coverage without changing violations.
        calls["n"] += 1
        if kw.get("repair_rounds"):
            kw = {**kw, "claims_by_index": {}}
        return real(*a, **kw)

    import unittest.mock as mock
    with mock.patch.object(l2, "build_report", _drifting):
        with pytest.raises(rp.RepairError) as excinfo:
            _run(snap, canon, claims, result,
                 _Provider(lambda b: _fixed_block(snap)), _Extractor())
    assert "disagrees" in str(excinfo.value)
