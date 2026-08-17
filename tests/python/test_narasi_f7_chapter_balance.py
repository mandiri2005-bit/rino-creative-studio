"""F7 — deterministic chapter-balance telemetry over the FINAL delivered manuscript.

🔴 F7 MEASURES; IT DOES NOT REPAIR. Every row asks what the report SAYS about bytes that are
   already final. Nothing here may assert a threshold, a verdict, or a repair: the moment this
   telemetry grows a PASS/FAIL it becomes an actuator, and an actuator on the delivery path
   needs the whole F6 apparatus that F7 deliberately does not have.

🔴 WHY THE PERSISTENCE SEAM AND NOT THE COUNTER REPORT. `_apply_v3_gates()` measures BEFORE
   `_f6_finalize()`, and F6 repair may rewrite the manuscript afterwards — a balance report
   produced there describes a book that was never delivered.
   `test_the_report_measures_the_final_markdown_not_an_earlier_snapshot` is the row that fails
   if this ever moves back upstream.

🔴 THE LANGUAGE MATRIX IS BUILT FROM PRODUCTION'S OWN LABEL MAP, NOT TYPED OUT HERE.
   An earlier version of this file checked `Chapter` and `Bab` and called them "both production
   heading labels". Production renders 19 languages, and measuring through the F6 gate's narrow
   vocabulary left SEVEN of them permanently UNMEASURED with no test able to notice. The matrix
   below is parametrised from `laozhang_api._NARASI_HEADER_LABELS` itself, so it cannot drift,
   a language added upstream is covered the day it lands, and no non-Latin label is retyped —
   a hand-copy of this list has already lost an entry to transcription once.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import math
import sys
import types
import uuid

import pytest


def _live(name):
    return sys.modules.get(name) or importlib.import_module(name)


nf7 = _live("narasi_f7")
db = _live("database")
ng = _live("narasi_gate")
chp = _live("chapter_heading_patterns")
lz = _live("laozhang_api")

#: The SOURCE OF TRUTH for what production actually emits: {"id": "Bab {n}", "ko": "{n}...", …}
PRODUCTION_LABELS = {code: labels["chapter"]
                     for code, labels in lz._NARASI_HEADER_LABELS.items()}

TENANT = str(uuid.uuid4())
JOB = "f7job001"


def book(counts, *, template="Chapter {n}", titles=None, ordinals=None):
    """An assembled book whose chapter BODIES have exactly `counts` whitespace words."""
    parts = []
    for i, n in enumerate(counts, 1):
        ordinal = (ordinals or {}).get(i, i)
        title = (titles or {}).get(i, f"Judul {i}")
        body = " ".join(f"w{j}" for j in range(n))
        parts.append(f"## {template.format(n=ordinal)}: {title}\n\n{body}")
    return "\n\n".join(parts) + "\n"


#: The recorded v9 shape. 1234/483 = 2.554865… — not already two decimals, so a mutant that
#: drops the rounding cannot hide behind a coincidence.
V9 = book([505, 1234, 483])


def measured(md, **kw):
    report = nf7.chapter_balance(md, **kw)
    assert report["status"] == "MEASURED", report
    return report


# ---------------------------------------------------------------------------
# the 19-language matrix — the P1 that a two-label test could not see
# ---------------------------------------------------------------------------
def test_production_publishes_more_languages_than_two():
    """Guards the matrix itself: if this map ever shrinks to a handful, the rows below stop
    proving anything and should fail loudly rather than quietly cover less."""
    assert len(PRODUCTION_LABELS) >= 19
    assert len({t for t in PRODUCTION_LABELS.values()}) >= 13


@pytest.mark.parametrize("code", sorted(PRODUCTION_LABELS))
def test_every_production_language_is_measured(code):
    """🔴 SEVEN LANGUAGES USED TO BE PERMANENTLY UNMEASURED — it/ko/ar/hi/th/vi/tl. Telemetry
    that silently covers two thirds of production looks healthy and is not."""
    report = nf7.chapter_balance(book([505, 1234, 483], template=PRODUCTION_LABELS[code]))
    assert report["status"] == "MEASURED", f"{code} ({PRODUCTION_LABELS[code]}) → {report}"
    assert report["word_counts"] == [505, 1234, 483]
    assert report["chapter_count"] == 3
    assert report["max_min_ratio"] == 2.55


@pytest.mark.parametrize("code", sorted(PRODUCTION_LABELS))
def test_every_production_language_reads_its_ordinals(code):
    """A grammar can match a heading and still misread which chapter it is."""
    template = PRODUCTION_LABELS[code]
    assert nf7.chapter_balance(
        book([10, 20, 30], template=template, ordinals={1: 1, 2: 3, 3: 2})
    )["reason"] == "malformed_headings"


def test_the_grammar_is_derived_from_the_canonical_vocabulary_not_forked():
    """🔴 NO THIRD HAND-COPY. `chapter_heading_patterns` documents that IT is already a
    hand-copy of the production map, and that the first such copy silently dropped `nl`. F7
    lifts that alternation verbatim instead of transcribing it a third time."""
    assert nf7.LABEL_ALTERNATION in chp.BARE_WORD_RX.pattern
    assert nf7.LABEL_ALTERNATION.count("|") >= 13


def test_a_broken_upstream_vocabulary_raises_rather_than_narrowing_silently():
    assert nf7._label_alternation() == nf7.LABEL_ALTERNATION
    import re as _re

    class _Fake:
        pattern = _re.compile(r"(?m)^something else$").pattern

    original = chp.BARE_WORD_RX
    try:
        chp.BARE_WORD_RX = _Fake
        with pytest.raises(RuntimeError):
            nf7._label_alternation()
    finally:
        chp.BARE_WORD_RX = original


def test_f7_agrees_with_the_f6_gate_wherever_the_gate_can_read_the_book():
    """🔴 ONE COUNTING RULE, TWO IMPLEMENTATIONS ONLY BECAUSE THE GATE IS FROZEN. Where the
    ratified gate CAN read a production language, F7 must return exactly what it returns —
    otherwise this is a fork, not a widening."""
    agreed = []
    for code, template in sorted(PRODUCTION_LABELS.items()):
        md = book([7, 3, 5], template=template)
        gate = ng.chapter_word_counts(md)
        if len(gate) != 3:
            continue                       # the gate cannot read this language at all
        assert nf7._word_counts(md) == gate, code
        assert nf7._word_counts(md) == [7, 3, 5], code
        agreed.append(code)
    assert len(agreed) >= 8, f"the gate should still cover the Latin subset, got {agreed}"


def test_the_f6_gate_alone_would_leave_production_languages_unmeasured():
    """The defect this widening exists to fix, pinned so it cannot silently return."""
    blind = sorted(code for code, template in PRODUCTION_LABELS.items()
                   if len(ng.chapter_word_counts(book([7, 3, 5], template=template))) != 3)
    assert blind, "if the gate covers everything, F7's own grammar is dead weight"
    for code in blind:
        assert nf7.chapter_balance(
            book([7, 3, 5], template=PRODUCTION_LABELS[code]))["status"] == "MEASURED", code


# ---------------------------------------------------------------------------
# the arithmetic, and what it is allowed to count
# ---------------------------------------------------------------------------
def test_the_v9_observation_measures_to_the_recorded_ratio():
    report = measured(V9)
    assert report["word_counts"] == [505, 1234, 483]
    assert report["chapter_count"] == 3
    assert report["min_words"] == 483
    assert report["max_words"] == 1234
    assert report["shortest_chapters"] == [3]
    assert report["longest_chapters"] == [2]
    assert report["max_min_ratio"] == 2.55
    assert report["schema_version"] == "narasi.f7.chapter_balance.v1"
    assert report["basis"] == "body_only_whitespace_words"


def test_the_ratio_is_max_over_min_rounded_to_two_decimals():
    ratio = measured(V9)["max_min_ratio"]
    assert ratio == round(1234 / 483, 2)
    assert ratio != round(483 / 1234, 2)
    assert ratio != 1234 / 483


def test_long_and_localised_headings_do_not_change_the_body_counts():
    plain = book([505, 1234, 483])
    localised = book(
        [505, 1234, 483], template=PRODUCTION_LABELS["id"],
        titles={1: "Sebuah judul yang panjang sekali dan penuh kata tambahan",
                2: "Judul lain yang juga panjang dan bertele tele sekali",
                3: "Un titre trop long pour etre honnete"})
    assert measured(plain)["word_counts"] == measured(localised)["word_counts"]


def test_the_preamble_and_the_gaya_header_are_excluded():
    """`> **Gaya:** …` is server framing the gates prepend; it is not chapter one."""
    framed = ("> **Gaya:** naratif hangat, sudut pandang orang ketiga\n"
              "> **Bahasa:** id\n\n---\n\n" + V9)
    assert measured(framed)["word_counts"] == [505, 1234, 483]
    assert measured(framed)["chapter_count"] == 3


def test_a_generic_markdown_h2_is_not_a_chapter():
    """🔴 THE HASHES ARE DECORATION IN FRONT OF A REQUIRED LABEL, never a separator of their
    own. A grammar that splits on any `## ` cuts the book at every subheading."""
    md = V9.replace("## Chapter 2: Judul 2", "## Sebuah Sub Judul Biasa")
    assert nf7.chapter_balance(md)["status"] == "UNMEASURED"
    assert measured(V9 + "\n## Catatan Penutup\n\nsatu dua tiga\n")["chapter_count"] == 3


def test_chapter_blocks_rejoin_to_the_original_byte_for_byte():
    for text in [V9, "> **Gaya:** x\n\n" + V9, "tanpa judul sama sekali", ""]:
        assert "".join(nf7._split_blocks(text)) == text


# ---------------------------------------------------------------------------
# two-phase regime selection — marked and bare are MUTUALLY EXCLUSIVE
# ---------------------------------------------------------------------------
#: The reviewer's repros, verbatim. In a marked document these lines are PROSE.
PROSE_IN_MARKED_EN = ("## Chapter 1: A\none two\n"
                      "Chapter 11 filings rose sharply this year.\nthree four\n\n"
                      "## Chapter 2: B\nfive six\n")
PROSE_IN_MARKED_ID = ("## Bab 1: A\nsatu dua\n"
                      "Bab 2 dalam hidupnya baru saja dimulai kembali.\ntiga empat\n\n"
                      "## Bab 2: B\nlima enam\n")


def test_a_marked_document_never_splits_on_bare_prose_english():
    """🔴 THE CANONICAL RULE `chapter_heading_patterns` STATES OUTRIGHT: the bare matcher must
    not run on a document that already carries `## `. Merging the two regimes into one
    optional-hash pattern reads as harmless and is not — an ordinary sentence about chapter 11
    filings becomes a chapter break, and the book is reported malformed because of its prose."""
    report = measured(PROSE_IN_MARKED_EN)
    assert report["chapter_count"] == 2
    assert report["word_counts"] == [11, 2]      # the prose line belongs to chapter 1's BODY


def test_a_marked_document_never_splits_on_bare_prose_indonesian():
    report = measured(PROSE_IN_MARKED_ID)
    assert report["chapter_count"] == 2
    assert report["word_counts"] == [12, 2]


def test_the_regime_is_chosen_by_the_canonical_discriminator():
    """Derived, not re-implemented: the same `MARKER_RX` decides, so the two can never drift."""
    assert nf7._heading_rx_for(PROSE_IN_MARKED_EN) is nf7._MARKED_HEADING_RX
    assert nf7._heading_rx_for("Chapter 1: A\n\nsatu dua\n") is nf7._BARE_HEADING_RX
    assert bool(chp.MARKER_RX.search(PROSE_IN_MARKED_EN)) is True
    assert bool(chp.MARKER_RX.search("Chapter 1: A\n\nsatu dua\n")) is False


def test_a_document_with_no_marker_still_finds_its_headings():
    """The bare regime is not decoration — a manuscript that never emits `##` must still be
    measurable, which is exactly what a selector pinned to `marked` would destroy."""
    bare = "Chapter 1: A\n\nsatu dua\n\nChapter 2: B\n\ntiga empat\n"
    assert measured(bare)["word_counts"] == [2, 2]
    assert measured(bare)["chapter_count"] == 2


def test_the_bare_regime_stays_case_sensitive():
    """🔴 WITHOUT THE MARKER, CASE IS THE LAST SIGNAL LEFT. A case-blind bare matcher turns any
    sentence opening with "chapter" into a chapter break."""
    bare = ("Chapter 1: A\n\nsatu dua\n"
            "chapter 2 was a strange year indeed\n\nChapter 2: B\n\ntiga\n")
    report = measured(bare)
    assert report["chapter_count"] == 2, "a lowercase sentence was read as a heading"
    assert nf7._BARE_HEADING_RX.match("chapter 1: a") is None
    assert nf7._MARKED_HEADING_RX.match("## chapter 1: a") is not None


# ---------------------------------------------------------------------------
# ordinal validation — a digit is not an ordinal until it ends like one
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("heading", ["Chapter 1a: Appendix", "Chapter 1.5: Half",
                                     "Chapter 1b", "Chapter 2,5: Setengah"])
def test_a_malformed_ordinal_is_not_a_chapter_number(heading):
    """🔴 READING `Chapter 1a` AS CHAPTER ONE lets an appendix occupy a real chapter's index,
    and every per-chapter number after it then describes the wrong chapter — silently."""
    assert nf7.chapter_balance(f"## {heading}\n\nsatu dua tiga\n")["reason"] == "malformed_headings"


def test_a_two_digit_chapter_number_is_read_whole():
    """The digit run must be MAXIMAL, or `Chapter 12` reads as chapter one plus stray text."""
    twelve = "\n\n".join(f"## Chapter {i}: T\n\n" + " ".join(["w"] * i) for i in range(1, 13))
    report = measured(twelve + "\n")
    assert report["chapter_count"] == 12
    assert report["word_counts"] == list(range(1, 13))
    assert nf7._heading_ordinal("## Chapter 12: T") == 12


def test_the_cjk_shapes_keep_their_own_suffix_after_the_digits():
    """The rejected tails are ASCII-specific on purpose: a rule phrased as "no letter may
    follow the digits" would reject the very languages this grammar exists to cover."""
    for code in ("ja", "zh", "ko"):
        template = PRODUCTION_LABELS[code]
        assert nf7._heading_ordinal(f"## {template.format(n=3)}: J") == 3


