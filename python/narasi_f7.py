"""narasi_f7.py — F7: deterministic chapter-balance telemetry over the FINAL manuscript.

🔴 F7 MEASURES; IT DOES NOT REPAIR. This module reports how a delivered book's chapter
   lengths sit relative to one another. It emits no verdict, no threshold, and no
   `balanced`/`unbalanced` label, and it never edits a byte of the manuscript. That
   restraint is the whole design: the moment telemetry answers "is this acceptable?" it is
   deciding delivery, and a decider on the delivery path needs the apparatus F6 has — a kill
   switch, a census it can refuse, a fail-closed path — none of which belongs in a counter.
   There is deliberately no feature flag here for the same reason: a flag guards an actuator,
   and this is not one.

🔴 WHY THE MEASUREMENT IS TAKEN AT PERSISTENCE, NOT IN THE COUNTER REPORT.
   `_apply_v3_gates()` runs BEFORE `_f6_finalize()`, and F6 repair may rewrite chapters after
   it — a balance report produced there can describe a book that was never delivered. The one
   place holding the bytes actually being persisted is `database.finish_narasi_job()`,
   immediately before the `done` payload is written, so the measurement is attached there.

🔴 THE GRAMMAR IS DERIVED FROM THE REPO'S CANONICAL 19-LANGUAGE VOCABULARY, NOT COPIED.
   Production renders chapter headings in the narrative's own language from
   `laozhang_api._NARASI_HEADER_LABELS` — 19 languages, 14 distinct heading shapes. The F6
   gate splitter knows only six Latin keywords plus two CJK forms, and its Korean form
   (`<prefix>N<suffix>`) is not even the one production emits, so measuring through it left
   SEVEN production languages permanently UNMEASURED. That file is ratified and frozen, so
   F7 cannot widen it and must not fork it either.

   `chapter_heading_patterns.BARE_WORD_RX` already carries the full 19-language vocabulary and
   is the canonical splitter `canon_lite_l2` uses. F7 EXTRACTS that alternation from it at
   import time rather than transcribing it: a third hand-copy of this list is how the second
   one lost `nl` to a transcription error, caught only by adversarial review. Extraction also
   means no non-Latin literal is ever retyped, and a language added upstream is picked up here
   for free. If the upstream shape ever changes, the extraction RAISES at import rather than
   silently degrading to a narrower grammar — a telemetry module that quietly stops seeing
   seven languages is exactly the failure being fixed.

🔴 EVERY ANSWER IS BOUNDED, AND A REFUSAL IS AN ANSWER. The report goes into a JSONB column on
   the jobs row, so it carries counts and 1-based indices only — never titles, snippets, prose,
   hashes, user data or exception text. When the book cannot be measured, the module returns an
   explicit `UNMEASURED` report naming a reason from a closed vocabulary, rather than raising
   into a delivery path or inventing a number.
"""
from __future__ import annotations

import re

import chapter_heading_patterns as _chp

#: Bumped whenever the MEASURED key set changes. A reader that does not know the version it is
#: handed must be able to say so instead of guessing at the shape.
SCHEMA_VERSION = "narasi.f7.chapter_balance.v1"

#: What is being counted, stated in the report itself: chapter BODIES only, split on
#: whitespace, headings and server framing excluded.
BASIS = "body_only_whitespace_words"

#: A book longer than this is a runaway answer, not a manuscript. The same bound the tense and
#: teleport censuses use: they describe the SAME chapters, and an unbounded list here is an
#: unbounded JSONB value.
MAX_CHAPTERS = 200

#: Server-owned fields that state how many chapters the job intended to deliver. `n_total`
#: comes from the Dalang payload builder, `chapters` from the classic and persist paths.
#: Both are written by the server; neither is model output.
EXPECTED_COUNT_KEYS = ("n_total", "chapters")

