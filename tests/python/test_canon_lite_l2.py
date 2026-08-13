"""Canon Lite L2a — offline acceptance suite.

Covers the §10.4 gates that are provable without a provider: the deterministic
materializer, the strict schemas, coverage semantics, the report boundary, and the
`NO_CANON_AUTHORITY` refusal. Nothing here touches a network, a database, or a job.

Deliberate bias throughout: every "it rejects X" test is paired with a POSITIVE build that
proves the same path can succeed. A suite of nothing but rejections stays green when the
whole construction path is dead.
"""
import ast
import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

import canon_lite as cl            # noqa: E402
import canon_lite_extractor as ext  # noqa: E402
import canon_lite_l2 as l2         # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures — small, explicit, and written out so a reader can see the bytes.
# ---------------------------------------------------------------------------

PREFIXED = "Kata pengantar\n\n## Bab 1: Awal\nisi satu\n\n## Bab 2: Lanjut\nisi dua\n"
NO_PREFIX = "## Bab 1\nisi\n## Bab 2\nisi dua"
CJK = "序\n\n## 第 1 章：開始\n本文がここにあります\n\n## 第 2 章\n続き\n"


def _canon(n_chapters=2, titles=None, *, entities=(), anchors=(), events=()):
    """A minimal valid CanonLiteV1. `entities` non-empty gives it semantic authority.

    Built through `canon_lite._finalize` on purpose. That function's own docstring calls
    itself "the ONLY constructor path", and it is what binds `canon_sha256` to the
    content. A hand-computed hash here would not merely be redundant — it would be a
    second, divergent definition of what the canon hash means, and the artifact's
    `verify_sha256` would reject it.
    """
    titles = titles or [cl.UNKNOWN] * n_chapters
    chapters = tuple(
        cl.CanonChapterV1(chapter_id=f"ch{i+1}", order=i + 1, expected_title=titles[i])
        for i in range(n_chapters))
    return cl._finalize({
        "schema_version": cl.SCHEMA_VERSION,
        "outline_sha256": "a" * 64,
        "generation_config_sha256": "b" * 64,
        "target_language": "id",
        "chapters": chapters,
        "entities": tuple(entities),
        "anchors": tuple(anchors), "one_time_events": tuple(events),
        "reveals": (), "flashback_exceptions": (),
        "fact_source_policy": "fiction_generated",
        "advisory_bible_sha256": cl.UNKNOWN,
    })


# ===========================================================================
# Materializer — exactness first
# ===========================================================================

@pytest.mark.parametrize("text", [PREFIXED, NO_PREFIX, CJK, "prosa tanpa judul"])
def test_snapshot_round_trips_to_the_exact_source_bytes(text):
    snap = l2.materialize_final_snapshot({"book": text})
    assert snap is not None
    assert snap.reconstruct() == text.encode("utf-8")
    assert snap.manuscript_sha256 == cl.sha256_hex(text.encode("utf-8"))


def test_materializer_is_deterministic():
    a = l2.materialize_final_snapshot({"book": PREFIXED})
    b = l2.materialize_final_snapshot({"book": PREFIXED})
    assert a.to_canonical_obj() == b.to_canonical_obj()
    assert a.ordered_blocks_sha256 == b.ordered_blocks_sha256


def test_offsets_are_byte_offsets_not_character_indices():
    """A multibyte manuscript is where a character index silently mis-slices."""
    snap = l2.materialize_final_snapshot({"book": CJK})
    assert snap.chapter_count == 2
    for b in snap.blocks:
        # Every slice must decode cleanly: a character-index bug lands mid-codepoint.
        snap.manuscript_bytes[b.byte_start:b.byte_end].decode("utf-8")
    assert snap.block_bytes(0).decode("utf-8").startswith("## 第 1 章")
    # And the byte range must genuinely exceed the character count for CJK.
    assert snap.blocks[0].byte_end - snap.blocks[0].byte_start > \
        len(snap.block_bytes(0).decode("utf-8"))


def test_one_byte_change_moves_exactly_the_affected_hashes():
    a = l2.materialize_final_snapshot({"book": PREFIXED})
    b = l2.materialize_final_snapshot({"book": PREFIXED.replace("isi dua", "isi Dua")})
    assert a.blocks[0].content_sha256 == b.blocks[0].content_sha256      # untouched
    assert a.blocks[1].content_sha256 != b.blocks[1].content_sha256      # changed
    assert a.ordered_blocks_sha256 != b.ordered_blocks_sha256
    assert a.manuscript_sha256 != b.manuscript_sha256


def test_output_key_scenarios_materialize_identically_to_book():
    a = l2.materialize_final_snapshot({"book": NO_PREFIX})
    b = l2.materialize_final_snapshot({"output": NO_PREFIX})
    assert a.source_key == "book" and b.source_key == "output"
    assert a.ordered_blocks_sha256 == b.ordered_blocks_sha256
    assert a.manuscript_sha256 == b.manuscript_sha256


def test_missing_manuscript_is_none_not_an_empty_snapshot():
    """An empty snapshot would report coverage 0 — indistinguishable from 'measured'."""
    assert l2.materialize_final_snapshot({}) is None
    assert l2.materialize_final_snapshot({"book": ""}) is None
    assert l2.materialize_final_snapshot({"book": None}) is None


def test_prefix_before_the_first_heading_is_preserved_not_dropped():
    snap = l2.materialize_final_snapshot({"book": PREFIXED})
    assert snap.prefix_byte_end > 0
    assert snap.manuscript_bytes[:snap.prefix_byte_end].decode("utf-8") == \
        "Kata pengantar\n\n"


def test_r16_suffixed_headings_are_unknown_never_collapsed_to_the_bare_number():
    """`3a`/`3.5` must not read as chapter 3 — that bug once deleted real chapters."""
    snap = l2.materialize_final_snapshot(
        {"book": "## Bab 3\nx\n## Bab 3a\ny\n## Bab 3.5\nz\n"})
    assert [b.heading_number for b in snap.blocks] == [3, -1, -1]


def test_manuscript_over_the_byte_bound_is_rejected_not_truncated():
    huge = "## Bab 1\n" + ("x" * (l2.MAX_MANUSCRIPT_BYTES + 1))
    with pytest.raises(cl.CanonBoundsError):
        l2.materialize_final_snapshot({"book": huge})


def test_snapshot_that_cannot_reconstruct_its_source_cannot_be_constructed():
    """The round-trip is a constructor invariant, not just a property tests check."""
    good = l2.materialize_final_snapshot({"book": NO_PREFIX})
    with pytest.raises(l2.SnapshotError):
        l2.FinalChapterSnapshotV1(
            schema_version=l2.SNAPSHOT_SCHEMA_VERSION,
            materializer_version=l2.MATERIALIZER_VERSION,
            source_key="book",
            manuscript_bytes=good.manuscript_bytes,
            prefix_byte_end=0,
            blocks=good.blocks[:1],                      # drops chapter 2
            ordered_blocks_sha256=good.ordered_blocks_sha256,
            manuscript_sha256=good.manuscript_sha256,
        )


def test_snapshot_constructor_rejects_a_forged_block_hash_and_ordered_hash():
    good = l2.materialize_final_snapshot({"book": NO_PREFIX})
    block = good.blocks[0]
    forged = l2.ChapterBlockV1(
        index=block.index, chapter_id=block.chapter_id,
        heading_number=block.heading_number, byte_start=block.byte_start,
        byte_end=block.byte_end, content_sha256="f" * 64)
    with pytest.raises(l2.SnapshotError, match="content_sha256"):
        l2.FinalChapterSnapshotV1(
            schema_version=good.schema_version,
            materializer_version=good.materializer_version,
            source_key=good.source_key, manuscript_bytes=good.manuscript_bytes,
            prefix_byte_end=good.prefix_byte_end,
            blocks=(forged, *good.blocks[1:]),
            ordered_blocks_sha256=good.ordered_blocks_sha256,
            manuscript_sha256=good.manuscript_sha256)
    with pytest.raises(l2.SnapshotError, match="ordered_blocks_sha256"):
        l2.FinalChapterSnapshotV1(
            schema_version=good.schema_version,
            materializer_version=good.materializer_version,
            source_key=good.source_key, manuscript_bytes=good.manuscript_bytes,
            prefix_byte_end=good.prefix_byte_end, blocks=good.blocks,
            ordered_blocks_sha256="e" * 64,
            manuscript_sha256=good.manuscript_sha256)


# ===========================================================================
# Structural predicate
# ===========================================================================