# ---------------------------------------------------------------------------
# expected chapter count — the second P1
# ---------------------------------------------------------------------------
def test_a_book_missing_a_chapter_is_refused_against_the_servers_own_count():
    """🔴 A TRUNCATED BOOK IS INTERNALLY CONSISTENT. Two chapters of a three-chapter job have
    headings 1..2 and a perfectly real ratio, so nothing in the manuscript reveals the loss.
    Only the server's own count does — and reporting 1.5 over a book that lost a third of
    itself is telemetry that actively misleads."""
    payload = {"markdown": book([300, 200]), "n_total": 3, "chapters": 3}
    report = nf7.attach_chapter_balance(payload)["chapter_balance"]
    assert report["status"] == "UNMEASURED"
    assert report["reason"] == "chapter_count_mismatch"
    assert "max_min_ratio" not in report


@pytest.mark.parametrize("key", ["n_total", "chapters"])
def test_either_server_owned_count_field_alone_is_enough_to_refuse(key):
    """`n_total` is the Dalang payload builder's field, `chapters` the classic/persist path's.
    A rule that reads only one of them is unproven on the other's jobs."""
    report = nf7.attach_chapter_balance(
        {"markdown": book([300, 200]), key: 3})["chapter_balance"]
    assert report["reason"] == "chapter_count_mismatch"


