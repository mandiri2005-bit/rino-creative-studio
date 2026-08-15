"""`chapter_heading_patterns` — the shared chapter-heading detector behind
`canon_lite_l2.materialize_final_snapshot` and `orchestrator.static._dedup_chapter_blocks` /
`_split_into_chunks`.

Regression coverage for the 2026-08-15 adversarial code review of the first version of this
fix (a single always-broadened regex, hand-duplicated across canon_lite_l2.py and
orchestrator/static.py, `(?i)` + `\\b` both present, no two-phase selection). Every finding
that review confirmed via real-API execution gets a test here that would have failed against
that first version and passes against this one — see git history / the review report for the
`file:line` this superseded.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import chapter_heading_patterns as chp  # noqa: E402


# ===========================================================================
# Two-phase selection — the core structural fix
# ===========================================================================

def test_a_book_with_any_marker_anywhere_selects_marker_rx_only():
    book = "Kata pengantar\n\n## Bab 1: Awal\nisi satu\n\n## Bab 2: Lanjut\nisi dua\n"
    assert chp.chapter_split_rx_for(book) is chp.MARKER_RX


def test_a_book_with_zero_markers_selects_the_bare_word_fallback():
    book = "Chapter 1: Opening\nisi satu\n\nChapter 2: Middle\nisi dua\n"
    assert chp.chapter_split_rx_for(book) is chp.BARE_WORD_RX


def test_bare_word_false_positive_inside_an_already_marked_book_is_now_inert():
    """The bug the first version of this fix shipped: a bare-word-shaped line INSIDE a
    chapter's body prose, in a book that already has real "## " headings, used to spuriously
    split — corrupting positional chapter_id binding, leaking deduped content, and splitting
    a chapter's polish pass across two chunks (all reproduced against the real production
    functions during review). Two-phase selection makes this structurally unreachable: once
    "## " is found anywhere, BARE_WORD_RX never runs on this book at all."""
    book = ("## Chapter 1: Real\nMaya found the old journal.\n"
            "Bab 2 dalam hidupnya baru saja dimulai kembali, entah kenapa.\n\n"
            "## Chapter 2: Real\nMore text here.\n")
    rx = chp.chapter_split_rx_for(book)
    parts = [p for p in rx.split(book) if p.strip()]
    assert rx is chp.MARKER_RX
    assert len(parts) == 2


# ===========================================================================
# MARKER_RX — must stay byte-identical to the pre-fix behavior
# ===========================================================================

_OLD_STRICT_RX = re.compile(r"(?m)(?=^## )")


@pytest.mark.parametrize("text", [
    "Kata pengantar\n\n## Bab 1: Awal\nisi satu\n\n## Bab 2: Lanjut\nisi dua\n",
    "## Bab 1\nisi\n## Bab 2\nisi dua",
    "序\n\n## 第 1 章：開始\n本文がここにあります\n\n## 第 2 章\n続き\n",
    "## Bab 3\nx\n## Bab 3a\ny\n## Bab 3.5\nz\n",
])
def test_marker_rx_is_byte_identical_to_the_old_pattern(text):
    assert chp.MARKER_RX.pattern == _OLD_STRICT_RX.pattern
    assert chp.MARKER_RX.split(text) == _OLD_STRICT_RX.split(text)


# ===========================================================================
# BARE_WORD_RX — every review finding gets its own regression test
# ===========================================================================

_HEADER_TEMPLATES = {
    "id": "Bab {n}", "en": "Chapter {n}", "es": "Capítulo {n}", "fr": "Chapitre {n}",
    "de": "Kapitel {n}", "pt": "Capítulo {n}", "nl": "Hoofdstuk {n}", "it": "Capitolo {n}",
    "ja": "第{n}章", "ko": "{n}장", "zh": "第{n}章", "ar": "الفصل {n}", "hi": "अध्याय {n}",
    "th": "บทที่ {n}", "vi": "Chương {n}", "ms": "Bab {n}", "jv": "Bab {n}", "su": "Bab {n}",
    "tl": "Kabanata {n}",
}  # copied from laozhang_api._NARASI_HEADER_LABELS's "chapter" field, all 19 entries —
   # re-diff this dict against the real one whenever either changes (see module docstring:
   # the first hand-copy already dropped "nl" once, caught only by review, not by a test).


@pytest.mark.parametrize("lang,template", sorted(_HEADER_TEMPLATES.items()))
def test_every_supported_language_bare_heading_splits_into_two_chapters(lang, template):
    label1, label2 = template.format(n=1), template.format(n=2)
    book = f"{label1}: Title\nbody one\n\n{label2}: Title\nbody two\n"
    parts = [p for p in chp.BARE_WORD_RX.split(book) if p.strip()]
    assert len(parts) == 2, f"{lang} ({template!r}) did not split: {parts!r}"


def test_dutch_hoofdstuk_specifically_was_the_omission_review_caught():
    """Review finding: the first hand-copy of the 18/19-language word list silently dropped
    'nl' (Hoofdstuk) — a plain transcription miss the docstring's own "18 languages" undercount
    should have been a hint of. A Dutch bare-heading manuscript reproduced the exact zero-
    chapter collapse this whole module exists to prevent."""
    book = "Hoofdstuk 1: Het begin\neen\n\nHoofdstuk 2: Het midden\ntwee\n"
    parts = [p for p in chp.BARE_WORD_RX.split(book) if p.strip()]
    assert len(parts) == 2


def test_glued_digit_with_no_separating_space_still_splits():
    """Review finding: `\\b` can never hold between a letter and an immediately-adjacent
    digit (both are \\w), so a word\\b[ \\t]*\\d shape only matched when real whitespace
    separated the word from the digit — "Chapter1: Opening" (no space) silently collapsed to
    0 chapters, reproducing this module's own target bug for a plausible drift variant.
    Fixed by dropping the now-redundant \\b (the mandatory digit-immediately-after constraint
    already blocks false prefix matches on its own — see module docstring)."""
    book = "Chapter1: Awal\nisi\n\nChapter2: Akhir\nisi dua\n"
    parts = [p for p in chp.BARE_WORD_RX.split(book) if p.strip()]
    assert len(parts) == 2


def test_bare_word_alternatives_are_case_sensitive():
    """Review finding: the first version's `(?i)` flag let ordinary lowercase prose
    ("chapter 11 bankruptcy proceedings...") collide as a phantom chapter boundary, for zero
    legitimate gain — real headings from `_chapter_label` are always Title Case."""
    lowercase_prose = "chapter 11 bankruptcy proceedings began that Monday morning.\n"
    assert chp.BARE_WORD_RX.search(lowercase_prose) is None
    # the correctly-cased form must still match, proving this isn't just a broken pattern
    assert chp.BARE_WORD_RX.search("Chapter 11: Bankruptcy\n") is not None


def test_bare_word_digit_class_is_ascii_only_not_unicode_digit_aware():
    """Review finding: bare `\\d` matches Arabic-Indic/Devanagari/Thai/fullwidth digits too,
    not just ASCII — while the real generator only ever emits plain int-formatted ASCII
    digits. Arabic prose merely REFERENCING "chapter 5" with a native Arabic-Indic digit
    (not a heading at all) used to inject a phantom chapter boundary."""
    arabic_prose_with_native_digit = "الفصل ٥ من هذا الكتاب يشرح ذلك بالتفصيل.\n"
    assert chp.BARE_WORD_RX.search(arabic_prose_with_native_digit) is None
    # ASCII-digit form must still match
    assert chp.BARE_WORD_RX.search("الفصل 5: العنوان\n") is not None


def test_cjk_accepts_the_native_fullwidth_ideographic_space():
    """Review finding: `[ \\t]*` between 第/N/章 was ASCII-only and rejected the idiomatic
    native Japanese/Chinese full-width space U+3000 — "第　1　章" (natural CJK typesetting)
    silently collapsed to 0 chapters."""
    book = "第　1　章\n一\n\n第　2　章\n二\n"
    parts = [p for p in chp.BARE_WORD_RX.split(book) if p.strip()]
    assert len(parts) == 2


def test_prose_that_opens_a_line_with_a_bare_chapter_word_and_no_digit_does_not_split():
    """The false-positive guard this module exists to provide in the first place — a bare
    chapter-word with nothing number-shaped after it must never be treated as a heading."""
    prose = "Dia bilang begitu.\nBab pertama dalam hidupnya baru saja dimulai.\n"
    assert chp.BARE_WORD_RX.search(prose) is None


def test_known_deferred_gap_nfd_normalized_vietnamese_does_not_match():
    """Documented, deliberately deferred (see module docstring): the source literal "Chương"
    is NFC; NFD-normalized input (decomposed combining marks) does not contain those exact
    codepoints, so the match silently fails. No evidence this pipeline's own paths introduce
    NFD; normalizing the manuscript text itself would break materialize_final_snapshot's
    byte-exact round-trip contract, so this needs a real NFC/NFD-tolerant pattern to close
    properly, not a rushed fix. This test pins the CURRENT (gap-having) behavior so a future
    fix has a red test to turn green, and a regression would be caught if NFC support broke."""
    import unicodedata
    nfc = "Chương 1: Mở đầu\n"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd  # sanity: the two forms really are different byte sequences
    assert chp.BARE_WORD_RX.search(nfc) is not None
    assert chp.BARE_WORD_RX.search(nfd) is None  # the documented gap


def test_a_pure_bare_word_book_can_still_false_split_on_an_incidental_line_documented_residual_risk():
    """NOT a bug this test guards against — it pins the accepted residual risk. Two-phase
    selection eliminates the false-positive-inside-a-##-marked-book class entirely (see
    test_bare_word_false_positive_inside_an_already_marked_book_is_now_inert above), but a
    book with ZERO "## " markers anywhere still uses BARE_WORD_RX for its whole body, so an
    incidental in-body line shaped like a heading can still create a spurious extra block.
    This is the accepted trade against the alternative (zero chapters detected at all) — see
    module docstring "Why chapter_split_rx_for picks ONE regime"."""
    book = ("Chapter 1: Opening\nisi satu.\n"
            "Chapter 11 filings rose sharply that year.\n\n"
            "Chapter 2: Ending\nisi dua.\n")
    rx = chp.chapter_split_rx_for(book)
    parts = [p for p in rx.split(book) if p.strip()]
    assert rx is chp.BARE_WORD_RX
    assert len(parts) == 3  # 2 real chapters + 1 incidental false split, by design trade-off