#: The closed vocabulary a refusal may use. Anything outside it is a bug, never "some other
#: reason" — an open-ended reason string is how manuscript text leaks into telemetry.
UNMEASURED_REASONS = frozenset({
    "no_markdown",             # nothing to measure: absent, empty, or not text
    "no_chapters",             # text with no recognisable chapter heading at all
    "malformed_headings",      # headings do not run 1..N in order
    "chapter_count_mismatch",  # the book does not hold the chapters the job says it has
    "too_many_chapters",       # beyond MAX_CHAPTERS
    "zero_word_chapter",       # an empty chapter — the ratio has no denominator
    "internal_error",          # measurement itself failed; never raised at the caller
})

#: Where the report is stored on the job's result payload.
PAYLOAD_KEY = "chapter_balance"


def _label_alternation() -> str:
    """The 19-language chapter-label alternation, lifted verbatim from the canonical pattern.

    Deliberately strict: if `BARE_WORD_RX` is ever rebuilt in a different shape, this raises at
    import instead of falling back to something narrower. A grammar that silently loses
    languages produces telemetry that looks healthy and covers two thirds of production."""
    pattern = _chp.BARE_WORD_RX.pattern
    prefix, suffix = r"(?m)(?=^[ \t]*(?:", "))"
    if not (pattern.startswith(prefix) and pattern.endswith(suffix)):
        raise RuntimeError(
            "narasi_f7: chapter_heading_patterns.BARE_WORD_RX no longer has the shape this "
            "grammar is derived from — refusing to fall back to a narrower vocabulary")
    alternation = pattern[len(prefix):-len(suffix)]
    if not alternation:
        raise RuntimeError("narasi_f7: derived chapter-label alternation is empty")
    return alternation


#: The alternation exactly as the canonical module spells it — asserted to be a substring of
#: `BARE_WORD_RX.pattern` by test, so a fork of this list cannot pass unnoticed.
LABEL_ALTERNATION = _label_alternation()

#: MARKED regime: the shape production actually emits (`orchestrator/static.py::_chapter_md`
#: renders `## <label> <n>: <title>`). The marker is REQUIRED here, and a label from the
#: canonical vocabulary is required with it — a generic `##` splitter would cut the book at
#: any markdown H2. Horizontal whitespace only (`[^\S\n]`) so a match can never begin on the
#: blank line before its heading and steal the separator.
_MARKED_HEADING_RX = re.compile(
    rf"(?im)^[^\S\n]*#{{1,3}}[^\S\n]*(?:{LABEL_ALTERNATION})[^\n]*$")

#: BARE regime: no marker anywhere in the document, so a line that opens with a chapter label
#: is the only thing a heading can be. CASE-SENSITIVE, exactly as the canonical bare matcher
#: is — without the marker to disambiguate, case is the last signal left, and a case-blind bare
#: matcher turns any sentence opening with "chapter"/"bab" into a chapter break.
_BARE_HEADING_RX = re.compile(rf"(?m)^[^\S\n]*(?:{LABEL_ALTERNATION})[^\n]*$")


def _heading_rx_for(text: str):
    """Pick ONE regime for the whole document. Marked and bare are mutually exclusive.

    🔴 THIS IS A CANONICAL RULE, NOT A PREFERENCE, AND DROPPING IT IS A REAL DEFECT.
    `chapter_heading_patterns` states it outright: the bare matcher must never run on a
    document that already carries `## `. Merging the two into one optional-hash pattern reads
    as harmless and is not — in a marked book, an ordinary sentence like
    "Chapter 11 filings rose sharply this year." or "Bab 2 dalam hidupnya baru saja dimulai."
    then becomes a chapter break, and the book is reported malformed because of its prose.

    The discriminator is `MARKER_RX` itself rather than a hand-written "does it contain `## `"
    test, for the same reason the vocabulary is derived rather than transcribed."""
    return _MARKED_HEADING_RX if _chp.MARKER_RX.search(text or "") else _BARE_HEADING_RX