def test_two_server_owned_counts_that_disagree_are_themselves_a_refusal():
    report = nf7.attach_chapter_balance(
        {"markdown": book([300, 200]), "n_total": 2, "chapters": 3})["chapter_balance"]
    assert report["reason"] == "chapter_count_mismatch"


def test_a_count_that_matches_measures_normally():
    report = nf7.attach_chapter_balance(
        {"markdown": V9, "n_total": 3, "chapters": 3})["chapter_balance"]
    assert report["status"] == "MEASURED"
    assert report["max_min_ratio"] == 2.55


@pytest.mark.parametrize("value", [True, False, 0, -1, "3", 3.0, None, [3]])
def test_an_unusable_count_field_is_ignored_rather_than_obeyed(value):
    """🔴 `True` IS AN `int` IN PYTHON and would otherwise read as "one chapter", refusing every
    multi-chapter book on a payload that never stated a count at all."""
    report = nf7.attach_chapter_balance(
        {"markdown": V9, "n_total": value})["chapter_balance"]
    assert report["status"] == "MEASURED", f"{value!r} was obeyed as a count"


def test_a_payload_with_no_count_field_at_all_still_measures():
    assert nf7.attach_chapter_balance({"markdown": V9})["chapter_balance"]["status"] == "MEASURED"