def test_structure_predicate_is_clean_on_a_matching_outline():
    canon = _canon(2)
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX}, canon=canon)
    res = l2.evaluate_structure(snap, canon)
    assert res.coverage_state == l2.COVERAGE_NO_VIOLATIONS_FOUND
    assert res.violations == ()
    assert res.checked_units == 2 and res.total_units == 2


def test_structure_predicate_flags_a_count_mismatch():
    canon = _canon(3)
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX}, canon=canon)
    res = l2.evaluate_structure(snap, canon)
    assert l2.VIOLATION_CHAPTER_COUNT in {v.code for v in res.violations}
    assert res.coverage_state == l2.COVERAGE_CHECKED


def test_structure_predicate_flags_out_of_order_headings():
    canon = _canon(2)
    snap = l2.materialize_final_snapshot(
        {"book": "## Bab 2\nx\n## Bab 1\ny"}, canon=canon)
    res = l2.evaluate_structure(snap, canon)
    assert l2.VIOLATION_CHAPTER_ORDER in {v.code for v in res.violations}


def test_structure_predicate_flags_a_duplicated_chapter_number():
    canon = _canon(2)
    snap = l2.materialize_final_snapshot(
        {"book": "## Bab 1\nx\n## Bab 1\ny"}, canon=canon)
    res = l2.evaluate_structure(snap, canon)
    assert l2.VIOLATION_CHAPTER_DUPLICATE in {v.code for v in res.violations}


def test_unparseable_heading_is_unknown_not_an_order_violation():
    """Calling something wrong when it was never read is the false positive to avoid."""
    canon = _canon(2)
    snap = l2.materialize_final_snapshot(
        {"book": "## Prolog\nx\n## Bab 2\ny"}, canon=canon)
    res = l2.evaluate_structure(snap, canon)
    assert l2.VIOLATION_CHAPTER_ORDER not in {v.code for v in res.violations}
    assert l2.VIOLATION_CHAPTER_IDENTITY_UNKNOWN in {
        v.code for v in res.violations}
    assert res.coverage_state == l2.COVERAGE_CHECKED


def test_structure_without_a_canon_is_refused_not_called_clean():
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX})
    res = l2.evaluate_structure(snap, None)
    assert res.coverage_state == l2.COVERAGE_NO_CANON_AUTHORITY
    assert res.violations == ()
    assert res.checked_units == 0 and res.total_units == 2      # denominator survives


# ===========================================================================
# D-L2-5 — no semantic authority means refusal, never "zero violations"
# ===========================================================================

@pytest.mark.parametrize("predicate", l2.SEMANTIC_PREDICATES)
def test_semantic_predicates_refuse_without_accepted_authority(predicate):
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX})
    res = l2.evaluate_semantic(predicate, snap, _canon(2), {})
    assert res.coverage_state == l2.COVERAGE_NO_CANON_AUTHORITY
    assert res.violations == ()


def test_l1_structural_canon_has_no_semantic_authority():
    """The live L1 canon carries no entities/anchors/events — so nothing to contradict."""
    assert l2.has_semantic_authority(_canon(2)) is False


def test_a_canon_with_entities_does_have_authority():
    """Positive control: the authority check is not hardwired to False."""
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="Rina", aliases=(),
                           alias_source="none")
    assert l2.has_semantic_authority(_canon(2, entities=(ent,))) is True


def test_semantic_predicate_with_authority_reports_incomplete_when_coverage_is_partial():
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="Rina", aliases=(),
                           alias_source="none")
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX})
    res = l2.evaluate_semantic(l2.PREDICATE_ENTITY_NAME, snap,
                               _canon(2, entities=(ent,)), {})
    assert res.coverage_state == l2.COVERAGE_INCOMPLETE_EXTRACTION
    assert res.total_units == 2


# ===========================================================================
# ChapterClaimsV1 — untrusted input
# ===========================================================================

def _claim_payload(snap, idx=0, canon=None, **over):
    """DG-4: v2 payloads must name their canon. `canon` is threaded through rather than
    defaulted to a constant, because the parser now checks BOTH directions — a payload
    naming the wrong real hash is refused just as firmly as one naming UNKNOWN."""
    block = snap.block_bytes(idx)
    start, end = 0, min(8, len(block))
    coverage = {
        predicate: l2.COVERAGE_NO_CLAIMS_FOUND
        for predicate in l2.SEMANTIC_PREDICATES
    }
    coverage[l2.PREDICATE_ENTITY_NAME] = l2.COVERAGE_CHECKED
    base = {
        "schema_version": l2.CLAIMS_SCHEMA_VERSION,
        "chapter_index": idx,
        "chapter_id": snap.blocks[idx].chapter_id,
        "content_sha256": snap.blocks[idx].content_sha256,
        "canon_sha256": canon.canon_sha256 if canon is not None else cl.UNKNOWN,
        "extractor_version": "test_v1",
        "model_version": "model_v1",
        "prompt_sha256": "d" * 64,
        "predicate_set_version": l2.PREDICATE_SET_VERSION,
        "coverage": coverage,
        # The wire carries the evidence VERBATIM; the server locates it and derives the
        # span and digest. Asking the model for offsets and a SHA-256 is what made every
        # live attempt unparseable — see `_CLAIM_FIELDS` in canon_lite_l2.
        "claims": [{
            "claim_type": l2.CLAIM_ENTITY_MENTION,
            "canon_ref": "e1",
            "quote": block[start:end].decode("utf-8"),
        }],
    }
    base.update(over)
    return base


def _payload_for_span(snap, *, idx, claim_type, canon_ref, evidence, canon=None):
    block = snap.block_bytes(idx)
    needle = evidence.encode("utf-8")
    start = block.index(needle)
    predicate = l2._CLAIM_PREDICATE[claim_type]
    coverage = {
        p: l2.COVERAGE_NO_CLAIMS_FOUND for p in l2.SEMANTIC_PREDICATES}
    coverage[predicate] = l2.COVERAGE_CHECKED
    return _claim_payload(
        snap, idx=idx, canon=canon, coverage=coverage,
        claims=[{
            "claim_type": claim_type,
            "canon_ref": canon_ref,
            "quote": evidence,
        }])


def test_a_well_formed_claim_parses(monkeypatch):
    """POSITIVE build — without it every rejection test below could pass on dead code."""
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="Rina", aliases=(),
                           alias_source="none")
    canon = _canon(2, entities=(ent,))
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX}, canon=canon)
    parsed = l2.parse_chapter_claims(_claim_payload(snap, canon=canon), snapshot=snap,
                                     canon=canon)
    assert parsed.coverage_state == l2.COVERAGE_CHECKED
    assert parsed.measured is True
    assert parsed.content_sha256 == snap.blocks[0].content_sha256


def test_extractor_invented_canon_id_is_rejected():
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="Rina", aliases=(),
                           alias_source="none")
    canon = _canon(2, entities=(ent,))
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX}, canon=canon)
    payload = _claim_payload(snap, canon=canon)
    payload["claims"][0]["canon_ref"] = "e_invented"
    with pytest.raises(cl.CanonSchemaError):
        l2.parse_chapter_claims(payload, snapshot=snap,
                                canon=canon)


def test_evidence_hash_that_does_not_bind_the_span_is_rejected():
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="Rina", aliases=(),
                           alias_source="none")
    canon = _canon(2, entities=(ent,))
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX}, canon=canon)
    payload = _claim_payload(snap, canon=canon)
    payload["claims"][0]["evidence_sha256"] = "c" * 64
    with pytest.raises(cl.CanonSchemaError):
        l2.parse_chapter_claims(payload, snapshot=snap,
                                canon=canon)


def test_a_quote_that_is_not_in_the_chapter_is_rejected():
    """🔴 THE PROPERTY THIS PARSER EXISTS FOR: a fabricated citation.

    Under the old wire the model supplied offsets and a digest, and this test bounded the
    "range outside the chapter" branch. Offsets are gone; the equivalent — and stronger —
    property is that evidence the chapter does not contain can never become a claim. A
    parser that located quotes leniently (nearest match, normalised whitespace, casefold)
    would let an extractor cite text it invented.
    """
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="Rina", aliases=(),
                           alias_source="none")
    canon = _canon(2, entities=(ent,))
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX}, canon=canon)
    payload = _claim_payload(snap, canon=canon)
    payload["claims"][0]["quote"] = "kalimat yang tidak pernah ada di bab ini"
    with pytest.raises(cl.CanonSchemaError, match="not found in the chapter block"):
        l2.parse_chapter_claims(payload, snapshot=snap, canon=canon)


