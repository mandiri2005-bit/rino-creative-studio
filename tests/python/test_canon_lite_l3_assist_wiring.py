"""L3-ASSIST — the delivery seam.

Stage 1 proved every chapter was written against one canon. Stage 2 proved the
repair engine only accepts a candidate that actually repairs. Neither says
anything about whether the repaired bytes reach the reader, and that is what this
file is for.

🔴 THE ONE CLAIM: the bytes the continuity verdict was computed over are the bytes
   that get STORED and the bytes that get DELIVERED. Three things, one hash. A
   repair that is computed, reported and never substituted is indistinguishable
   from one that landed unless somebody reads the delivery path back.

🔴 AND NON-ASSIST MUST BE UNTOUCHED. `off` and `shadow` run through the same
   function; if the seam changes a single byte for them it has changed production
   behaviour for every job that is not opted in.
"""
import asyncio
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import canon_lite as cl                       # noqa: E402
import canon_lite_l2 as l2                    # noqa: E402
import canon_lite_l3_repair as rp             # noqa: E402
import narration_api as na                    # noqa: E402

from test_canon_lite_l3_repair_stage2 import (  # noqa: E402
    BAD_NAME, BOOK, GOOD_NAME, _Extractor, _Provider, _claims_for, _setup)


def _seam_snapshot(canon, book=BOOK):
    """The snapshot the seam itself builds — with canon, so chapter ids match."""
    return l2.materialize_final_snapshot({"book": book}, canon=canon)


@pytest.fixture
def wave(monkeypatch):
    """Stand in for the metered extraction wave the seam asks for its claims.

    Without it the seam gets no claims, every semantic predicate reports
    INCOMPLETE_EXTRACTION, and there is nothing to repair — which is correct
    behaviour and a useless fixture.
    """
    import canon_lite_qc_runner as qcr

    def _install(canon, book=BOOK):
        snap = _seam_snapshot(canon, book)
        claims = {
            0: _claims_for(snap.block_bytes(0), index=0,
                           chapter_id=snap.blocks[0].chapter_id, canon=canon,
                           needle=GOOD_NAME),
            1: _claims_for(snap.block_bytes(1), index=1,
                           chapter_id=snap.blocks[1].chapter_id, canon=canon,
                           needle=BAD_NAME),
        }

        async def _fake(*_a, **_k):
            return claims

        monkeypatch.setattr(qcr, "maybe_run_metered_wave", _fake)
        return snap

    return _install


@pytest.fixture(autouse=True)
def _no_repairer():
    """Restore the seam's injected hooks; they are process-global."""
    before = (na._L3_REPAIR_PROVIDER, na._L3_CHAPTER_EXTRACTOR)
    yield
    na._L3_REPAIR_PROVIDER, na._L3_CHAPTER_EXTRACTOR = before


def _install(provider, extractor):
    na._L3_REPAIR_PROVIDER = provider
    na._L3_CHAPTER_EXTRACTOR = extractor


def _fixed(snap):
    return snap.block_bytes(1).replace(BAD_NAME.encode(), GOOD_NAME.encode())


def _real_result(book=BOOK):
    """A result shaped like the one the job actually builds.

    `chapters[*].content` is the chapter BODY and `book` is the assembly of those
    bodies under their headings. Both are live: the payload ships the book, and
    `_persist_chapters` writes the bodies into narasi_chapters, where the reader
    can read them back. A fixture carrying only `book` cannot see the second one
    at all — which is how the seam shipped storing pre-repair prose.
    """
    bodies = [seg.split("\n", 1)[1].rstrip("\n")
              for seg in book.split("## ")[1:]]
    return {
        "book": book,
        "chapters": [{"no": i, "ok": True, "content": body}
                     for i, body in enumerate(bodies)],
        "n_ok": len(bodies), "n_total": len(bodies),
    }


def _seam(result, *, mode, canon):
    return asyncio.run(na._canon_lite_l3_assist_repair(
        result, mode=mode, canon=canon, wave_token=None, run_id="j1"))


# ── 1. non-assist modes are untouched ───────────────────────────────────────
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_non_assist_modes_do_not_enter_the_seam(mode):
    snap, canon, _claims, _r = _setup()
    _install(_Provider(lambda b: _fixed(snap)), _Extractor())
    result = _real_result()
    before = copy.deepcopy(result)
    assert _seam(result, mode=mode, canon=canon) is None
    assert result == before, f"mode={mode} had its manuscript rewritten"