def test_expected_counts_are_read_only_from_server_owned_keys():
    assert nf7.expected_chapter_counts({"n_total": 3}) == frozenset({3})
    assert nf7.expected_chapter_counts({"chapters": 4}) == frozenset({4})
    assert nf7.expected_chapter_counts({"n_total": 3, "chapters": 3}) == frozenset({3})
    assert nf7.expected_chapter_counts({"n_total": 2, "chapters": 3}) == frozenset({2, 3})
    assert nf7.expected_chapter_counts({"n_ok": 3, "bab1_words": 500}) == frozenset()
    assert nf7.expected_chapter_counts("not a dict") == frozenset()


# ---------------------------------------------------------------------------
# everything that must refuse to measure, and refuse in a BOUNDED way
# ---------------------------------------------------------------------------
def _unmeasured(md, reason, **kw):
    report = nf7.chapter_balance(md, **kw)
    assert report["status"] == "UNMEASURED", report
    assert report["reason"] == reason, report
    assert report["schema_version"] == "narasi.f7.chapter_balance.v1"
    assert report["basis"] == "body_only_whitespace_words"
    assert set(report) == {"schema_version", "status", "basis", "reason"}
    return report


@pytest.mark.parametrize("md", [None, "", "   \n\n  ", 12345, {"markdown": "x"}])
def test_absent_or_non_text_markdown_is_unmeasured(md):
    _unmeasured(md, "no_markdown")