def test_a_quote_appearing_twice_is_refused_rather_than_resolved():
    """Ambiguity is a refusal, never a guess. Taking the first occurrence would pin the
    claim to a location the extractor never chose, and the digest would then bind bytes
    nobody cited."""
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="Rina", aliases=(),
                           alias_source="none")
    canon = _canon(2, entities=(ent,))
    snap = l2.materialize_final_snapshot(
        {"book": "## Bab 1\nRina pergi. Rina pergi.\n## Bab 2\nlain"}, canon=canon)
    assert snap.block_bytes(0).count(b"Rina pergi") == 2
    payload = _claim_payload(snap, canon=canon)
    payload["claims"][0]["quote"] = "Rina pergi"
    with pytest.raises(cl.CanonSchemaError, match="ambiguous"):
        l2.parse_chapter_claims(payload, snapshot=snap, canon=canon)


def test_evidence_span_over_the_size_bound_is_rejected():
    """And bound the size limit separately, so neither check can hide the other."""
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="Rina", aliases=(),
                           alias_source="none")
    canon = _canon(2, entities=(ent,))
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX}, canon=canon)
    payload = _claim_payload(snap, canon=canon)
    # The bound still applies to the DERIVED span. A quote this long cannot be located in
    # the chapter either, so the size check is reached only because it is evaluated inside
    # `ObservedClaimV1.__post_init__` — before the parser searches. Keeping the row proves
    # the bound survived the move from model-supplied offsets to a server-derived span.
    payload["claims"][0]["quote"] = "x" * (l2.MAX_EVIDENCE_BYTES + 1)
    with pytest.raises((cl.CanonBoundsError, cl.CanonSchemaError)):
        l2.parse_chapter_claims(payload, snapshot=snap,
                                canon=canon)


def test_a_multibyte_quote_derives_the_correct_byte_span():
    """The UTF-8 concern survived the contract change; only its shape moved.

    A model can no longer hand over a range that splits a code point — it hands over text.
    The risk is now the server's: locating a quote whose characters are multi-byte and
    deriving a span that is off by the difference between characters and bytes. Indonesian
    and Korean names are exactly where that would show, and a book of them is what this
    system writes.

    Asserted against `block.index(...)` computed independently in the test, so a parser
    that used character offsets instead of byte offsets would fail here rather than
    quietly cite the wrong span.
    """
    ent = cl.CanonEntityV1(
        entity_id="e1", canonical_name="Rina", aliases=(), alias_source="none")
    canon = _canon(1, entities=(ent,))
    snap = l2.materialize_final_snapshot(
        {"book": "## Bab 1\nSeoul—Itaewon. Namanya Rina di kota itu."}, canon=canon)
    block = snap.block_bytes(0)
    quote = "Rina"
    # An em dash precedes the quote, so a character-indexed parser is off by two bytes.
    assert b"\xe2\x80\x94" in block, "the multi-byte character under test is present"

    payload = _claim_payload(
        snap, canon=canon,
        claims=[{"claim_type": l2.CLAIM_ENTITY_MENTION, "canon_ref": "e1",
                 "quote": quote}])
    art = l2.parse_chapter_claims(payload, snapshot=snap, canon=canon)

    claim = art.claims[0]
    expected_start = block.index(quote.encode("utf-8"))
    assert claim.evidence_start == expected_start
    assert claim.evidence_end == expected_start + len(quote.encode("utf-8"))
    assert block[claim.evidence_start:claim.evidence_end].decode("utf-8") == quote
    assert claim.evidence_sha256 == cl.sha256_hex(quote.encode("utf-8"))


def test_unknown_field_in_extractor_output_is_rejected():
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX})
    payload = _claim_payload(snap, canon=_canon(2))
    payload["surprise"] = 1
    with pytest.raises(cl.CanonSchemaError):
        l2.parse_chapter_claims(payload, snapshot=snap, canon=_canon(2))


def test_chapter_index_outside_the_snapshot_is_rejected():
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX})
    with pytest.raises(cl.CanonSchemaError):
        l2.parse_chapter_claims(_claim_payload(snap, canon=_canon(2), idx=0) | {"chapter_index": 99},
                                snapshot=snap, canon=_canon(2))


@pytest.mark.parametrize("state", [
    l2.COVERAGE_TIMEOUT, l2.COVERAGE_PROVIDER_FAILURE,
    l2.COVERAGE_INVALID_EXTRACTOR_OUTPUT, l2.COVERAGE_INCOMPLETE_EXTRACTION,
])
def test_a_failed_extraction_is_never_measured_and_may_not_carry_claims(state):
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX})
    failed_coverage = {predicate: state for predicate in l2.SEMANTIC_PREDICATES}
    with pytest.raises(cl.CanonSchemaError):
        l2.parse_chapter_claims(
            _claim_payload(snap, canon=_canon(2)) | {"coverage": failed_coverage},
                                snapshot=snap, canon=_canon(2))
    empty = l2.parse_chapter_claims(
        _claim_payload(snap, canon=_canon(2)) | {"coverage": failed_coverage, "claims": []},
        snapshot=snap, canon=_canon(2))
    assert empty.measured is False


def test_checked_with_no_claims_becomes_no_claims_found():
    """The one state that means 'measured, and none found' must be stated explicitly."""
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX})
    coverage = {
        predicate: l2.COVERAGE_NO_CLAIMS_FOUND
        for predicate in l2.SEMANTIC_PREDICATES
    }
    parsed = l2.parse_chapter_claims(
        _claim_payload(snap, canon=_canon(2)) | {"coverage": coverage, "claims": []},
        snapshot=snap, canon=_canon(2))
    assert parsed.coverage_state == l2.COVERAGE_NO_CLAIMS_FOUND
    assert parsed.measured is True


def test_claims_bind_to_the_exact_chapter_bytes_so_a_mutation_invalidates_them():
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="Rina", aliases=(),
                           alias_source="none")
    canon = _canon(2, entities=(ent,))
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX}, canon=canon)
    parsed = l2.parse_chapter_claims(_claim_payload(snap, canon=canon), snapshot=snap,
                                     canon=canon)
    mutated = l2.materialize_final_snapshot(
        {"book": NO_PREFIX.replace("isi\n", "ISI\n")}, canon=canon)
    assert parsed.content_sha256 != mutated.blocks[0].content_sha256


def test_authority_is_predicate_specific_not_a_global_boolean():
    ent = cl.CanonEntityV1(
        entity_id="e1", canonical_name="Rina", aliases=(), alias_source="none")
    canon = _canon(2, entities=(ent,))
    assert l2.has_predicate_authority(l2.PREDICATE_ENTITY_NAME, canon)
    assert not l2.has_predicate_authority(l2.PREDICATE_FIXED_LITERAL, canon)
    assert not l2.has_predicate_authority(l2.PREDICATE_ONE_TIME_EVENT, canon)


def test_entity_predicate_compares_evidence_against_authoritative_names():
    ent = cl.CanonEntityV1(
        entity_id="e1", canonical_name="Rina", aliases=("Ina",),
        alias_source="job_input")
    canon = _canon(2, entities=(ent,))
    text = "## Bab 1\nRina datang\n## Bab 2\nBudi pergi"
    snap = l2.materialize_final_snapshot({"book": text}, canon=canon)
    good = l2.parse_chapter_claims(
        _payload_for_span(
            snap, canon=canon, idx=0, claim_type=l2.CLAIM_ENTITY_MENTION,
            canon_ref="e1", evidence="Rina"),
        snapshot=snap, canon=canon)
    bad = l2.parse_chapter_claims(
        _payload_for_span(
            snap, canon=canon, idx=1, claim_type=l2.CLAIM_ENTITY_MENTION,
            canon_ref="e1", evidence="Budi"),
        snapshot=snap, canon=canon)
    result = l2.evaluate_semantic(
        l2.PREDICATE_ENTITY_NAME, snap, canon, {0: good, 1: bad})
    assert result.coverage_state == l2.COVERAGE_CHECKED
    assert [(v.code, v.chapter_index) for v in result.violations] == [
        (l2.VIOLATION_ENTITY_NAME, 1)]


def test_fixed_literal_predicate_is_deterministic():
    anchor = cl.CanonAnchorV1(anchor_id="a1", kind="time", literal="10:00")
    canon = _canon(2, anchors=(anchor,))
    text = "## Bab 1\n10:00\n## Bab 2\n11:00"
    snap = l2.materialize_final_snapshot({"book": text}, canon=canon)
    rows = {}
    for index, literal in enumerate(("10:00", "11:00")):
        payload = _payload_for_span(
            snap, canon=canon, idx=index, claim_type=l2.CLAIM_FIXED_LITERAL,
            canon_ref="a1", evidence=literal)
        rows[index] = l2.parse_chapter_claims(
            payload, snapshot=snap, canon=canon)
    result = l2.evaluate_semantic(
        l2.PREDICATE_FIXED_LITERAL, snap, canon, rows)
    assert [(v.code, v.chapter_index) for v in result.violations] == [
        (l2.VIOLATION_FIXED_LITERAL, 1)]


