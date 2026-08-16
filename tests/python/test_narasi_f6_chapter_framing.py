"""F6 root cause — the chapter splitter does not recognise the shape production emits.

🔴 THE DEFECT, STATED AS THE PRODUCTION FORMAT. `orchestrator/static.py::_chapter_md` builds
   every chapter as `## <label>[: <title>]` followed by a blank line and the prose, and joins
   the chapters with `\\n\\n`. That is what every downstream detector actually receives.

   `narasi_gate._CHAPTER_SPLIT_RX` matches `^\\s*(?:Chapter|Bab|…)`. `#` is not whitespace, so
   `## Chapter 2` never matches, `re.split` finds no boundary, and a whole book collapses into
   ONE chapter. Four scanners then measure a single blob: entity consistency, POV/persona drift,
   aphorism density and narrator-opening ratio. Per-chapter reasoning — which is all F6 is —
   cannot exist on top of that.

   The identical class was already found and fixed once in `narasi_counters` (audit r16: "a bare
   'Chapter N' split never matched, making this scanner dead code on every real chaptered
   manuscript"), with `#{0,3}\\s*` as the accepted remedy. The gate splitter never got it.

🔴 WHAT THE FIX MUST NOT DO. `narasi_entities` and `narasi_proper_noun` split on `^##\\s+` — ANY
   markdown h2, which is a GENERIC separator: a `## Epilogue` or a stray heading inside prose
   becomes a chapter. The gate splitter must stay keyword-anchored so it recognises chapter
   headings specifically, in every language it already supports, and nothing else.
"""
from __future__ import annotations

import narasi_gate as ng


CH_TITLES = ("The Return", "The Reveal", "The Board")
BODIES = (
    "Eun-soo walked the hanok corridor and counted the doors she had closed.",
    "The last light of the city paints the window while Min-jae sits and waits.",
    "Tae-jun signed the deposition and the room finally emptied of its noise.",
)


def production_book() -> str:
    """Byte-for-byte the shape `_chapter_md` emits: `## Chapter N: Title`, blank line, prose,
    chapters joined by a blank line."""
    return "\n\n".join(
        f"## Chapter {i}: {title}\n\n{body}"
        for i, (title, body) in enumerate(zip(CH_TITLES, BODIES), 1))


# ---------------------------------------------------------------------------
# The splitter itself
# ---------------------------------------------------------------------------
def test_the_gate_splitter_finds_every_chapter_in_the_production_shape():
    """🔴 RED BEFORE THE FIX: this returns 1. The book is three chapters."""
    parts = ng._CHAPTER_SPLIT_RX.split(production_book())
    chapters = parts[1:] if len(parts) > 1 else parts
    assert len(chapters) == 3, (
        f"the production heading shape yields {len(chapters)} chapter(s) — a book that is "
        f"one blob cannot be reasoned about per chapter")


def test_the_splitter_still_reads_a_bare_heading_without_markdown():
    """A plain-text export has no `#`. Both shapes must work; the fix widens, never moves."""
    bare = "\n\n".join(f"Chapter {i}: {t}\n\n{b}"
                       for i, (t, b) in enumerate(zip(CH_TITLES, BODIES), 1))
    parts = ng._CHAPTER_SPLIT_RX.split(bare)
    assert len(parts[1:]) == 3


def test_the_splitter_keeps_every_language_it_already_supported():
    """The multilingual keyword set is existing behaviour and must not regress."""
    for keyword in ("Hoofdstuk", "Chapter", "Bab", "Capítulo", "Kapitel", "Chapitre"):
        book = "\n\n".join(f"## {keyword} {i}\n\nprose {i}" for i in (1, 2, 3))
        parts = ng._CHAPTER_SPLIT_RX.split(book)
        assert len(parts[1:]) == 3, keyword


def test_the_splitter_does_not_become_a_generic_markdown_separator():
    """🔴 THE FAILURE THE WIDENING MUST NOT INTRODUCE. `narasi_entities` splits on any `##`;
    doing that here would turn a section heading inside a chapter into a chapter boundary and
    silently inflate every per-chapter count."""
    book = ("## Chapter 1: The Return\n\nprose one\n\n"
            "## A Note On Sources\n\nstill chapter one\n\n"
            "## Chapter 2: The Reveal\n\nprose two")
    parts = ng._CHAPTER_SPLIT_RX.split(book)
    assert len(parts[1:]) == 2, "a non-chapter h2 was treated as a chapter boundary"


# ---------------------------------------------------------------------------
# ...and the scanners that sit on it, through their own entry points
# ---------------------------------------------------------------------------
def test_pov_drift_scan_counts_chapters_not_one_blob():
    """🔴 RED BEFORE THE FIX: `chapters` is 1. POV/tense drift is per-chapter by definition —
    this scanner is the one F6's tense work has to stand on."""
    out = ng.pov_persona_drift_scan(production_book())
    assert out["chapters"] == 3