#: The ordinal a heading carries. Every one of the 14 production shapes contains exactly one
#: `{n}`, so ONE digit run serves every language — no per-label ordinal grammar, and nothing to
#: keep in sync when a language is added. The run must be MAXIMAL (`(?<![0-9])`/`(?![0-9])`) or
#: `Chapter 12` reads as chapter one followed by stray text.
_ORDINAL_RX = re.compile(r"(?<![0-9])([0-9]{1,4})(?![0-9])")


def _heading_ordinal(heading: str):
    """The chapter number a heading carries, or None when what follows the digits says it is
    not an ordinal at all.

    🔴 A DIGIT IS NOT AN ORDINAL UNTIL IT ENDS LIKE ONE. `## Chapter 1a: Appendix` and
    `## Chapter 1.5: Half` both start with a 1, and reading them as chapter one lets an
    appendix or a half-chapter occupy a real chapter's index — every per-chapter number after
    it then describes the wrong chapter, silently.

    The rejected tails are ASCII-specific on purpose: the CJK shapes put their own suffix
    immediately after the digits (`<prefix>1<suffix>`), so a rule phrased as "no letter may
    follow" would reject the very languages this grammar exists to cover."""
    match = _ORDINAL_RX.search(heading)
    if not match:
        return None
    tail = heading[match.end():match.end() + 2]
    if tail[:1].isascii() and tail[:1].isalpha():
        return None
    if tail[:1] in (".", ",") and tail[1:2].isdigit():
        return None
    return int(match.group(1))


def _split_blocks(text: str, rx=None) -> list:
    """Split an assembled book into chapter blocks without losing a byte.

    `"".join(_split_blocks(t)) == t` for ANY input, preamble included. A book with no
    recognisable heading is one block. The regime is resolved once per document and passed
    down, so the splitter and the heading reader can never disagree about which one is in
    force."""
    rx = rx if rx is not None else _heading_rx_for(text or "")
    if not text:
        return []
    starts = [m.start() for m in rx.finditer(text)]
    if not starts:
        return [text]
    blocks = []
    if starts[0] > 0:
        blocks.append(text[:starts[0]])          # preamble, kept rather than discarded
    bounds = starts + [len(text)]
    blocks.extend(text[bounds[i]:bounds[i + 1]] for i in range(len(starts)))
    return blocks


def _heading_line(block: str, rx) -> str:
    """The heading line of a block exactly as written, or "" when the block has none."""
    match = rx.match(block)
    return match.group(0) if match else ""


def _word_counts(text: str) -> list:
    """Words per chapter body, 1-BASED: entry N is chapter N.

    The heading is excluded: the gates localise it per language, and a heading counted as prose
    would move a chapter's measurement for a reason that has nothing to do with what was
    written. A block with no heading — the preamble, or the `> **Gaya:** …` metadata header the
    gates prepend — is not a chapter and is not counted."""
    text = text or ""
    rx = _heading_rx_for(text)
    counts = []
    for block in _split_blocks(text, rx):
        heading = _heading_line(block, rx)
        if not heading:
            continue
        counts.append(len(block[len(heading):].split()))
    return counts


def _headings_well_formed(text: str) -> bool:
    """Do the headings run 1..N, in order, with no gaps and no repeats?

    Missing, extra, duplicated and reordered are ONE failure, not four: entry N of
    `word_counts` claims to be chapter N, and when the headings are not a clean 1..N that claim
    is false for the whole report rather than for any one entry."""
    text = text or ""
    rx = _heading_rx_for(text)
    ordinals = []
    for block in _split_blocks(text, rx):
        heading = _heading_line(block, rx)
        if not heading:
            continue
        ordinal = _heading_ordinal(heading)
        if ordinal is None:
            return False
        ordinals.append(ordinal)
    return bool(ordinals) and ordinals == list(range(1, len(ordinals) + 1))


def expected_chapter_counts(payload) -> frozenset:
    """The distinct, usable chapter counts the SERVER put on this payload.

    Returns a set because the payload may carry more than one such field, and two server-owned
    fields that disagree is itself a reason to refuse: the job cannot be both. Values that are
    not usable counts are dropped rather than refused — `True` is an `int` in Python and would
    otherwise read as "one chapter"."""
    if not isinstance(payload, dict):
        return frozenset()
    found = set()
    for key in EXPECTED_COUNT_KEYS:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        found.add(value)
    return frozenset(found)