def test_one_time_event_predicate_flags_only_repeated_occurrences():
    event = cl.CanonEventV1(event_id="ev1", occurs_chapter_order=1, label="pintu pecah")
    canon = _canon(2, events=(event,))
    text = "## Bab 1\npintu pecah\n## Bab 2\npintu pecah lagi"
    snap = l2.materialize_final_snapshot({"book": text}, canon=canon)
    rows = {}
    for index, evidence in enumerate(("pintu pecah", "pintu pecah")):
        rows[index] = l2.parse_chapter_claims(
            _payload_for_span(
                snap, canon=canon, idx=index, claim_type=l2.CLAIM_ONE_TIME_EVENT,
                canon_ref="ev1", evidence=evidence),
            snapshot=snap, canon=canon)
    result = l2.evaluate_semantic(
        l2.PREDICATE_ONE_TIME_EVENT, snap, canon, rows)
    assert [(v.code, v.chapter_index) for v in result.violations] == [
        (l2.VIOLATION_ONE_TIME_EVENT, 1)]


def test_one_time_event_duplication_ignores_quote_wording_only_canon_ref_matters():
    """🔴 THE EVALUATOR NEVER READS THE EVIDENCE FOR ONE_TIME_EVENT. `CanonEventV1` carries
    no literal to compare against (unlike entity_mention/fixed_literal), so two claims
    citing the SAME canon_ref with COMPLETELY DIFFERENT wording must still be flagged as a
    duplicate — the predicate is canon_ref identity alone, never quote equality. The row
    above uses identical evidence text both times and cannot by itself tell "compares
    canon_ref" apart from "happens to compare equal text"; this one can."""
    event = cl.CanonEventV1(event_id="ev1", occurs_chapter_order=1, label="pintu pecah")
    canon = _canon(2, events=(event,))
    snap = l2.materialize_final_snapshot(
        {"book": "## Bab 1\npintu itu pecah berkeping-keping\n"
                 "## Bab 2\nsuara ledakan menggema di lorong"},
        canon=canon)
    rows = {
        0: l2.parse_chapter_claims(
            _payload_for_span(snap, idx=0, claim_type=l2.CLAIM_ONE_TIME_EVENT,
                              canon_ref="ev1", evidence="pintu itu pecah berkeping-keping",
                              canon=canon),
            snapshot=snap, canon=canon),
        1: l2.parse_chapter_claims(
            _payload_for_span(snap, idx=1, claim_type=l2.CLAIM_ONE_TIME_EVENT,
                              canon_ref="ev1", evidence="suara ledakan menggema di lorong",
                              canon=canon),
            snapshot=snap, canon=canon),
    }
    result = l2.evaluate_semantic(l2.PREDICATE_ONE_TIME_EVENT, snap, canon, rows)
    assert [(v.code, v.chapter_index) for v in result.violations] == [
        (l2.VIOLATION_ONE_TIME_EVENT, 1)], (
        "duplication must fire on canon_ref identity alone — wording never matters for "
        "one_time_event, unlike entity_mention/fixed_literal")


def test_claim_type_cannot_reference_an_id_from_another_authority_class():
    event = cl.CanonEventV1(event_id="ev1", occurs_chapter_order=1, label="pintu pecah")
    canon = _canon(2, events=(event,))
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX}, canon=canon)
    payload = _claim_payload(snap, canon=canon)
    payload["claims"][0]["canon_ref"] = "ev1"
    with pytest.raises(cl.CanonSchemaError, match="not accepted"):
        l2.parse_chapter_claims(payload, snapshot=snap, canon=canon)


def test_untrusted_claims_require_the_exact_json_array_type():
    ent = cl.CanonEntityV1(
        entity_id="e1", canonical_name="Rina", aliases=(), alias_source="none")
    canon = _canon(2, entities=(ent,))
    snap = l2.materialize_final_snapshot({"book": NO_PREFIX}, canon=canon)
    payload = _claim_payload(snap, canon=canon)
    payload["claims"] = tuple(payload["claims"])
    with pytest.raises(cl.CanonSchemaError, match="expected a list"):
        l2.parse_chapter_claims(payload, snapshot=snap, canon=canon)


def test_negative_denominators_and_forged_report_status_are_rejected():
    with pytest.raises(cl.CanonSchemaError, match="checked_units"):
        l2.PredicateResultV1(
            predicate=l2.PREDICATE_STRUCTURE, scope=l2.SCOPE_GLOBAL,
            coverage_state=l2.COVERAGE_NO_VIOLATIONS_FOUND,
            checked_units=-1, total_units=2, violations=())
    canon = _canon(2)
    result = {"book": NO_PREFIX}
    snap = l2.materialize_final_snapshot(result, canon=canon)
    good = l2.build_report(snap, canon, mode="shadow", result=result)
    with pytest.raises(cl.CanonSchemaError, match="continuity_status"):
        replace(good, continuity_status=l2.STATUS_CLEAN)


# ===========================================================================
# Injected extractor lifecycle — no production provider, metering, or cost
# ===========================================================================

def _provider_output(*, entity_state=l2.COVERAGE_NO_CLAIMS_FOUND, claims=()):
    return {
        "coverage": {
            l2.PREDICATE_ENTITY_NAME: entity_state,
            l2.PREDICATE_FIXED_LITERAL: l2.COVERAGE_NO_CANON_AUTHORITY,
            l2.PREDICATE_ONE_TIME_EVENT: l2.COVERAGE_NO_CANON_AUTHORITY,
        },
        "claims": list(claims),
    }


def test_no_semantic_authority_makes_zero_provider_calls():
    async def scenario():
        called = 0

        async def provider(_request):
            nonlocal called
            called += 1
            raise AssertionError("provider must not run without semantic authority")

        canon = _canon(2)
        snap = l2.materialize_final_snapshot({"book": NO_PREFIX}, canon=canon)
        run = await ext.extract_all(
            snap, canon, provider=provider, model_version="test-model", prompt_sha256="d" * 64,
            max_concurrency=2)
        assert called == 0
        assert run.logical_extractions == 0
        assert all(
            row.coverage_state == l2.COVERAGE_NO_CANON_AUTHORITY
            for row in run.claims)

    asyncio.run(scenario())


def test_extractor_is_bounded_and_results_stay_in_chapter_order():
    async def scenario():
        ent = cl.CanonEntityV1(
            entity_id="e1", canonical_name="Rina", aliases=(),
            alias_source="none")
        canon = _canon(4, entities=(ent,))
        text = "".join(f"## Bab {i}\nisi {i}\n" for i in range(1, 5))
        snap = l2.materialize_final_snapshot({"book": text}, canon=canon)

        async def provider(request):
            await asyncio.sleep((4 - request.chapter_index) * 0.001)
            return _provider_output()

        run = await ext.extract_all(
            snap, canon, provider=provider, model_version="test-model", prompt_sha256="d" * 64,
            max_concurrency=2)
        assert [row.chapter_index for row in run.claims] == [0, 1, 2, 3]
        assert run.max_concurrency_observed == 2
        assert run.logical_extractions == 4
        assert run.logical_attempts == 4
        assert run.tasks_started == run.tasks_drained == 4
        assert run.live_tasks_after == 0
        assert run.physical_attempts_state == ext.PHYSICAL_ATTEMPTS_UNKNOWN

    asyncio.run(scenario())


def test_provider_failure_retries_only_to_the_absolute_ceiling():
    async def scenario():
        ent = cl.CanonEntityV1(
            entity_id="e1", canonical_name="Rina", aliases=(),
            alias_source="none")
        canon = _canon(1, entities=(ent,))
        snap = l2.materialize_final_snapshot(
            {"book": "## Bab 1\nRina"}, canon=canon)
        calls = 0

        async def provider(_request):
            nonlocal calls
            calls += 1
            raise RuntimeError("provider-secret-prose")

        run = await ext.extract_all(
            snap, canon, provider=provider, model_version="test-model", prompt_sha256="d" * 64,
            max_concurrency=1, max_attempts=3)
        assert calls == run.logical_attempts == 3
        assert run.claims[0].coverage_state == l2.COVERAGE_PROVIDER_FAILURE
        assert "provider-secret-prose" not in repr(run)

    asyncio.run(scenario())