def test_assist_without_a_metered_session_measures_and_stops():
    """🔴 NO SESSION MEANS NO RE-EXTRACTION, AND A REPAIR THAT CANNOT BE RE-CHECKED
    CANNOT BE ACCEPTED.

    The metered wave is refused on most hosts — that is its normal production
    outcome, not an error. When it is, there is no provider to re-read a candidate
    with, so assist measures and stops rather than accepting anything unverified.
    It is recorded, not silent.
    """
    _, canon, _claims, _r = _setup()
    result = _real_result()
    before = copy.deepcopy(result)
    assert na._L3_REPAIR_PROVIDER is None and na._L3_CHAPTER_EXTRACTOR is None

    outcome = _seam(result, mode="assist", canon=canon)
    assert outcome["stage"] == "no_session"
    assert outcome["outcome"] == "unchecked"
    assert outcome["chapters_repaired"] == 0
    before["canon_lite_l3"] = result.get("canon_lite_l3")
    assert result == before, "the manuscript moved without a session to verify it"


# ── 2. the substitution, and the proof that it is the delivered text ────────
def test_an_accepted_repair_is_substituted_and_binds_to_the_delivered_bytes(wave):
    _, canon, _claims, _r = _setup()
    snap = wave(canon)
    _install(_Provider(lambda b: _fixed(snap)), _Extractor())
    result = _real_result()

    telemetry = _seam(result, mode="assist", canon=canon)
    assert telemetry and telemetry["l3_status"] == "present"
    assert telemetry["chapters_repaired"] == 1
    assert telemetry["manuscript_changed"] is True

    # The delivered text moved, and its hash is the one the verdict was bound to.
    assert BAD_NAME not in result["book"]
    assert cl.sha256_hex(result["book"].encode("utf-8")) == \
        telemetry["manuscript_sha256_after"]
    assert telemetry["delivery_binding"] == l2.BINDING_MATCH


def test_the_stored_row_and_the_delivered_payload_are_the_same_bytes(wave):
    """🔴 VALIDATED == STORED == DELIVERED, checked at the two real consumers.

    `_persist_chapters` writes the durable rows and `_result_payload` builds the
    bounded payload; both read `result` AFTER the seam. If the seam ran late, or
    substituted into a different key, these two would carry the pre-repair book
    while the verdict described the repaired one.
    """
    _, canon, _claims, _r = _setup()
    snap = wave(canon)
    _install(_Provider(lambda b: _fixed(snap)), _Extractor())
    result = _real_result()

    telemetry = _seam(result, mode="assist", canon=canon)
    validated = telemetry["manuscript_sha256_after"]

    # ── the DURABLE rows, through the real writer ────────────────────────────
    rows = []

    class _DB:
        async def save_narasi_chapter(self, tenant_id, job_uuid, index, content,
                                      **kw):
            rows.append({"index": index, "content": content})

    monkey = _DB()
    real_db = na.db
    na.db = monkey
    try:
        asyncio.run(na._persist_chapters("t-1", "job-uuid", result))
    finally:
        na.db = real_db

    assert [r["index"] for r in rows] == [0, 1]
    assert BAD_NAME not in "".join(r["content"] for r in rows), \
        "the durable chapter rows still hold the pre-repair prose"

    # ── and the bounded payload ──────────────────────────────────────────────
    payload = na._result_payload(result)
    assert cl.sha256_hex(payload["markdown"].encode("utf-8")) == validated
    assert BAD_NAME not in payload["markdown"]

    # 🔴 ONE HASH, THREE CONSUMERS. Every stored body must appear verbatim in the
    #    validated manuscript — otherwise the rows and the book are two different
    #    versions of the same chapter and the verdict describes only one of them.
    book = payload["markdown"]
    for row in rows:
        assert row["content"] in book, (
            f"chapter {row['index']} was stored with prose that is not in the "
            "manuscript the continuity verdict was computed over")
    assert payload["canon_lite_l3"]["manuscript_sha256"] == validated
    assert payload["canon_lite_l3"]["chapters_repaired"] == 1