def test_prose_with_no_chapter_heading_at_all_is_unmeasured():
    _unmeasured("Ini paragraf biasa tanpa judul bab sama sekali.\n", "no_chapters")


@pytest.mark.parametrize("ordinals,case", [
    ({1: 1, 2: 1, 3: 3}, "duplicate"),
    ({1: 1, 2: 3, 3: 2}, "reordered"),
    ({1: 1, 2: 2, 3: 4}, "gapped"),
    ({1: 2, 2: 3, 3: 4}, "does not start at one"),
])
def test_a_broken_heading_sequence_is_unmeasured(ordinals, case):
    _unmeasured(book([10, 20, 30], ordinals=ordinals), "malformed_headings")


def test_a_zero_word_chapter_cannot_produce_infinity_nan_or_a_ratio():
    report = _unmeasured(book([120, 0, 90]), "zero_word_chapter")
    assert "max_min_ratio" not in report
    for value in report.values():
        assert not (isinstance(value, float)
                    and (math.isinf(value) or math.isnan(value)))


def test_more_than_two_hundred_chapters_is_bounded_and_unmeasured():
    assert measured(book([5] * 200))["chapter_count"] == 200
    _unmeasured(book([5] * 201), "too_many_chapters")


def test_the_refusal_reason_comes_from_a_closed_vocabulary():
    cases = [None, "", "no headings here", book([1, 0]), book([5] * 201),
             book([1, 2], ordinals={1: 2, 2: 1})]
    for md in cases:
        report = nf7.chapter_balance(md)
        if report["status"] == "UNMEASURED":
            assert report["reason"] in nf7.UNMEASURED_REASONS
    mismatch = nf7.chapter_balance(V9, expected_chapters=frozenset({9}))
    assert mismatch["reason"] in nf7.UNMEASURED_REASONS