@pytest.mark.parametrize("timeout_s", [float("nan"), float("inf"), 121.0])
def test_extractor_timeout_must_remain_finite_and_bounded(timeout_s):
    async def scenario():
        ent = cl.CanonEntityV1(
            entity_id="e1", canonical_name="Rina", aliases=(),
            alias_source="none")
        canon = _canon(1, entities=(ent,))
        snap = l2.materialize_final_snapshot(
            {"book": "## Bab 1\nRina"}, canon=canon)

        async def provider(_request):
            return _provider_output()

        with pytest.raises(cl.CanonSchemaError, match="finite number"):
            await ext.extract_all(
                snap, canon, provider=provider, model_version="test-model", prompt_sha256="d" * 64,
                max_concurrency=1, timeout_s=timeout_s)

    asyncio.run(scenario())


def test_invalid_output_and_timeout_are_incomplete_never_clean():
    async def run_invalid():
        ent = cl.CanonEntityV1(
            entity_id="e1", canonical_name="Rina", aliases=(),
            alias_source="none")
        canon = _canon(1, entities=(ent,))
        snap = l2.materialize_final_snapshot(
            {"book": "## Bab 1\nRina"}, canon=canon)

        async def malformed(_request):
            return {"coverage": {}, "claims": [], "raw_prose": "must not survive"}

        run = await ext.extract_all(
            snap, canon, provider=malformed, model_version="test-model", prompt_sha256="d" * 64,
            max_concurrency=1, max_attempts=1)
        report = l2.build_report(
            snap, canon, mode="shadow", result={"book": "## Bab 1\nRina"},
            claims_by_index=run.claims_by_index)
        assert run.claims[0].coverage_state == l2.COVERAGE_INVALID_EXTRACTOR_OUTPUT
        assert report.continuity_status == l2.STATUS_UNCHECKED
        assert "raw_prose" not in repr(run)

        async def slow(_request):
            await asyncio.sleep(1)
            return _provider_output()

        timed = await ext.extract_all(
            snap, canon, provider=slow, model_version="test-model", prompt_sha256="d" * 64,
            max_concurrency=1, max_attempts=1, timeout_s=0.001)
        assert timed.claims[0].coverage_state == l2.COVERAGE_TIMEOUT
        assert timed.tasks_started == timed.tasks_drained == 1
        assert timed.live_tasks_after == 0

    asyncio.run(run_invalid())


def test_cancellation_waits_until_cancellation_hostile_provider_is_physically_dead():
    async def scenario():
        ent = cl.CanonEntityV1(
            entity_id="e1", canonical_name="Rina", aliases=(),
            alias_source="none")
        canon = _canon(1, entities=(ent,))
        snap = l2.materialize_final_snapshot(
            {"book": "## Bab 1\nRina"}, canon=canon)
        started = asyncio.Event()
        cancellation_seen = asyncio.Event()
        release = asyncio.Event()

        async def stubborn(_request):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()
                raise

        task = asyncio.create_task(ext.extract_all(
            snap, canon, provider=stubborn, model_version="test-model", prompt_sha256="d" * 64,
            max_concurrency=1))
        await started.wait()
        task.cancel()
        await cancellation_seen.wait()
        await asyncio.sleep(0)
        assert not task.done(), "a physically alive provider task was called drained"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_sibling_failure_drains_a_cancellation_hostile_provider_before_raising():
    """The explicit drain matters when gather raises because one sibling failed.

    Direct cancellation can itself remain coupled to `gather`; that does not prove the
    catch-path drain. A fatal child plus a cancellation-hostile sibling reaches the exact
    branch where returning early would leave provider work alive after terminalization.
    """
    class FatalProviderSignal(BaseException):
        pass

    async def scenario():
        ent = cl.CanonEntityV1(
            entity_id="e1", canonical_name="Rina", aliases=(),
            alias_source="none")
        canon = _canon(2, entities=(ent,))
        snap = l2.materialize_final_snapshot(
            {"book": "## Bab 1\nRina\n## Bab 2\nRina"}, canon=canon)
        sibling_started = asyncio.Event()
        cancellation_seen = asyncio.Event()
        release = asyncio.Event()

        async def provider(request):
            if request.chapter_index == 0:
                await sibling_started.wait()
                raise FatalProviderSignal()
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()
                raise

        task = asyncio.create_task(ext.extract_all(
            snap, canon, provider=provider, model_version="test-model", prompt_sha256="d" * 64,
            max_concurrency=2))
        await cancellation_seen.wait()
        await asyncio.sleep(0)
        assert not task.done(), "fatal sibling returned before live provider work drained"
        release.set()
        with pytest.raises(FatalProviderSignal):
            await task

    asyncio.run(scenario())


def test_extractor_module_has_no_real_provider_metering_or_persistence_import():
    source = (
        Path(__file__).resolve().parents[2] / "python" /
        "canon_lite_extractor.py").read_text()
    for forbidden in (
        "make_narasi_client", "log_usage", "import requests", "import httpx",
        "import aiohttp", "import metering", "import credits", "asyncpg", "redis",
    ):
        assert forbidden not in source, forbidden


# ===========================================================================
# Report boundary — D-L2-7 / §11.2
# ===========================================================================

def test_delivery_binding_matches_when_the_bytes_are_unchanged():
    result = {"book": PREFIXED}
    snap = l2.materialize_final_snapshot(result)
    assert l2.verify_delivery_binding(snap, result) == l2.BINDING_MATCH


def test_delivery_binding_mismatches_after_a_post_snapshot_mutation():
    """C8: a verdict bound to bytes that later changed is not a verdict at all."""
    result = {"book": PREFIXED}
    snap = l2.materialize_final_snapshot(result)
    result["book"] = PREFIXED + "\nsatu kata lagi"
    assert l2.verify_delivery_binding(snap, result) == l2.BINDING_MISMATCH


def test_report_never_substitutes_bytes_into_the_delivered_result():
    """§11.2 — L2 compares and reports; substitution is L3's authority."""
    result = {"book": PREFIXED, "output": ""}
    before = dict(result)
    canon = _canon(2)
    snap = l2.materialize_final_snapshot(result, canon=canon)
    l2.build_report(snap, canon, mode="shadow", result=result)
    assert result == before


def test_persistence_binding_is_unproved_and_blocks_a_clean_verdict():
    """The declared partial-C9 deviation, visible in every report rather than in a doc."""
    result = {"book": NO_PREFIX}
    canon = _canon(2)
    snap = l2.materialize_final_snapshot(result, canon=canon)
    rep = l2.build_report(snap, canon, mode="shadow", result=result)
    assert rep.persistence_binding == l2.BINDING_UNPROVED
    assert rep.continuity_status != l2.STATUS_CLEAN


def test_clean_is_reachable_only_when_both_bindings_settle():
    """Positive control: `clean` is not dead code, it is gated."""
    res = tuple(
        l2.PredicateResultV1(
            predicate=predicate,
            scope=l2._PREDICATE_SCOPE[predicate],
            coverage_state=l2.COVERAGE_NO_VIOLATIONS_FOUND,
            checked_units=2,
            total_units=2,
            violations=(),
        )
        for predicate in l2.ALL_PREDICATES
    )
    assert l2.decide_continuity_status(
        res, delivery_binding=l2.BINDING_MATCH,
        persistence_binding=l2.BINDING_NOT_APPLICABLE) == l2.STATUS_CLEAN
    assert l2.decide_continuity_status(
        res, delivery_binding=l2.BINDING_MATCH,
        persistence_binding=l2.BINDING_UNPROVED) == l2.STATUS_UNRESOLVED


def test_partial_predicate_set_can_never_be_called_clean():
    one_clean_result = (l2.PredicateResultV1(
        predicate=l2.PREDICATE_STRUCTURE,
        scope=l2.SCOPE_GLOBAL,
        coverage_state=l2.COVERAGE_NO_VIOLATIONS_FOUND,
        checked_units=2,
        total_units=2,
        violations=(),
    ),)
    assert l2.decide_continuity_status(
        one_clean_result,
        delivery_binding=l2.BINDING_MATCH,
        persistence_binding=l2.BINDING_NOT_APPLICABLE,
    ) == l2.STATUS_UNCHECKED