def test_nothing_is_substituted_when_no_repair_was_accepted(wave):
    """A write on a job that repaired nothing would make "assist ran" and "the
    text moved" the same observation. They are not the same, and the difference
    is what an audit reads."""
    _, canon, _claims, _r = _setup()
    snap = wave(canon)
    ineffective = snap.block_bytes(1).replace(b"di kota itu", b"di kota besar")
    _install(_Provider(lambda b: ineffective), _Extractor())
    result = _real_result()
    original = result["book"]

    telemetry = _seam(result, mode="assist", canon=canon)
    assert telemetry["chapters_repaired"] == 0
    assert telemetry["manuscript_changed"] is False
    assert result["book"] is original, "the manuscript key was rewritten in place"


# ── 3. the seam never becomes a refusal ─────────────────────────────────────
def test_an_unresolved_verdict_still_delivers(wave):
    """🔴 THE ENFORCE LEAK, TESTED. Everything needed to block a delivery is in
    hand once the re-check has run. Assist reports and ships; refusing is a
    separate deferred project."""
    _, canon, _claims, _r = _setup()
    wave(canon)
    _install(_Provider(lambda b: b"## Bab 2\nrusak.\n"), _Extractor())
    result = _real_result()
    telemetry = _seam(result, mode="assist", canon=canon)
    assert telemetry["l3_status"] == "present"
    assert result["book"] == BOOK, "an unresolved verdict withheld the manuscript"


def test_a_raising_repairer_does_not_break_delivery():
    class _Boom:
        async def __call__(self, **kw):
            raise RuntimeError("repairer exploded")

    _, canon, _claims, _r = _setup()
    _install(_Boom(), _Extractor())
    result = _real_result()
    telemetry = _seam(result, mode="assist", canon=canon)
    assert telemetry["l3_status"] in ("present", "error")
    assert result["book"] == BOOK


def test_a_manuscript_the_seam_cannot_locate_is_left_alone():
    _, canon, _claims, _r = _setup()
    _install(_Provider(lambda b: b), _Extractor())
    result = {"nothing": "here"}
    outcome = _seam(result, mode="assist", canon=canon)
    assert outcome["outcome"] == "unchecked" and outcome["stage"] == "no_manuscript"
    # The manuscript is untouched, but the run is on the record: a path that
    # returns without recording is a job that ran assist and cannot say so.
    assert result["nothing"] == "here"
    assert result["canon_lite_l3"]["outcome"] == "unchecked"


# ── 4. the durable row must be byte-exact, not merely a superstring ─────────
def test_a_candidate_that_moves_the_block_framing_is_not_written_back(wave):
    """🔴 A SUBSTRING CHECK PASSES THIS ONE. The repaired body is still inside the
    new block and the wrapper still matches at both ends — the candidate only
    appended a newline. Reapplying the old offsets absorbs that byte into the
    BODY, so the durable row gains a trailing newline the manuscript never
    authorised, and nothing downstream re-reads the row against the book.

    Framing belongs to the assembler. A repair may change prose and nothing else.
    """
    _, canon, _claims, _r = _setup()
    snap = wave(canon)
    _install(_Provider(lambda b: _fixed(snap) + b"\n"), _Extractor())
    result = _real_result()
    bodies_before = [r["content"] for r in result["chapters"]]

    outcome = _seam(result, mode="assist", canon=canon)

    assert outcome["outcome"] == "unresolved"
    assert outcome["stage"] == "unsyncable"
    # Byte-exact, not "still contains the old text".
    assert [r["content"] for r in result["chapters"]] == bodies_before
    assert result["book"] == BOOK
    assert result["canon_lite_l3"]["outcome"] == "unresolved"


def test_a_polished_book_that_no_longer_matches_the_records_refuses_the_write(wave):
    """🔴 THE COMMON CASE, NOT AN EXOTIC ONE. `polish` is on by default and
    rewrites the assembled book without touching the chapter records, so by the
    time assist runs the rows and the manuscript can already be two different
    books. Substituting into one of them leaves repaired prose in the book and
    unrepaired prose in the rows, both live, and the verdict describing only one.

    The mismatch here is in a chapter NOBODY REPAIRED — which is why the census
    has to cover every index rather than only the ones that were touched.
    """
    _, canon, _claims, _r = _setup()
    snap = wave(canon)
    _install(_Provider(lambda b: _fixed(snap)), _Extractor())
    result = _real_result()
    # Chapter 1 was polished after its record was captured.
    result["chapters"][0]["content"] = "Namanya Suranto di desa lain."
    bodies_before = [r["content"] for r in result["chapters"]]

    outcome = _seam(result, mode="assist", canon=canon)

    assert outcome["stage"] == "unsyncable"
    assert outcome["outcome"] == "unresolved"
    assert [r["content"] for r in result["chapters"]] == bodies_before
    assert result["book"] == BOOK, "the book moved while the rows did not"