def _unmeasured(reason: str) -> dict:
    """A refusal carries its reason and nothing else — no counts, no ratio, no prose."""
    return {"schema_version": SCHEMA_VERSION, "status": "UNMEASURED",
            "basis": BASIS, "reason": reason}


def chapter_balance(markdown, *, expected_chapters=frozenset()) -> dict:
    """Measure chapter balance over final Markdown. Pure, synchronous, provider-free.

    Returns either a MEASURED report — `chapter_count`, per-chapter `word_counts`, the extremes
    with every tied 1-based index preserved, and `max_min_ratio` rounded to two decimals — or an
    UNMEASURED report naming one closed reason.

    🔴 THE ORDER OF THE REFUSALS IS PART OF THE CONTRACT. The bound is checked before the
    heading sequence so a runaway answer is refused as runaway rather than as malformed; the
    count check follows the structural ones because comparing against a book whose own headings
    are unreadable says nothing; and the empty-chapter check comes last because it is the only
    one that depends on the counts being trustworthy in the first place.

    🔴 A BOOK MISSING CHAPTERS STILL MEASURES CLEANLY ON ITS OWN. Two chapters of a three
    chapter job are internally consistent — headings 1..2, a real ratio — so nothing about the
    manuscript alone reveals the loss. `expected_chapters` is the server's own statement of how
    many chapters the job holds, and disagreeing with it is a refusal, not a measurement of a
    truncated book presented as whole.

    🔴 A ZERO-WORD CHAPTER IS REFUSED, NOT DIVIDED BY. F6's ceiling reduction can legitimately
    empty a chapter, so this is a real production shape. `max/0` would either raise inside a
    delivery path or produce an infinity no JSON encoder accepts, and a report the jobs row
    cannot store is worse than no report."""
    try:
        if not isinstance(markdown, str) or not markdown.strip():
            return _unmeasured("no_markdown")
        counts = _word_counts(markdown)
        if not counts:
            return _unmeasured("no_chapters")
        if len(counts) > MAX_CHAPTERS:
            return _unmeasured("too_many_chapters")
        if not _headings_well_formed(markdown):
            return _unmeasured("malformed_headings")
        if expected_chapters and set(expected_chapters) != {len(counts)}:
            return _unmeasured("chapter_count_mismatch")
        fewest = min(counts)
        most = max(counts)
        if fewest <= 0:
            return _unmeasured("zero_word_chapter")
        ratio = round(most / fewest, 2)
    except Exception:  # noqa: BLE001 — telemetry may refuse, it may never raise here
        return _unmeasured("internal_error")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "MEASURED",
        "basis": BASIS,
        "chapter_count": len(counts),
        "word_counts": list(counts),
        "min_words": fewest,
        "max_words": most,
        "shortest_chapters": [n for n, words in enumerate(counts, 1) if words == fewest],
        "longest_chapters": [n for n, words in enumerate(counts, 1) if words == most],
        "max_min_ratio": ratio,
    }


def attach_chapter_balance(payload: dict) -> dict:
    """Return a shallow copy of `payload` carrying a freshly measured report.

    🔴 THE CALLER'S DICT IS INPUT, NOT SCRATCH SPACE. Mutating it in place would make the
    measurement visible to whatever else still holds a reference to that payload, which is how
    a "report" ends up describing one book while another is delivered.

    🔴 THE REPORT IS SERVER-OWNED AND ALWAYS RECOMPUTED. Any `chapter_balance` already on the
    payload is overwritten, never trusted: it can only have come from an earlier stage, and an
    earlier stage measured an earlier manuscript.

    The expected chapter count is read from the SAME payload, so the two halves of the
    comparison always describe one job."""
    return {**payload, PAYLOAD_KEY: chapter_balance(
        payload.get("markdown"), expected_chapters=expected_chapter_counts(payload))}