def test_any_bounded_violation_forces_the_violations_status():
    results = []
    for predicate in l2.ALL_PREDICATES:
        violations = ()
        state = l2.COVERAGE_NO_VIOLATIONS_FOUND
        if predicate == l2.PREDICATE_STRUCTURE:
            violations = (l2.ViolationV1(
                code=l2.VIOLATION_CHAPTER_COUNT,
                predicate=predicate,
                chapter_index=-1,
            ),)
            state = l2.COVERAGE_CHECKED
        results.append(l2.PredicateResultV1(
            predicate=predicate,
            scope=l2._PREDICATE_SCOPE[predicate],
            coverage_state=state,
            checked_units=2,
            total_units=2,
            violations=violations,
        ))
    assert l2.decide_continuity_status(
        tuple(results),
        delivery_binding=l2.BINDING_MATCH,
        persistence_binding=l2.BINDING_NOT_APPLICABLE,
    ) == l2.STATUS_VIOLATIONS


@pytest.mark.parametrize("state", [
    l2.COVERAGE_TIMEOUT, l2.COVERAGE_PROVIDER_FAILURE, l2.COVERAGE_NO_CANON_AUTHORITY,
    l2.COVERAGE_INCOMPLETE_EXTRACTION, l2.COVERAGE_INVALID_EXTRACTOR_OUTPUT,
])
def test_c7_unmeasured_coverage_can_never_become_clean(state):
    res = (l2.PredicateResultV1(
        predicate=l2.PREDICATE_STRUCTURE, scope=l2.SCOPE_GLOBAL,
        coverage_state=state, checked_units=0, total_units=2, violations=()),)
    assert l2.decide_continuity_status(
        res, delivery_binding=l2.BINDING_MATCH,
        persistence_binding=l2.BINDING_NOT_APPLICABLE) == l2.STATUS_UNCHECKED


def test_a_mismatched_binding_outranks_a_clean_predicate_sweep():
    res = (l2.PredicateResultV1(
        predicate=l2.PREDICATE_STRUCTURE, scope=l2.SCOPE_GLOBAL,
        coverage_state=l2.COVERAGE_NO_VIOLATIONS_FOUND,
        checked_units=2, total_units=2, violations=()),)
    assert l2.decide_continuity_status(
        res, delivery_binding=l2.BINDING_MISMATCH,
        persistence_binding=l2.BINDING_NOT_APPLICABLE) == l2.STATUS_UNRESOLVED


# ===========================================================================
# Privacy — C12 / §10
# ===========================================================================

def test_telemetry_carries_no_manuscript_byte_title_or_prose():
    result = {"book": PREFIXED}
    canon = _canon(2, titles=["Awal", "Lanjut"])
    snap = l2.materialize_final_snapshot(result, canon=canon)
    rep = l2.build_report(snap, canon, mode="shadow", result=result)
    blob = repr(l2.report_telemetry(
        rep, l2_status="present", mode="shadow"))
    for leaked in (
        "Kata pengantar", "isi satu", "isi dua", "Awal", "Lanjut", "Bab 1",
        "ch1", "ch2",
    ):
        assert leaked not in blob


def test_every_telemetry_count_travels_with_its_denominator():
    """A bare 0 is unreadable under the zero rule."""
    result = {"book": NO_PREFIX}
    snap = l2.materialize_final_snapshot(result)
    tel = l2.report_telemetry(
        l2.build_report(snap, None, mode="shadow", result=result),
        l2_status="present", mode="shadow")
    for predicate, cov in tel["coverage"].items():
        assert set(cov) == {"state", "checked", "total", "violations"}, predicate
        assert cov["total"] >= cov["checked"]


def test_absent_report_telemetry_is_unchecked_never_clean():
    tel = l2.report_telemetry(None, l2_status="absent", mode="shadow")
    assert tel["continuity_status"] == l2.STATUS_UNCHECKED
    assert tel["manuscript_sha256"] == cl.UNKNOWN


def test_present_and_absent_telemetry_share_one_closed_top_level_keyset():
    result = {"book": NO_PREFIX}
    snapshot = l2.materialize_final_snapshot(result)
    present = l2.report_telemetry(
        l2.build_report(snapshot, None, mode="shadow", result=result),
        l2_status="present",
        mode="shadow",
    )
    absent = l2.report_telemetry(None, l2_status="absent", mode="shadow")
    assert set(present) == set(absent)


def test_present_status_requires_a_report_and_vice_versa():
    with pytest.raises(cl.CanonSchemaError):
        l2.report_telemetry(None, l2_status="present", mode="shadow")
    result = {"book": NO_PREFIX}
    snap = l2.materialize_final_snapshot(result)
    rep = l2.build_report(snap, None, mode="shadow", result=result)
    with pytest.raises(cl.CanonSchemaError):
        l2.report_telemetry(rep, l2_status="absent", mode="shadow")


def test_report_digest_is_stable_and_domain_separated():
    result = {"book": NO_PREFIX}
    snap = l2.materialize_final_snapshot(result)
    canon = _canon(2)
    snap = l2.materialize_final_snapshot(result, canon=canon)
    a = l2.build_report(snap, canon, mode="shadow", result=result)
    b = l2.build_report(snap, canon, mode="shadow", result=result)
    assert l2.report_digest(a) == l2.report_digest(b)
    assert l2.report_digest(a) != cl._digest("canon_lite_l2.continuity_report_other",
                                             a.to_canonical_obj())


# ===========================================================================
# Real final-seam integration and C11
# ===========================================================================

def test_real_narration_helper_materializes_and_reports_without_mutating_result():
    import narration_api as na

    canon = _canon(2)
    result = {"book": NO_PREFIX, "chapters": [{"content": "older durable bytes"}]}
    before = {
        "book": result["book"],
        "chapters": [dict(result["chapters"][0])],
    }
    # DG-4 made this helper async: the metered wave is awaited inside it. On this host
    # the runner's gates refuse (no host sentinel), so claims_by_index stays None and
    # the projection is byte-identical to the pre-DG-4 behaviour.
    telemetry = asyncio.run(na._canon_lite_l2_shadow_projection(
        result, mode="shadow", canon=canon, wave_token=ext.ExtractionWaveToken()))
    assert result == before
    assert telemetry["l2_status"] == "present"
    assert telemetry["mode"] == "shadow"
    assert telemetry["delivery_binding"] == l2.BINDING_MATCH
    assert telemetry["persistence_binding"] == l2.BINDING_UNPROVED
    assert telemetry["continuity_status"] != l2.STATUS_CLEAN


def test_flag_off_helper_returns_before_any_l2_import(monkeypatch):
    import builtins
    import narration_api as na

    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "canon_lite_l2":
            raise AssertionError("flag-off imported Canon Lite L2")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    assert asyncio.run(na._canon_lite_l2_shadow_projection(
        {"book": NO_PREFIX}, mode="off", canon=None, wave_token=None)) is None


def test_l2_final_seam_is_after_post_gates_dedup_and_before_persistence():
    source = (
        Path(__file__).resolve().parents[2] / "python" /
        "narration_api.py").read_text()
    tree = ast.parse(source)
    job_body = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_run_narration_job_after_parity"
    )

    def called_name(call):
        # DG-4 wraps the projection call in `await`; ast.Call is still the node we
        # count, so ordering is unaffected — but the helper is resolved through the
        # Await node's value, not the statement.
        return call.func.id if isinstance(call.func, ast.Name) else None

    calls = [node for node in ast.walk(job_body) if isinstance(node, ast.Call)]
    projection = [
        node for node in calls
        if called_name(node) == "_canon_lite_l2_shadow_projection"]
    persistence = [
        node for node in calls if called_name(node) == "_persist_chapters"]
    final_dedup = [node for node in calls if called_name(node) == "_dedup_final"]
    assert len(projection) == len(persistence) == len(final_dedup) == 1
    assert final_dedup[0].lineno < projection[0].lineno < persistence[0].lineno


def test_flag_off_static_return_has_no_private_l2_transit_key_by_construction():
    source = (
        Path(__file__).resolve().parents[2] / "python" /
        "orchestrator" / "static.py").read_text()
    tree = ast.parse(source)
    # 🔴 LOCATED STRUCTURALLY, NOT BY LINE NUMBER. This used to select "the first
    #    `return` between lines 1400 and 1600", which is a proxy for the terminal
    #    return that stops being one the moment anything above it grows or a
    #    nested helper acquires a `return` inside the window — the control then
    #    quietly starts asserting about some other statement. Taking the last
    #    top-level statement of `narrate_chapters` itself names the thing the test
    #    is actually about, and cannot drift.
    fn = next(
        (node for node in ast.walk(tree)
         if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
         and node.name == "narrate_chapters"), None)
    assert fn is not None, "narrate_chapters not found"
    terminal_node = fn.body[-1]
    assert isinstance(terminal_node, ast.Return), (
        "the last statement of narrate_chapters is not its terminal return "
        f"(got {type(terminal_node).__name__})")
    terminal = ast.get_source_segment(source, terminal_node.value)
    # The gate widened from shadow to shadow|assist when L3-ASSIST began consuming
    # the same transit key. The PROPERTY is unchanged and is what is asserted: the
    # key is included CONDITIONALLY, by construction, so a flag-off return cannot
    # carry it. Only the set of modes that qualify has grown.
    assert '_cl_mode in ("shadow", "assist")' in terminal
    assert '"_canon_lite_canon"' in terminal
    assert '"off"' not in terminal