def test_an_internal_failure_is_reported_as_a_bounded_reason_not_an_exception(monkeypatch):
    def boom(_text):
        raise RuntimeError("secret manuscript text that must never be echoed")

    monkeypatch.setattr(nf7, "_word_counts", boom)
    report = _unmeasured(V9, "internal_error")
    assert "secret manuscript" not in json.dumps(report)


# ---------------------------------------------------------------------------
# ties, and what the report is allowed to contain
# ---------------------------------------------------------------------------
def test_ties_keep_every_chapter_index_one_based():
    report = measured(book([100, 300, 100, 300, 200]))
    assert report["shortest_chapters"] == [1, 3]
    assert report["longest_chapters"] == [2, 4]
    assert report["min_words"] == 100 and report["max_words"] == 300


def test_a_single_chapter_book_is_its_own_shortest_and_longest():
    report = measured(book([42]))
    assert report["shortest_chapters"] == [1] and report["longest_chapters"] == [1]
    assert report["max_min_ratio"] == 1.0


def test_the_report_carries_no_prose_titles_or_hashes_and_is_json_serialisable():
    secret_title = "RahasiaJudulYangTidakBolehBocor"
    md = book([505, 1234, 483], titles={1: secret_title, 2: secret_title, 3: secret_title})
    md = md.replace("w0", "KalimatRahasiaDalamTubuhBab")
    report = measured(md)
    blob = json.dumps(report)
    assert secret_title not in blob
    assert "KalimatRahasiaDalamTubuhBab" not in blob
    assert "Chapter" not in blob and "##" not in blob
    assert set(report) == {"schema_version", "status", "basis", "chapter_count",
                           "word_counts", "min_words", "max_words",
                           "shortest_chapters", "longest_chapters", "max_min_ratio"}


def test_the_report_emits_no_verdict_threshold_or_go_no_go():
    blob = json.dumps(measured(V9)).lower()
    for forbidden in ("pass", "fail", "balanced", "unbalanced", "no_go",
                      "threshold", "violation", "blocked"):
        assert forbidden not in blob


# ---------------------------------------------------------------------------
# the attachment helper — the caller's dict is INPUT, never scratch space
# ---------------------------------------------------------------------------
def test_the_callers_payload_is_not_mutated():
    payload = {"markdown": V9, "chapters": 3}
    before = json.dumps(payload, sort_keys=True)
    out = nf7.attach_chapter_balance(payload)
    assert json.dumps(payload, sort_keys=True) == before
    assert "chapter_balance" not in payload
    assert out is not payload
    assert out["chapter_balance"]["status"] == "MEASURED"
    assert out["chapters"] == 3


def test_a_forged_or_stale_chapter_balance_is_overwritten():
    forged = {"schema_version": "narasi.f7.chapter_balance.v1", "status": "MEASURED",
              "basis": "body_only_whitespace_words", "chapter_count": 1,
              "word_counts": [1], "min_words": 1, "max_words": 1,
              "shortest_chapters": [1], "longest_chapters": [1], "max_min_ratio": 1.0}
    out = nf7.attach_chapter_balance({"markdown": V9, "chapter_balance": forged})
    assert out["chapter_balance"]["word_counts"] == [505, 1234, 483]
    assert out["chapter_balance"]["max_min_ratio"] == 2.55