@pytest.mark.parametrize("stage,mutate", [
    ("no_manuscript", lambda r: r.clear()),
    ("unsyncable", lambda r: r["chapters"].pop()),
])
def test_every_assist_path_records_a_bounded_outcome(wave, stage, mutate):
    """No silent exits. Each unhappy path is the one an audit needs to see."""
    _, canon, _claims, _r = _setup()
    snap = wave(canon)
    _install(_Provider(lambda b: _fixed(snap)), _Extractor())
    result = _real_result()
    mutate(result)

    outcome = _seam(result, mode="assist", canon=canon)
    assert outcome["stage"] == stage
    assert outcome["outcome"] in na.L3_OUTCOMES
    assert result["canon_lite_l3"] == outcome
    # Bounded: labels, counts and hashes only.
    blob = repr(outcome)
    for leak in (GOOD_NAME, BAD_NAME, "Namanya", "desa", "kota"):
        assert leak not in blob


def test_a_repair_pass_that_never_concluded_is_unchecked_not_clean():
    """🔴 THE ENGINE ITSELF FAILING, not a provider inside it.

    A raising PROVIDER is caught by the engine and becomes a reported outcome, so
    it never reaches the seam's error branch. Only a broken engine contract does —
    and that branch must record `unchecked`, because nothing concluded. Recording
    `clean` there would turn every future crash into an all-clear.
    """
    _, canon, _claims, _r = _setup()
    _install(_Provider(lambda b: b), object())      # extractor is not callable
    result = _real_result()
    outcome = _seam(result, mode="assist", canon=canon)
    assert outcome["stage"] == "repair_error"
    assert outcome["outcome"] == "unchecked", \
        "a repair that never concluded was recorded as a clean book"
    assert result["book"] == BOOK


def test_a_provider_fault_inside_the_engine_is_contained_not_an_error(wave):
    """The other half: a provider that raises is the engine's business, and the
    seam still reaches a real verdict."""
    class _Boom:
        async def __call__(self, **kw):
            raise RuntimeError("repairer exploded")

    _, canon, _claims, _r = _setup()
    wave(canon)
    _install(_Boom(), _Extractor())
    result = _real_result()
    outcome = _seam(result, mode="assist", canon=canon)
    assert outcome["stage"] == "complete"
    assert outcome["outcome"] == "unresolved"
    assert result["book"] == BOOK


def test_the_outcome_vocabulary_is_three_values_and_defaults_closed():
    import canon_lite_l2 as _l2
    assert na._l3_normalise_outcome(_l2.STATUS_CLEAN) == "clean"
    assert na._l3_normalise_outcome(_l2.STATUS_VIOLATIONS) == "unresolved"
    assert na._l3_normalise_outcome(_l2.STATUS_UNRESOLVED) == "unresolved"
    assert na._l3_normalise_outcome(_l2.STATUS_UNCHECKED) == "unchecked"
    # 🔴 An L2 status this mapping has never seen must not read as an all-clear.
    assert na._l3_normalise_outcome("some_future_status") == "unchecked"


def test_a_candidate_that_pads_the_body_is_not_written_back(wave):
    """The front-end mirror of the framing rule: a blank line inserted after the
    heading survives `startswith`, survives the reconstruction, and would land in
    the durable row as whitespace the record never had."""
    _, canon, _claims, _r = _setup()
    snap = wave(canon)
    padded = _fixed(snap).replace(b"## Bab 2\n", b"## Bab 2\n\n", 1)
    _install(_Provider(lambda b: padded), _Extractor())
    result = _real_result()
    bodies_before = [r["content"] for r in result["chapters"]]

    outcome = _seam(result, mode="assist", canon=canon)
    assert outcome["stage"] == "unsyncable"
    assert [r["content"] for r in result["chapters"]] == bodies_before
    assert result["book"] == BOOK