def test_narrator_opening_ratio_scan_measures_three_chapters():
    """🔴 RED BEFORE THE FIX: total is 1, so the ratio is computed over one blob."""
    _ratio, _matched, total = ng.narrator_opening_ratio_scan(production_book(), lang="en")
    assert total == 3


# ---------------------------------------------------------------------------
# The byte-preserving splitter F6 repair and verification stand on
# ---------------------------------------------------------------------------
def test_chapter_blocks_rejoin_to_the_original_byte_for_byte():
    """🔴 THE PROPERTY THE WHOLE F6 LOOP DEPENDS ON. Repair hands ONE chapter to a provider and
    must then prove the others — and every heading and server-owned separator — came back
    untouched. That proof is only possible if splitting loses nothing."""
    book = production_book()
    blocks = ng.split_chapter_blocks(book)
    assert len(blocks) == 3
    assert "".join(blocks) == book


def test_each_block_begins_at_its_own_heading_and_keeps_it_verbatim():
    blocks = ng.split_chapter_blocks(production_book())
    for i, (block, title) in enumerate(zip(blocks, CH_TITLES), 1):
        assert block.startswith(f"## Chapter {i}: {title}")
        assert ng.chapter_heading_line(block) == f"## Chapter {i}: {title}"


def test_the_separator_between_chapters_stays_with_the_earlier_block():
    """A block must not begin on the blank line before its heading — otherwise the separator
    migrates between chapters and a byte comparison of an untouched chapter fails for a reason
    that has nothing to do with the repair."""
    blocks = ng.split_chapter_blocks(production_book())
    assert blocks[0].endswith("\n\n")
    assert blocks[1].startswith("## Chapter 2")


def test_a_preamble_before_the_first_heading_is_kept_not_discarded():
    book = "Gaya: storytelling\n\n" + production_book()
    blocks = ng.split_chapter_blocks(book)
    assert "".join(blocks) == book
    assert blocks[0].startswith("Gaya: storytelling")
    assert len(blocks) == 4


def test_text_with_no_recognisable_heading_is_one_block():
    assert ng.split_chapter_blocks("just prose, no headings") == ["just prose, no headings"]
    assert ng.split_chapter_blocks("") == []


def test_block_splitting_uses_the_same_chapter_grammar_as_the_scanner():
    """🔴 ONE VOCABULARY, OR FINDINGS AND REPAIRS ADDRESS DIFFERENT CHAPTERS. The scanner
    decides which chapter a violation belongs to; the block splitter decides which bytes get
    handed to the repair. If the two disagree about what a chapter heading is — in ANY
    supported language — F6 targets chapter 2 and edits chapter 1.

    Checked across the whole keyword set, not just English: a block splitter narrowed to
    `Chapter` agrees perfectly on an English book and silently diverges on every other."""
    non_chapter_h2 = ("## Chapter 1: The Return\n\nprose one\n\n"
                      "## A Note On Sources\n\nstill chapter one\n\n"
                      "## Chapter 2: The Reveal\n\nprose two")
    assert len(ng._CHAPTER_SPLIT_RX.split(non_chapter_h2)[1:]) == 2
    assert len(ng.split_chapter_blocks(non_chapter_h2)) == 2

    for keyword in ("Hoofdstuk", "Chapter", "Bab", "Capítulo", "Kapitel", "Chapitre"):
        book = "\n\n".join(f"## {keyword} {i}\n\nprose {i}" for i in (1, 2, 3))
        scanned = len(ng._CHAPTER_SPLIT_RX.split(book)[1:])
        blocked = len(ng.split_chapter_blocks(book))
        assert scanned == blocked == 3, (
            f"{keyword}: scanner sees {scanned} chapter(s), block splitter sees {blocked}")


def test_aphorism_density_scan_divides_by_the_real_chapter_count():
    """🔴 RED BEFORE THE FIX: the splitter finds nothing, `n_ch or 6` silently substitutes SIX,
    and a three-chapter book's density is reported at half its real value.

    Asserted through the published rate rather than by adding a field: the scan's output shape
    is existing contract, and the denominator is observable without touching it."""
    aphorism = "Not all silence is agreement, and not all distance is safety."
    book = "\n\n".join(
        f"## Chapter {i}: {title}\n\n{body}" + (f"\n\n{aphorism}" if i == 2 else "")
        for i, (title, body) in enumerate(zip(CH_TITLES, BODIES), 1))

    out = ng.aphorism_density_scan(book, style="storytelling")
    hits = out["aphorism_hits"]
    assert hits >= 1, "the fixture produced no aphorism — the rate assertion would be vacuous"
    assert out["aphorism_rate_per_10_chapters"] == round(10.0 * hits / 3, 2), (
        "the rate was not computed over three chapters (6 is the no-chapters fallback)")
