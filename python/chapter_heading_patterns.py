# -*- coding: utf-8 -*-
r"""Single source of truth for "does this line look like a chapter heading" — shared by
`canon_lite_l2.materialize_final_snapshot` and `orchestrator.static._dedup_chapter_blocks` /
`_split_into_chunks`. Deliberately a dependency-free leaf (only `re`): `canon_lite_l2` is a
lower layer than `laozhang_api` (importing back would invert that), and
`orchestrator/static.py`'s C11 rule forbids importing Canon Lite on its flag-off path — this
module is neither, so both files can import it unconditionally at module level.

## Why two regexes, not one

`MARKER_RX` — "## " — is the canonical internal marker every assembly path currently traced
(`laozhang_api._chapter_label` call sites) emits. `BARE_WORD_RX` is the fallback: production
manuscripts are not proven to carry "## " on every path that reaches these splitters — the v9
canary artifact that triggered L3-Assist (`y1b503pf`) is a real, fully-delivered manuscript
whose chapter lines read "Chapter 1: ...", never "## Chapter 1: ...". Before this existed, a
manuscript shaped that way matched `MARKER_RX` zero times: `.split()` found no boundary at all,
so the entire book materialized as ZERO chapter blocks (misread as pre-chapter-1 front matter),
not even one. Every chapter-scoped repair and word-band check silently no-ops on such a
manuscript, with nothing that looks like an error.

## Why `chapter_split_rx_for` picks ONE regime for the whole document, never both

`BARE_WORD_RX` requires only a chapter-word + digit at line-start — it cannot tell a real
heading from a coincidental line of ordinary prose that happens to open that way (e.g. "Chapter
11 filings rose sharply..." inside a chapter about litigation, or "Bab 2 dalam hidupnya baru
saja dimulai kembali." inside dialogue). That is an acceptable trade against the alternative
(zero chapters detected) for a manuscript that has NO "## " markers anywhere. It is NOT
acceptable to apply inside a manuscript that already has real "## " headings: a false match
landing in body prose there would (verified against the real production functions, adversarial
code review 2026-08-15) shift every later chapter's positional `chapter_id` binding, let a
genuinely-duplicated chapter's stale content leak past `_dedup_chapter_blocks`'s dedup, split
one chapter's polish pass across two seam-blind chunks, and cause L3's own re-materialization
guards to reject valid repair candidates. `chapter_split_rx_for` structurally prevents all of
that by checking for `MARKER_RX` FIRST and using it exclusively whenever it finds anything —
`BARE_WORD_RX` only ever runs on a document that has already proven to contain zero "## "
markers, never as a supplement within one that does.

## Design notes on `BARE_WORD_RX` itself (each one closes a real gap found in review)

- Case-SENSITIVE, no `re.IGNORECASE`: real headings from `_chapter_label` are always Title
  Case ("Chapter {n}", "Bab {n}", ...); case-insensitivity bought no legitimate coverage while
  letting ordinary lowercase prose ("bab 5", "kapitel 3") collide.
- ASCII digits only (`[0-9]`, not `\d`): `\d` is Unicode-digit-aware (Arabic-Indic, Devanagari,
  Thai, fullwidth all match) but `_chapter_label` only ever formats a plain Python `int`, always
  ASCII — `\d` bought no real coverage while widening the collision surface for exactly the
  scripts most likely to reference a chapter number in ordinary prose.
- No `\b` before the digit: `\d` is itself `\w`, so `\b` can never hold at a
  letter-immediately-followed-by-digit transition — `Word\b[ \t]*\d` can therefore only ever
  match when real whitespace separates the word from the digit, silently failing on a glued
  form like "Chapter1: Opening" (a plausible further drift from the "## " convention, the exact
  failure class this module exists to catch). Dropping `\b` does not reopen a false-match door:
  the mandatory `[ \t]*` + digit immediately after the literal already rejects any longer word
  that merely starts with one of these tokens (e.g. Indonesian "Babak" ["act/scene"], Arabic
  "الفصلية" ["quarterly"]) because the letters continuing that longer word are neither
  whitespace nor a digit — verified empirically against real dictionary words in Arabic and
  Hindi during review; `\b` was redundant, not load-bearing, once the digit constraint exists.
- `第`/`章` (ja/zh) additionally accept the native ideographic full-width space (U+3000), not
  just ASCII space/tab — "第　1　章" is idiomatic native Japanese/Chinese typesetting, and the
  ASCII-only `[ \t]*` silently failed on it (same zero-chapter collapse).

## Known, deliberately deferred gaps (documented, not silently absent)

- NFD-normalized Vietnamese text (decomposed combining marks) defeats the literal "Chương"
  match — the source literal is NFC. No evidence this pipeline's own paths introduce NFD, and
  normalizing the manuscript text itself would break `materialize_final_snapshot`'s byte-exact
  round-trip contract, so this needs a genuinely NFC/NFD-tolerant pattern (or a normalize-only
  MATCHING step that never touches the reconstructed bytes) to close properly — left for a
  follow-up rather than rushed here.
- `(?m)^` (used inside both regexes) only recognizes `\n` as a line boundary, not Unicode line
  separators (U+2028, U+2029, NEL U+0085, vertical tab, form feed) or a bare `\r`. This is a
  pre-existing characteristic of `(?m)^` in Python's `re`, shared by the old "## "-only pattern
  and by every other regex-based splitter in this codebase — not a regression this module
  introduces, so out of scope here.
- The word list is hand-copied from the literal `"chapter"` template of every entry in
  `laozhang_api._NARASI_HEADER_LABELS` (19 languages: id/ms/jv/su/en/es/pt/fr/de/nl/it/ja/ko/zh/
  ar/hi/th/vi/tl) rather than imported, for the layering reason above. A first hand-copy of
  this exact list already dropped "nl" (Hoofdstuk) by transcription error, caught only by
  adversarial review, not by any test — a concrete reminder to re-diff this list against
  `_NARASI_HEADER_LABELS` whenever either changes, not just trust the copy.
"""

from __future__ import annotations

import re

#: The canonical, unambiguous internal marker. See module docstring.
MARKER_RX = re.compile(r"(?m)(?=^## )")

#: Bare chapter-word + digit fallback. See module docstring for every design choice below.
BARE_WORD_RX = re.compile(
    r"(?m)(?=^[ \t]*(?:"
    r"Bab[ \t]*[0-9]|Chapter[ \t]*[0-9]|Cap[íi]tulo[ \t]*[0-9]|Chapitre[ \t]*[0-9]"
    r"|Kapitel[ \t]*[0-9]|Capitolo[ \t]*[0-9]|Kabanata[ \t]*[0-9]|Hoofdstuk[ \t]*[0-9]"
    r"|الفصل[ \t]*[0-9]|अध्याय[ \t]*[0-9]|บทที่[ \t]*[0-9]|Chương[ \t]*[0-9]"
    r"|第[ \t　]*[0-9]+[ \t　]*章|[0-9]+[ \t]*장"
    r"))"
)


def chapter_split_rx_for(text: str) -> "re.Pattern[str]":
    """The one regex to `.split()` `text` with — `MARKER_RX` if `text` contains "## "
    anywhere, `BARE_WORD_RX` only if it contains it nowhere. Never mix the two within one
    document; see module docstring for why that matters."""
    return MARKER_RX if MARKER_RX.search(text) else BARE_WORD_RX