def test_a_payload_without_markdown_still_gets_a_bounded_report():
    out = nf7.attach_chapter_balance({"chapters": 0})
    assert out["chapter_balance"]["status"] == "UNMEASURED"
    assert out["chapter_balance"]["reason"] == "no_markdown"


# ---------------------------------------------------------------------------
# the persistence seam
# ---------------------------------------------------------------------------
class _Exec:
    """Stand-in for `database._q_exec` that records the payload actually persisted."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def __call__(self, sql, *args, **kwargs):
        self.calls.append({"sql": sql, "args": args, "kwargs": kwargs})
        if self.fail:
            raise RuntimeError("db down")
        return "UPDATE 1"

    @property
    def status(self):
        return self.calls[-1]["args"][2]

    @property
    def payload(self):
        return self.calls[-1]["args"][3]


def _finish(monkeypatch, status, **kw):
    spy = _Exec()
    monkeypatch.setattr(db, "_q_exec", spy)
    asyncio.run(db.finish_narasi_job(TENANT, JOB, status, **kw))
    return spy


def test_a_done_job_persists_the_chapter_balance(monkeypatch):
    spy = _finish(monkeypatch, "done", result={"markdown": V9, "chapters": 3})
    assert spy.status == "done"
    assert spy.payload["chapter_balance"]["word_counts"] == [505, 1234, 483]
    assert spy.payload["chapter_balance"]["max_min_ratio"] == 2.55
    assert spy.payload["chapters"] == 3
    assert spy.payload["markdown"] == V9


@pytest.mark.parametrize("status", ["error", "cancelled"])
def test_failed_and_cancelled_jobs_keep_their_previous_payload_behaviour(monkeypatch, status):
    spy = _finish(monkeypatch, status, error="Dibatalkan oleh user")
    assert spy.status == status
    assert spy.payload is None
    assert spy.calls[-1]["args"][4] == "Dibatalkan oleh user"


@pytest.mark.parametrize("status", ["error", "cancelled"])
def test_a_failed_job_carrying_a_payload_is_still_not_measured(monkeypatch, status):
    spy = _finish(monkeypatch, status, result={"markdown": V9}, error="gagal")
    assert spy.status == status
    assert "chapter_balance" not in spy.payload
    assert spy.payload["markdown"] == V9


def test_a_done_job_with_a_non_dict_result_is_left_exactly_as_it_was(monkeypatch):
    spy = _finish(monkeypatch, "done", result=None)
    assert spy.payload is None


def test_an_outline_payload_is_persisted_byte_identical(monkeypatch):
    """🔴 `finish_narasi_job` IS SHARED. The async outline path finishes `done` with a dict
    that has no manuscript in it, so a guard keyed only on `done` stamped
    `chapter_balance: UNMEASURED/no_markdown` onto payloads that have nothing to do with
    chapters. The condition is "this job delivered a book", not "this job ended well"."""
    outline = {"ok": True, "outline": [{"id": 1, "title": "A"}], "chapters": 3,
               "usage": {"tokens": 12}}
    before = json.dumps(outline, sort_keys=True)
    spy = _finish(monkeypatch, "done", result=outline)
    assert "chapter_balance" not in spy.payload
    assert json.dumps(spy.payload, sort_keys=True) == before
    assert spy.payload is outline, "the untouched payload must be the very same object"


@pytest.mark.parametrize("md", ["", "   \n  ", None, 12345, [1, 2]])
def test_a_done_payload_without_a_real_manuscript_is_left_byte_identical(monkeypatch, md):
    payload = {"markdown": md, "chapters": 3}
    before = json.dumps(payload, sort_keys=True, default=str)
    spy = _finish(monkeypatch, "done", result=payload)
    assert "chapter_balance" not in spy.payload
    assert json.dumps(spy.payload, sort_keys=True, default=str) == before


def test_the_report_measures_the_final_markdown_not_an_earlier_snapshot(monkeypatch):
    """🔴 THE FINAL-BYTE WITNESS, AND THE REASON THIS LIVES IN `finish_narasi_job`.

    The payload arrives carrying a `chapter_balance` measured from an EARLIER manuscript —
    exactly what an upstream seam would have produced. The markdown is then replaced with the
    bytes actually being delivered. The stored report must describe the SECOND book."""
    stale_md = book([10, 10, 10])
    payload = {"markdown": stale_md}
    payload["chapter_balance"] = nf7.chapter_balance(stale_md)
    assert payload["chapter_balance"]["word_counts"] == [10, 10, 10]

    payload["markdown"] = V9                      # F6 repair lands; these are the final bytes
    spy = _finish(monkeypatch, "done", result=payload)

    stored = spy.payload["chapter_balance"]
    assert stored["word_counts"] == [505, 1234, 483], "measured a stale manuscript"
    assert stored["max_min_ratio"] == 2.55


def test_the_seam_refuses_a_truncated_book_end_to_end(monkeypatch):
    """The reviewer's repro, driven through the real persistence path."""
    spy = _finish(monkeypatch, "done",
                  result={"markdown": book([300, 200]), "n_total": 3, "chapters": 3})
    assert spy.payload["chapter_balance"]["status"] == "UNMEASURED"
    assert spy.payload["chapter_balance"]["reason"] == "chapter_count_mismatch"