@pytest.mark.parametrize("projection_fails", [False, True])
def test_shared_real_job_body_consumes_private_canon_before_persist_and_finalize(
    monkeypatch, caplog, projection_fails,
):
    """Both routes share this body; shadow-report failure must preserve delivery."""
    import narration_api as na

    canon = _canon(2)
    generated = {
        "ok": True, "book": NO_PREFIX, "chapters": [],
        "_canon_lite_canon": canon,
        "_canon_lite_canon_status": "present",
    }
    persisted = []
    finalized = []

    async def generate(_req, **_):
        return dict(generated)

    async def never_cancel(_job_id):
        await asyncio.Event().wait()

    async def noop(*_args, **_kwargs):
        return None

    async def persist(_tenant_id, _job_uuid, result):
        persisted.append(dict(result))

    async def finalize(*args, **kwargs):
        finalized.append((args, kwargs))

    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    monkeypatch.setattr(na, "generate_narration", generate)
    monkeypatch.setattr(na, "_cancel_watcher", never_cancel)
    monkeypatch.setattr(na, "_set_status", noop)
    monkeypatch.setattr(na, "_safe_progress", noop)
    monkeypatch.setattr(na, "_reconcile_checkboxes", noop)
    monkeypatch.setattr(na, "_apply_v3_gates", noop)
    monkeypatch.setattr(na, "_persist_chapters", persist)
    monkeypatch.setattr(na, "_finalize", finalize)
    monkeypatch.setattr(na, "_settle", noop)
    monkeypatch.setattr(na, "_refund", noop)
    monkeypatch.setattr(na, "_p0a", noop)
    monkeypatch.setattr(na.db, "get_known_bad_claims", noop)
    monkeypatch.setattr(na.db, "get_known_good_claims", noop)
    if projection_fails:
        async def fail_projection(*_args, **_kwargs):
            raise RuntimeError("provider-or-tenant-prose-must-not-leak")
        monkeypatch.setattr(
            na, "_canon_lite_l2_shadow_projection", fail_projection)

    with caplog.at_level("INFO"):
        asyncio.run(na._run_narration_job_after_parity(
            body={}, job_id="j", job_uuid=None, tenant_id="t", user_id=None,
            total=2, meter_op=None, model="m", executor="narration_worker"))

    assert len(persisted) == len(finalized) == 1
    assert "_canon_lite_canon" not in persisted[0]
    assert "_canon_lite_canon_status" not in persisted[0]
    assert persisted[0]["book"] == NO_PREFIX
    final_payload = finalized[0][1]["result"]
    assert final_payload["markdown"] == NO_PREFIX
    assert "_canon_lite_canon" not in repr(final_payload)
    messages = [record.getMessage() for record in caplog.records]
    if projection_fails:
        assert any(
            message == (
                "canon lite l2: report unavailable "
                "(error_code=l2_report_error)")
            for message in messages)
        assert all("provider-or-tenant-prose" not in message for message in messages)
    else:
        assert any(
            message.startswith("canon lite l2:")
            and "'l2_status': 'present'" in message
            for message in messages)


# ===========================================================================
# Module hygiene
# ===========================================================================

def test_l2_module_performs_no_provider_or_persistence_io():
    """L2a is offline by construction — the import surface is the cheapest proof."""
    src = (Path(__file__).resolve().parents[2] / "python" / "canon_lite_l2.py").read_text()
    for forbidden in ("import requests", "import httpx", "import aiohttp",
                      "make_narasi_client", "log_usage", "_persist_chapters",
                      "asyncpg", "redis"):
        assert forbidden not in src, forbidden


def test_every_closed_vocabulary_is_actually_closed():
    for value, allowed in (
        ("NOT_A_STATE", l2.COVERAGE_STATES),
        ("NOT_A_BINDING", l2.BINDING_STATES),
        ("NOT_A_STATUS", l2.CONTINUITY_STATUSES),
        ("NOT_A_REASON", l2.EXTRACT_REASON_CODES),
    ):
        assert value not in allowed
    # The reason vocabulary must stay DISJOINT from the coverage vocabulary, not merely
    # closed: canon_lite_extractor logs one `error_code` field carrying either kind, so an
    # overlapping member would make a log line ambiguous about which vocabulary it names.
    assert not set(l2.EXTRACT_REASON_CODES) & set(l2.COVERAGE_STATES)
    assert len(set(l2.EXTRACT_REASON_CODES)) == len(l2.EXTRACT_REASON_CODES)


# ===========================================================================
# The locator is not the evidence — repeated names, repeated literals, overlap
# ===========================================================================
#
# 🔴 THE DEFECT THESE ROWS EXIST FOR. A first version of the quote contract required the
#    quote itself to be unique in the chapter. That silently broke the evaluator, which
#    compares the evidence span against `{canonical_name, *aliases}`: a name appearing
#    twice then had NO valid representation. "Rina" was refused as ambiguous, and "Rina
#    datang" — widened until unique — was compared whole against the canon and reported as
#    an `entity_name_contradiction` the manuscript never committed. A repeated name is the
#    ordinary case in a novel, so the contract was wrong for most real chapters.
#
#    `context` locates; `quote` is evaluated. These rows hold that separation from both
#    sides: the claim must parse, AND it must not manufacture a violation.

def _claim(quote, *, canon_ref="e1", claim_type=None, context=None):
    claim = {"claim_type": claim_type or l2.CLAIM_ENTITY_MENTION,
             "canon_ref": canon_ref, "quote": quote}
    if context is not None:
        claim["context"] = context
    return claim


def test_a_repeated_entity_name_is_located_by_context_and_evaluates_clean():
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="Rina", aliases=(),
                           alias_source="none")
    canon = _canon(2, entities=(ent,))
    text = "## Bab 1\nRina datang. Rina pergi.\n## Bab 2\nlain"
    snap = l2.materialize_final_snapshot({"book": text}, canon=canon)
    assert snap.block_bytes(0).count(b"Rina") == 2, "the repeat under test is real"

    art = l2.parse_chapter_claims(
        _claim_payload(snap, canon=canon,
                       claims=[_claim("Rina", context="Rina pergi")]),
        snapshot=snap, canon=canon)

    # the SPAN is the bare name, not the locator
    block = snap.block_bytes(0)
    c = art.claims[0]
    assert block[c.evidence_start:c.evidence_end].decode("utf-8") == "Rina"
    # and it is the SECOND occurrence — the one the context named
    assert c.evidence_start == block.rindex(b"Rina")

    # Chapter 2 must be represented too: a missing unit is PARTIAL coverage, which
    # reports INCOMPLETE_EXTRACTION and would mask whether chapter 1 evaluated clean.
    empty = l2.parse_chapter_claims(
        _claim_payload(snap, idx=1, canon=canon, claims=[],
                       coverage={p: l2.COVERAGE_NO_CLAIMS_FOUND
                                 for p in l2.SEMANTIC_PREDICATES}),
        snapshot=snap, canon=canon)
    result = l2.evaluate_semantic(
        l2.PREDICATE_ENTITY_NAME, snap, canon, {0: art, 1: empty})
    # NO_VIOLATIONS_FOUND, not CHECKED: the server derives the predicate state as
    # `CHECKED if violations else NO_VIOLATIONS_FOUND`, so "measured and clean" is
    # precisely this value. Both are in `_PREDICATE_MEASURED`.
    assert result.coverage_state == l2.COVERAGE_NO_VIOLATIONS_FOUND
    assert result.coverage_state in l2._PREDICATE_MEASURED
    assert result.violations == (), (
        "a correctly located repeated name became a contradiction — the locator leaked "
        "into the value the evaluator compares")