#: A book whose last chapter body ends in a SPACE. That makes `content.strip()`
#: differ from `content`, which switches the body-framing rule off — so this is the
#: one shape where only the BLOCK-framing rule can catch a moved wrapper.
TRAILING_WS_BOOK = ("## Bab 1\nNamanya Suranto di desa itu.\n\n"
                    "## Bab 2\nNamanya Hartono di kota itu. \n")


def test_a_body_with_trailing_space_still_rejects_a_leading_blank_line(wave):
    """🔴 THE CROSS CASE. Each framing half was green on its own; the combination
    was not tested and walked straight through.

    The old body ends in a SPACE, which is what used to switch the body guard off
    entirely. The candidate then inserts a blank line after the heading — a change
    at the OTHER end, which the trailing-run check could never see. Result: the
    repair was accepted and the durable row silently gained a leading newline.

    A guard with a precondition is a guard that is absent exactly when some input
    satisfies it. Both runs are now compared unconditionally.
    """
    _, canon, _claims, _r = _setup()
    snap = wave(canon, TRAILING_WS_BOOK)
    result = _real_result(TRAILING_WS_BOOK)
    assert result["chapters"][1]["content"].endswith(" "), \
        "the fixture lost the trailing space that makes this case distinct"

    fixed = snap.block_bytes(1).replace(BAD_NAME.encode(), GOOD_NAME.encode())
    padded = fixed.replace(b"## Bab 2\n", b"## Bab 2\n\n", 1)
    _install(_Provider(lambda b: padded), _Extractor())
    bodies_before = [r["content"] for r in result["chapters"]]

    outcome = _seam(result, mode="assist", canon=canon)
    assert outcome["stage"] == "unsyncable", (
        "a leading blank line was written into a durable row because the body "
        "already ended in whitespace")
    assert [r["content"] for r in result["chapters"]] == bodies_before
    assert result["book"] == TRAILING_WS_BOOK


def test_a_trailing_space_body_still_rejects_an_appended_newline(wave):
    """The same shape from the other end, so neither run is left unproven."""
    _, canon, _claims, _r = _setup()
    snap = wave(canon, TRAILING_WS_BOOK)
    result = _real_result(TRAILING_WS_BOOK)
    fixed = snap.block_bytes(1).replace(BAD_NAME.encode(), GOOD_NAME.encode())
    _install(_Provider(lambda b: fixed + b"\n"), _Extractor())
    bodies_before = [r["content"] for r in result["chapters"]]

    outcome = _seam(result, mode="assist", canon=canon)
    assert outcome["stage"] == "unsyncable"
    assert [r["content"] for r in result["chapters"]] == bodies_before


def test_the_cohort_that_can_actually_reach_complete(wave):
    """🔴 REFUSING SAFELY IS NOT THE SAME AS WORKING.

    The census refuses whenever the assembled book no longer contains the chapter
    bodies verbatim — which the default `polish` pass causes. That is honest, but a
    seam that only ever refuses has never been shown to deliver anything. This
    pins the cohort where assist genuinely completes: records that still match
    their blocks byte for byte.
    """
    _, canon, _claims, _r = _setup()
    snap = wave(canon)
    _install(_Provider(lambda b: _fixed(snap)), _Extractor())
    result = _real_result()
    # The precondition, stated rather than assumed.
    book = result["book"]
    assert all(rec["content"] in book for rec in result["chapters"])

    outcome = _seam(result, mode="assist", canon=canon)
    assert outcome["stage"] == "complete"
    # `outcome` reflects how much of the predicate set this canon can authorise —
    # a canon with entities but no anchors or events cannot reach `clean`, and
    # saying so is the point of the three-value vocabulary. What this control
    # pins is that the seam COMPLETED and delivered, not which of the three it
    # landed on.
    assert outcome["outcome"] in na.L3_OUTCOMES
    assert outcome["delivery_binding"] == l2.BINDING_MATCH
    assert outcome["manuscript_changed"] is True
    assert outcome["chapters_repaired"] == 1
    assert BAD_NAME not in result["book"]
    assert BAD_NAME not in "".join(r["content"] for r in result["chapters"])