def test_persistence_still_proceeds_when_the_measurement_is_unavailable(monkeypatch, caplog):
    def boom(_payload):
        raise RuntimeError("manuscript bytes that must not reach the log")

    monkeypatch.setattr(nf7, "attach_chapter_balance", boom)
    spy = _Exec()
    monkeypatch.setattr(db, "_q_exec", spy)
    with caplog.at_level("ERROR"):
        asyncio.run(db.finish_narasi_job(TENANT, JOB, "done", result={"markdown": V9}))

    assert spy.status == "done"
    assert spy.payload["markdown"] == V9
    assert "chapter_balance" not in spy.payload
    assert "manuscript bytes that must not reach the log" not in caplog.text
    assert "f7_chapter_balance_unavailable" in caplog.text


def test_a_database_failure_still_raises_exactly_as_before(monkeypatch):
    spy = _Exec(fail=True)
    monkeypatch.setattr(db, "_q_exec", spy)
    with pytest.raises(RuntimeError):
        asyncio.run(db.finish_narasi_job(TENANT, JOB, "done", result={"markdown": V9}))


def test_the_persisted_payload_is_a_dict_the_jsonb_codec_can_encode(monkeypatch):
    """🔴 NEVER `json.dumps` BEFORE A JSONB PARAM — the pool's codec encodes once."""
    spy = _finish(monkeypatch, "done", result={"markdown": V9})
    assert isinstance(spy.payload, dict)
    json.dumps(spy.payload)


# ---------------------------------------------------------------------------
# cost: this is local arithmetic and nothing else
# ---------------------------------------------------------------------------
def test_f7_reaches_no_provider_client_or_network(monkeypatch):
    imported = {name for name, value in vars(nf7).items()
                if isinstance(value, types.ModuleType)}
    allowed = {"re", "_chp", "chapter_heading_patterns"}
    assert imported <= allowed, f"F7 imported something it does not need: {imported - allowed}"

    with open(nf7.__file__, encoding="utf-8") as fh:
        source = fh.read()
    for forbidden in ("requests", "httpx", "openai", "OpenAI", "anthropic", "genai",
                      "socket", "urllib", "aiohttp", "boto3", "asyncio", "await "):
        assert forbidden not in source, f"F7 must not reference {forbidden}"

    from conftest import BLOCKED_ATTEMPTS
    before = len(BLOCKED_ATTEMPTS)
    _finish(monkeypatch, "done", result={"markdown": V9})
    assert len(BLOCKED_ATTEMPTS) == before


def test_measurement_is_pure_and_repeatable():
    """No feature flag, no state, no clock: the same bytes measure the same way forever."""
    assert nf7.chapter_balance(V9) == nf7.chapter_balance(V9) == measured(V9)