def test_a_repeated_fixed_literal_is_located_by_context_and_evaluates_clean():
    anchor = cl.CanonAnchorV1(anchor_id="a1", kind="time", literal="10:00")
    canon = _canon(2, anchors=(anchor,))
    text = "## Bab 1\nRapat 10:00 lalu 10:00 lagi\n## Bab 2\nlain"
    snap = l2.materialize_final_snapshot({"book": text}, canon=canon)
    assert snap.block_bytes(0).count(b"10:00") == 2

    art = l2.parse_chapter_claims(
        _claim_payload(snap, canon=canon,
                       coverage={**{p: l2.COVERAGE_NO_CLAIMS_FOUND
                                    for p in l2.SEMANTIC_PREDICATES},
                                 l2.PREDICATE_FIXED_LITERAL: l2.COVERAGE_CHECKED},
                       claims=[_claim("10:00", canon_ref="a1",
                                      claim_type=l2.CLAIM_FIXED_LITERAL,
                                      context="lalu 10:00 lagi")]),
        snapshot=snap, canon=canon)

    block = snap.block_bytes(0)
    c = art.claims[0]
    assert block[c.evidence_start:c.evidence_end].decode("utf-8") == "10:00"
    result = l2.evaluate_semantic(l2.PREDICATE_FIXED_LITERAL, snap, canon, {0: art})
    assert result.violations == ()


def test_an_overlapping_second_occurrence_is_seen_as_ambiguous():
    """🔴 `bytes.count()` COUNTS NON-OVERLAPPING MATCHES. `b"aaaa".count(b"aaa")` is 1,
    while "aaa" genuinely starts at offsets 0 and 1. A uniqueness check built on `count()`
    accepts this quote and binds it to the first offset — a silent mis-location that looks
    exactly like a correct one."""
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="aaa", aliases=(),
                           alias_source="none")
    canon = _canon(2, entities=(ent,))
    snap = l2.materialize_final_snapshot(
        {"book": "## Bab 1\naaaa\n## Bab 2\nlain"}, canon=canon)
    block = snap.block_bytes(0)
    assert block.count(b"aaa") == 1, "count() under-reports, which is the bug"
    assert block.find(b"aaa", block.find(b"aaa") + 1) != -1, "but it truly repeats"

    with pytest.raises(cl.CanonSchemaError, match="ambiguous"):
        l2.parse_chapter_claims(
            _claim_payload(snap, canon=canon, claims=[_claim("aaa")]),
            snapshot=snap, canon=canon)


def test_a_context_that_does_not_contain_the_quote_is_rejected():
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="Rina", aliases=(),
                           alias_source="none")
    canon = _canon(2, entities=(ent,))
    snap = l2.materialize_final_snapshot(
        {"book": "## Bab 1\nRina datang. Rina pergi.\n## Bab 2\nlain"}, canon=canon)
    with pytest.raises(cl.CanonSchemaError, match="context does not contain the quote"):
        l2.parse_chapter_claims(
            _claim_payload(snap, canon=canon,
                           claims=[_claim("Rina", context="datang.")]),
            snapshot=snap, canon=canon)


def test_an_ambiguous_context_is_rejected_rather_than_resolved():
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="Rina", aliases=(),
                           alias_source="none")
    canon = _canon(2, entities=(ent,))
    snap = l2.materialize_final_snapshot(
        {"book": "## Bab 1\nRina pergi. Rina pergi.\n## Bab 2\nlain"}, canon=canon)
    with pytest.raises(cl.CanonSchemaError, match="context is ambiguous"):
        l2.parse_chapter_claims(
            _claim_payload(snap, canon=canon,
                           claims=[_claim("Rina", context="Rina pergi")]),
            snapshot=snap, canon=canon)


def test_the_evidence_size_bound_is_actually_reached():
    """The bound must be hit by a quote that IS in the chapter.

    An earlier version used `"x" * (MAX + 1)`, which is absent from the chapter, so the
    parse died at `quote not found` and `MAX_EVIDENCE_BYTES` was never exercised — a
    vacuous row whose comment claimed otherwise. The quote here is real text from the
    block, so the size check is the only thing left to fail.
    """
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="Rina", aliases=(),
                           alias_source="none")
    canon = _canon(2, entities=(ent,))
    long_run = "x" * (l2.MAX_EVIDENCE_BYTES + 50)
    snap = l2.materialize_final_snapshot(
        {"book": f"## Bab 1\n{long_run}\n## Bab 2\nlain"}, canon=canon)
    assert long_run.encode("utf-8") in snap.block_bytes(0), "the quote is really present"

    with pytest.raises(cl.CanonBoundsError):
        l2.parse_chapter_claims(
            _claim_payload(snap, canon=canon, claims=[_claim(long_run)]),
            snapshot=snap, canon=canon)


# ===========================================================================
# one_time_event quote semantics — P1 audit finding, 2026-08-13
# ===========================================================================
# The prompt used to define "quote" as "the name or literal ALONE" for every claim_type.
# True for entity_mention/fixed_literal; false for one_time_event — evaluate_semantic never
# reads a one_time_event claim's evidence text (see its `else` branch above), and
# CanonEventV1 carries no literal to compare it against in the first place. Quote semantics
# are now defined per claim_type (canon_lite_qc_provider's QC_SYSTEM_TEMPLATE): for events,
# the shortest verbatim phrase that shows the event happened. These rows prove the parser
# accepts a phrase-shaped quote through the same mechanism a name already uses, and that
# the evaluator genuinely does not care what that phrase says.

def test_one_time_event_quote_can_be_a_phrase_located_by_context():
    """Unlike entity_mention/fixed_literal, an event's quote is not a bare name — the
    contract defines it as the shortest verbatim phrase that shows the event happened. The
    parser's quote+context mechanism is claim-type-agnostic, so a multi-word phrase must
    locate exactly the way a repeated name already does."""
    event = cl.CanonEventV1(event_id="ev1", occurs_chapter_order=1, label="pintu pecah")
    canon = _canon(2, events=(event,))
    text = ("## Bab 1\npintu itu pecah. Lalu pintu itu pecah lagi dalam mimpinya.\n"
            "## Bab 2\nlain")
    snap = l2.materialize_final_snapshot({"book": text}, canon=canon)
    block = snap.block_bytes(0)
    assert block.count("pintu itu pecah".encode("utf-8")) == 2, \
        "the repeat under test is real"

    art = l2.parse_chapter_claims(
        _claim_payload(
            snap, canon=canon,
            coverage={**{p: l2.COVERAGE_NO_CLAIMS_FOUND for p in l2.SEMANTIC_PREDICATES},
                      l2.PREDICATE_ONE_TIME_EVENT: l2.COVERAGE_CHECKED},
            claims=[_claim("pintu itu pecah", canon_ref="ev1",
                           claim_type=l2.CLAIM_ONE_TIME_EVENT,
                           context="Lalu pintu itu pecah lagi")]),
        snapshot=snap, canon=canon)

    c = art.claims[0]
    assert block[c.evidence_start:c.evidence_end].decode("utf-8") == "pintu itu pecah"
    assert c.evidence_start == block.rindex("pintu itu pecah".encode("utf-8")), \
        "must bind to the occurrence the context named, not the first one"


def test_claim_rejections_carry_distinct_closed_reason_codes():
    """§10.3 / P2 audit finding: canon_lite_extractor's telemetry narrows
    `INVALID_EXTRACTOR_OUTPUT` using `.reason_code`, set here. Prove it at the source:
    three genuinely different claims-loop rejections must carry three different codes, not
    one shared code reused regardless of cause."""
    ent = cl.CanonEntityV1(entity_id="e1", canonical_name="Rina", aliases=(),
                           alias_source="none")
    canon = _canon(2, entities=(ent,))
    snap = l2.materialize_final_snapshot(
        {"book": "## Bab 1\nRina pergi\n## Bab 2\nlain"}, canon=canon)

    def _raised(**claim_kwargs):
        with pytest.raises(cl.CanonSchemaError) as exc:
            l2.parse_chapter_claims(
                _claim_payload(snap, canon=canon, claims=[_claim(**claim_kwargs)]),
                snapshot=snap, canon=canon)
        return exc.value

    not_found = _raised(quote="ABSENT_FROM_THE_CHAPTER")
    bad_ref = _raised(quote="Rina", canon_ref="not-a-real-id")
    ctx_not_found = _raised(quote="Rina", context="ABSENT CONTEXT")

    assert not_found.reason_code == l2.EXTRACT_REASON_QUOTE_NOT_FOUND
    assert bad_ref.reason_code == l2.EXTRACT_REASON_CANON_REF_INVALID
    assert ctx_not_found.reason_code == l2.EXTRACT_REASON_CONTEXT_NOT_FOUND
    codes = {not_found.reason_code, bad_ref.reason_code, ctx_not_found.reason_code}
    assert len(codes) == 3, "three distinct causes must not collapse into fewer codes"
    assert codes <= set(l2.EXTRACT_REASON_CODES)
