"""Canon Lite L2a — final-snapshot reconciliation, offline core.

Package L2a of CANON-LITE-ARCHITECTURE-FINAL.md §13, planning record
L2-PLANNING-RECORD-001.md §§10-11. Pure, offline, report-only:

- `FinalChapterSnapshotV1` (§6.3) materialized from the EXACT delivered manuscript bytes
  at the post-gates seam, byte-exact and round-trippable;
- `ChapterClaimsV1` (§6.2) as a strict container for UNTRUSTED extractor output;
- a deterministic predicate engine (C6) whose semantic tiers refuse to answer without an
  accepted authority (D-L2-5);
- `ContinuityReportV1` (§6.4) with separate delivery/persistence binding states (D-L2-7),
  projected to bounded telemetry only (§10, C12).

This module performs NO provider I/O, NO persistence, and NO payload mutation. It never
substitutes bytes into the delivered result and never rewrites `narasi_chapters`
(D-L2-7 as ratified in §11.2) — that authority is L3's.

Zero rule, inherited from L1 and load-bearing here: a count of `0` means NOT MEASURED
unless the accompanying coverage state says the measurement actually completed. Every
aggregate therefore travels with its denominator and its coverage state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import canon_lite as _cl
from canon_lite import (  # strict foundation — do not re-implement these here
    UNKNOWN,
    CanonLiteV1,
    MAX_CHAPTERS,
    _digest,
    _norm_text,
    _reject_unknown_fields,
    _req_enum,
    _req_id,
    _req_sha256,
    _req_str,
    sha256_hex,
)


def _schema_error(msg: str) -> Exception:
    """Raise through the CURRENT `canon_lite` module, never a snapshot of it.

    `from canon_lite import CanonSchemaError` binds the class OBJECT at import time. The
    L1 suite legitimately calls `importlib.reload(canon_lite)` to re-resolve environment
    state, and a reload rebuilds every class in that module — after which a stale binding
    raises an exception that `except canon_lite.CanonSchemaError` no longer catches. For a
    module whose entire job is to refuse bad input, an uncatchable refusal is worse than a
    noisy one, so the class is resolved at RAISE time.
    """
    return _cl.CanonSchemaError(msg)


def _bounds_error(msg: str) -> Exception:
    """Same reasoning as `_schema_error`, for the bounds class."""
    return _cl.CanonBoundsError(msg)

SNAPSHOT_SCHEMA_VERSION = "final_chapter_snapshot_v1"
# v2 adds `canon_sha256` to ChapterClaimsV1. The provider's output depends on the exact
# canon supplied, so a claim that does not name its canon cannot be safely cached or
# reused. A v1 payload FAILS CLOSED — it is never silently upgraded, because a v1 claim
# genuinely does not know which canon produced it and inferring one would fabricate
# provenance. REPORT_SCHEMA_VERSION deliberately stays v1: ContinuityReportV1 already
# carries canon_sha256, so bumping it would force re-acceptance of an unchanged schema.
CLAIMS_SCHEMA_VERSION = "chapter_claims_v2"
REPORT_SCHEMA_VERSION = "continuity_report_v1"

#: Bumped whenever the split/offset algorithm changes. A snapshot is only comparable to
#: another snapshot built by the same materializer version.
MATERIALIZER_VERSION = "cl_l2_materializer_v1"

#: The predicate set a job snapshots at start (§7, §11). Promotion is per-predicate and
#: versioned; nothing is promoted to enforced in L2.
PREDICATE_SET_VERSION = _cl.L2_PREDICATE_SET_VERSION

# ---------------------------------------------------------------------------
# Bounds. Rejecting is always correct; truncating a manuscript never is.
# ---------------------------------------------------------------------------
#: L3-ASSIST Stage 2: **max two internal repair iterations per chapter**. Defined
#: HERE, next to the report field it bounds, and imported by the repair engine — two
#: copies of a ceiling are two ceilings, and the one that drifts is always the one
#: nobody is looking at.
MAX_REPAIR_ROUNDS = 2

MAX_MANUSCRIPT_BYTES = 8 * 1024 * 1024
MAX_CLAIMS_PER_CHAPTER = 200
MAX_VIOLATIONS = 400
MAX_EVIDENCE_BYTES = 4096
MAX_LABEL_LEN = 64

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

#: §10.2. `NO_CLAIMS_FOUND` is valid ONLY when extraction AND evidence validation both
#: completed — it is the one state that legitimately means "measured, and none found".
#: Every other non-`CHECKED` state means the measurement did not complete.
COVERAGE_CHECKED = "CHECKED"
COVERAGE_NO_CLAIMS_FOUND = "NO_CLAIMS_FOUND"
COVERAGE_NO_VIOLATIONS_FOUND = "NO_VIOLATIONS_FOUND"
COVERAGE_INCOMPLETE_EXTRACTION = "INCOMPLETE_EXTRACTION"
COVERAGE_INVALID_EXTRACTOR_OUTPUT = "INVALID_EXTRACTOR_OUTPUT"
COVERAGE_TIMEOUT = "TIMEOUT"
COVERAGE_PROVIDER_FAILURE = "PROVIDER_FAILURE"
COVERAGE_UNKNOWN_CANON_REFERENCE = "UNKNOWN_CANON_REFERENCE"
COVERAGE_NO_CANON_AUTHORITY = "NO_CANON_AUTHORITY"
COVERAGE_NOT_APPLICABLE = "NOT_APPLICABLE"

COVERAGE_STATES = (
    COVERAGE_CHECKED,
    COVERAGE_NO_CLAIMS_FOUND,
    COVERAGE_NO_VIOLATIONS_FOUND,
    COVERAGE_INCOMPLETE_EXTRACTION,
    COVERAGE_INVALID_EXTRACTOR_OUTPUT,
    COVERAGE_TIMEOUT,
    COVERAGE_PROVIDER_FAILURE,
    COVERAGE_UNKNOWN_CANON_REFERENCE,
    COVERAGE_NO_CANON_AUTHORITY,
    COVERAGE_NOT_APPLICABLE,
)

#: Extraction and predicate evaluation have different successful-zero states. The former
#: found no claims; the latter found no violations. Keeping them distinct prevents a
#: predicate report from claiming "no claims" after it successfully evaluated real claims.
_EXTRACTION_MEASURED = frozenset({COVERAGE_CHECKED, COVERAGE_NO_CLAIMS_FOUND})
_PREDICATE_MEASURED = frozenset({COVERAGE_CHECKED, COVERAGE_NO_VIOLATIONS_FOUND})

#: D-L2-7 / §11.2 — delivery and persistence bind separately and are reported separately.
BINDING_MATCH = "MATCH"
BINDING_MISMATCH = "MISMATCH"
BINDING_UNPROVED = "UNPROVED"
BINDING_NOT_APPLICABLE = "NOT_APPLICABLE"
BINDING_STATES = (BINDING_MATCH, BINDING_MISMATCH, BINDING_UNPROVED, BINDING_NOT_APPLICABLE)

#: Terminal continuity status. `CLEAN` is reachable only under the strict conditions in
#: `decide_continuity_status` — C7 forbids converting absence of evidence into clean.
STATUS_CLEAN = "clean"
STATUS_VIOLATIONS = "violations_found"
STATUS_UNRESOLVED = "unresolved"
STATUS_UNCHECKED = "unchecked"
CONTINUITY_STATUSES = (STATUS_CLEAN, STATUS_VIOLATIONS, STATUS_UNRESOLVED, STATUS_UNCHECKED)
REPORT_MODES = ("shadow", "assist", "enforce")

#: Predicate scopes (§7.3).
SCOPE_CHAPTER = "chapter"
SCOPE_ADJACENT_PAIR = "adjacent_pair"
SCOPE_GLOBAL = "global"
PREDICATE_SCOPES = (SCOPE_CHAPTER, SCOPE_ADJACENT_PAIR, SCOPE_GLOBAL)

#: Predicate identifiers. Only `structure` is evaluable without semantic authority; the
#: rest are the §7.1 candidates, all REPORT-ONLY in L2 and all gated by D-L2-5.
PREDICATE_STRUCTURE = "chapter_structure_identity"
PREDICATE_ENTITY_NAME = "entity_name_contradiction"
PREDICATE_FIXED_LITERAL = "fixed_literal_contradiction"
PREDICATE_ONE_TIME_EVENT = "one_time_event_duplication"
SEMANTIC_PREDICATES = (
    PREDICATE_ENTITY_NAME, PREDICATE_FIXED_LITERAL, PREDICATE_ONE_TIME_EVENT)
ALL_PREDICATES = (PREDICATE_STRUCTURE,) + SEMANTIC_PREDICATES

_PREDICATE_SCOPE = {
    PREDICATE_STRUCTURE: SCOPE_GLOBAL,
    PREDICATE_ENTITY_NAME: SCOPE_CHAPTER,
    PREDICATE_FIXED_LITERAL: SCOPE_CHAPTER,
    PREDICATE_ONE_TIME_EVENT: SCOPE_GLOBAL,
}

#: Closed claim types an extractor may assert. An extractor cannot widen this set.
CLAIM_ENTITY_MENTION = "entity_mention"
CLAIM_FIXED_LITERAL = "fixed_literal"
CLAIM_ONE_TIME_EVENT = "one_time_event"
CLAIM_TYPES = (CLAIM_ENTITY_MENTION, CLAIM_FIXED_LITERAL, CLAIM_ONE_TIME_EVENT)

#: Bounded violation codes. Never a manuscript span, never prose (C12, §10).
VIOLATION_CHAPTER_COUNT = "chapter_count_mismatch"
VIOLATION_CHAPTER_ORDER = "chapter_order_mismatch"
VIOLATION_CHAPTER_TITLE = "chapter_title_mismatch"
VIOLATION_CHAPTER_DUPLICATE = "chapter_number_duplicated"
VIOLATION_CHAPTER_IDENTITY_UNKNOWN = "chapter_identity_unreadable"
VIOLATION_ENTITY_NAME = "entity_name_contradiction"
VIOLATION_FIXED_LITERAL = "fixed_literal_contradiction"
VIOLATION_ONE_TIME_EVENT = "one_time_event_duplication"
VIOLATION_CODES = (
    VIOLATION_CHAPTER_COUNT, VIOLATION_CHAPTER_ORDER,
    VIOLATION_CHAPTER_TITLE, VIOLATION_CHAPTER_DUPLICATE,
    VIOLATION_CHAPTER_IDENTITY_UNKNOWN, VIOLATION_ENTITY_NAME,
    VIOLATION_FIXED_LITERAL, VIOLATION_ONE_TIME_EVENT,
)

_CLAIM_PREDICATE = {
    CLAIM_ENTITY_MENTION: PREDICATE_ENTITY_NAME,
    CLAIM_FIXED_LITERAL: PREDICATE_FIXED_LITERAL,
    CLAIM_ONE_TIME_EVENT: PREDICATE_ONE_TIME_EVENT,
}
_VIOLATION_PREDICATE = {
    VIOLATION_CHAPTER_COUNT: PREDICATE_STRUCTURE,
    VIOLATION_CHAPTER_ORDER: PREDICATE_STRUCTURE,
    VIOLATION_CHAPTER_TITLE: PREDICATE_STRUCTURE,
    VIOLATION_CHAPTER_DUPLICATE: PREDICATE_STRUCTURE,
    VIOLATION_CHAPTER_IDENTITY_UNKNOWN: PREDICATE_STRUCTURE,
    VIOLATION_ENTITY_NAME: PREDICATE_ENTITY_NAME,
    VIOLATION_FIXED_LITERAL: PREDICATE_FIXED_LITERAL,
    VIOLATION_ONE_TIME_EVENT: PREDICATE_ONE_TIME_EVENT,
}


class CanonLiteL2Error(Exception):
    """Base for L2 errors. Distinct from L1's so a caller can contain them separately."""


class SnapshotError(CanonLiteL2Error):
    """The manuscript could not be materialized into a valid snapshot."""


# ===========================================================================
# §6.3 FinalChapterSnapshotV1 — the exact delivered bytes
# ===========================================================================

#: Shared with `orchestrator.static._POLISH_CHAPTER_SPLIT_RX`. A chapter block starts at a
#: line beginning `## `. Kept as its own constant so a change here is visible in review
#: rather than inherited silently from a module with a different purpose.
_CHAPTER_SPLIT_RX = re.compile(r"(?m)(?=^## )")

#: Deliberately mirrors `orchestrator.static._CH_HEADER_NUM_RX`, INCLUDING its r16 audit
#: fix: the negative lookahead stops "Chapter 3a"/"Chapter 3.5" from collapsing into plain
#: "Chapter 3". That bug once deleted genuinely distinct chapters as false duplicates; a
#: fresh regex here would reintroduce it.
_CH_HEADER_NUM_RX = re.compile(r"^##\s+\D*?(\d+)(?![a-z.])")


@dataclass(frozen=True, slots=True)
class ChapterBlockV1:
    """One materialized chapter block, bound to its exact byte range."""
    index: int                  # 0-based position in the ordered block list
    chapter_id: str             # accepted outline id for this position, or UNKNOWN
    heading_number: int         # parsed `## ... N`, or -1 when unparseable
    byte_start: int             # inclusive, into the UTF-8 manuscript bytes
    byte_end: int               # exclusive
    content_sha256: str         # sha256 of the block's exact UTF-8 bytes

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise _schema_error("index: expected a non-negative int")
        if self.chapter_id != UNKNOWN:
            _req_id(self.chapter_id, "chapter_id")
        if isinstance(self.heading_number, bool) or not isinstance(self.heading_number, int) \
                or self.heading_number < -1:
            raise _schema_error("heading_number: expected int >= -1")
        for name in ("byte_start", "byte_end"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _schema_error(f"{name}: expected a non-negative int")
        if self.byte_end <= self.byte_start:
            raise _schema_error("byte range: byte_end must exceed byte_start")
        _req_sha256(self.content_sha256, "content_sha256")

    def to_canonical_obj(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "chapter_id": self.chapter_id,
            "heading_number": self.heading_number,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class FinalChapterSnapshotV1:
    """§6.3. The exact delivered manuscript, split but never rewritten.

    `prefix_bytes` + every block's byte range reconstruct the original manuscript byte for
    byte. Nothing here normalizes, trims, or reflows prose: the snapshot's whole purpose is
    to be the same bytes the user receives, so any normalization would make the binding a
    lie (C9).
    """
    schema_version: str
    materializer_version: str
    source_key: str                       # "book" or "output" — which result key was read
    manuscript_bytes: bytes               # the exact UTF-8 manuscript
    prefix_byte_end: int                  # bytes before the first `## ` heading
    blocks: tuple[ChapterBlockV1, ...]
    ordered_blocks_sha256: str
    manuscript_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != SNAPSHOT_SCHEMA_VERSION:
            raise _schema_error(
                f"schema_version: expected {SNAPSHOT_SCHEMA_VERSION!r}")
        if self.materializer_version != MATERIALIZER_VERSION:
            raise _schema_error(
                f"materializer_version: expected {MATERIALIZER_VERSION!r}")
        _req_enum(self.source_key, "source_key", ("book", "output"))
        if not isinstance(self.manuscript_bytes, bytes):
            raise _schema_error("manuscript_bytes: expected bytes")
        if isinstance(self.prefix_byte_end, bool) or not isinstance(
                self.prefix_byte_end, int) or not (
                    0 <= self.prefix_byte_end <= len(self.manuscript_bytes)):
            raise _schema_error("prefix_byte_end: outside manuscript byte range")
        if not isinstance(self.blocks, tuple):
            raise _schema_error("blocks: expected tuple")
        if len(self.blocks) > MAX_CHAPTERS:
            raise _bounds_error(
                f"blocks: {len(self.blocks)} exceeds MAX_CHAPTERS {MAX_CHAPTERS}")
        _req_sha256(self.ordered_blocks_sha256, "ordered_blocks_sha256")
        _req_sha256(self.manuscript_sha256, "manuscript_sha256")
        cursor = self.prefix_byte_end
        ordered_rows: list[dict[str, str]] = []
        for i, block in enumerate(self.blocks):
            if not isinstance(block, ChapterBlockV1):
                raise _schema_error(
                    f"blocks[{i}]: expected ChapterBlockV1, got {type(block).__name__}")
            if block.index != i:
                raise _schema_error(f"blocks[{i}].index: expected exactly {i}")
            if block.byte_start != cursor or block.byte_end > len(self.manuscript_bytes):
                raise SnapshotError(
                    f"blocks[{i}]: ranges must be contiguous and inside manuscript")
            exact = self.manuscript_bytes[block.byte_start:block.byte_end]
            if sha256_hex(exact) != block.content_sha256:
                raise SnapshotError(f"blocks[{i}].content_sha256 does not bind its bytes")
            ordered_rows.append({
                "chapter_id": block.chapter_id,
                "content_sha256": block.content_sha256,
            })
            cursor = block.byte_end
        if cursor != len(self.manuscript_bytes):
            raise SnapshotError("blocks do not consume the complete manuscript suffix")
        expected_ordered = _digest("canon_lite_l2.ordered_blocks", ordered_rows)
        if expected_ordered != self.ordered_blocks_sha256:
            raise SnapshotError("ordered_blocks_sha256 does not bind the ordered blocks")
        # The round-trip is an INVARIANT, not a test-only property: an instance that
        # cannot reconstruct its own source bytes must not exist at all.
        if self.reconstruct() != self.manuscript_bytes:
            raise SnapshotError("blocks do not reconstruct the manuscript byte for byte")
        if sha256_hex(self.manuscript_bytes) != self.manuscript_sha256:
            raise SnapshotError("manuscript_sha256 does not bind manuscript_bytes")

    # -- exactness --------------------------------------------------------
    def reconstruct(self) -> bytes:
        """Rebuild the source bytes from the prefix plus every block range, in order."""
        out = bytearray(self.manuscript_bytes[: self.prefix_byte_end])
        for b in self.blocks:
            out += self.manuscript_bytes[b.byte_start: b.byte_end]
        return bytes(out)

    def block_bytes(self, index: int) -> bytes:
        b = self.blocks[index]
        return self.manuscript_bytes[b.byte_start: b.byte_end]

    @property
    def chapter_count(self) -> int:
        return len(self.blocks)

    # -- bounded projection ----------------------------------------------
    def to_canonical_obj(self) -> dict[str, Any]:
        """Hashable form. Carries hashes and offsets — never a manuscript byte."""
        return {
            "schema_version": self.schema_version,
            "materializer_version": self.materializer_version,
            "source_key": self.source_key,
            "prefix_byte_end": self.prefix_byte_end,
            "blocks": [b.to_canonical_obj() for b in self.blocks],
            "ordered_blocks_sha256": self.ordered_blocks_sha256,
            "manuscript_sha256": self.manuscript_sha256,
        }


def resolve_manuscript_key(result: Mapping[str, Any]) -> Optional[str]:
    """Pick the result key the manuscript lives under, exactly as the delivery path does.

    `narration_api`'s post-gates dedup guard resolves
    `"book" if result.get("book") else "output"`. Scenarios C/D/E carry `output`. Mirroring
    that choice is load-bearing: reading only `book` would materialize an EMPTY snapshot for
    those scenarios, and an empty snapshot reports coverage `0` — which, under the zero
    rule, is indistinguishable from "measured and found nothing" unless the state says
    otherwise. Returning None here is what keeps that case honest.
    """
    if not isinstance(result, Mapping):
        return None
    if result.get("book"):
        return "book"
    if result.get("output"):
        return "output"
    return None


def materialize_final_snapshot(
    result: Mapping[str, Any],
    *,
    canon: Optional[CanonLiteV1] = None,
) -> Optional[FinalChapterSnapshotV1]:
    """Materialize §6.3 from the delivered result. Returns None when there is no text.

    Deterministic by construction: the same manuscript always yields the same offsets and
    the same hashes. Offsets are BYTE offsets into the UTF-8 encoding, never character
    indices — a character index would silently mis-slice every CJK or accented manuscript.
    """
    if canon is not None and not isinstance(canon, _cl.CanonLiteV1):
        raise _schema_error("canon: expected CanonLiteV1 or None")
    key = resolve_manuscript_key(result)
    if key is None:
        return None
    text = result.get(key)
    if not isinstance(text, str) or not text:
        return None

    raw = text.encode("utf-8")
    if len(raw) > MAX_MANUSCRIPT_BYTES:
        raise _bounds_error(
            f"manuscript: {len(raw)} bytes exceeds {MAX_MANUSCRIPT_BYTES}")

    # Split on the *string* so the regex semantics match the delivery path exactly, then
    # convert each piece to a byte range by encoding the prefix walked so far.
    pieces = _CHAPTER_SPLIT_RX.split(text)
    prefix_text = pieces[0] if pieces and not pieces[0].startswith("## ") else ""
    chapter_texts = pieces[1:] if prefix_text else [p for p in pieces if p]

    if len(chapter_texts) > MAX_CHAPTERS:
        raise _bounds_error(
            f"chapters: {len(chapter_texts)} exceeds MAX_CHAPTERS {MAX_CHAPTERS}")

    cursor = len(prefix_text.encode("utf-8"))
    prefix_byte_end = cursor
    blocks: list[ChapterBlockV1] = []
    for i, ch_text in enumerate(chapter_texts):
        ch_raw = ch_text.encode("utf-8")
        start, end = cursor, cursor + len(ch_raw)
        m = _CH_HEADER_NUM_RX.match(ch_text)
        # -1, not 0: an unparseable heading is UNKNOWN, and 0 would be a real number.
        number = int(m.group(1)) if m else -1
        chapter_id = (
            canon.chapters[i].chapter_id
            if canon is not None and i < len(canon.chapters)
            else UNKNOWN
        )
        blocks.append(ChapterBlockV1(
            index=i, chapter_id=chapter_id, heading_number=number,
            byte_start=start, byte_end=end,
            content_sha256=sha256_hex(ch_raw),
        ))
        cursor = end

    ordered = _digest(
        "canon_lite_l2.ordered_blocks",
        [{"chapter_id": b.chapter_id, "content_sha256": b.content_sha256}
         for b in blocks],
    )
    return FinalChapterSnapshotV1(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        materializer_version=MATERIALIZER_VERSION,
        source_key=key,
        manuscript_bytes=raw,
        prefix_byte_end=prefix_byte_end,
        blocks=tuple(blocks),
        ordered_blocks_sha256=ordered,
        manuscript_sha256=sha256_hex(raw),
    )


# ===========================================================================
# §6.2 ChapterClaimsV1 — a strict container for UNTRUSTED output
# ===========================================================================

# 🔴 THE WIRE ASKS FOR A QUOTE; THE SERVER DERIVES THE SPAN AND THE HASH.
#    This used to be ("claim_type", "canon_ref", "evidence_start", "evidence_end",
#    "evidence_sha256") — the extractor required the MODEL to count byte offsets and to
#    produce a SHA-256 of the span. A language model can do neither reliably, and the
#    parser then recomputed the hash and compared it to the model's, so a mismatch failed
#    the WHOLE payload. Live canaries `wbkc80eq` and `p64wz5kp` proved it: attempt 1
#    reached the provider, was billed, returned well-formed JSON, and was rejected here
#    every time — 3 units, 9 attempts, 0 usable claims, `continuity_status=unchecked`.
#
#    The model now returns the evidence VERBATIM and the server locates it. That removes
#    an impossible ask without weakening anything: the hash was already server-computed,
#    so the model's copy contributed no assurance — only a failure mode. Provenance is
#    strictly stronger now, because the span and its digest are derived from the delivered
#    bytes rather than asserted by the thing being checked.
_CLAIM_FIELDS = ("claim_type", "canon_ref", "quote")
_CLAIMS_PAYLOAD_FIELDS = (
    "schema_version", "chapter_index", "chapter_id", "content_sha256", "canon_sha256",
    "extractor_version", "model_version", "prompt_sha256", "predicate_set_version",
    "coverage", "claims",
)


@dataclass(frozen=True, slots=True)
class ObservedClaimV1:
    """One observed claim. `canon_ref` REFERENCES existing canon; it never creates it."""
    claim_type: str
    canon_ref: str              # an existing canon id, or UNKNOWN
    evidence_start: int         # byte offset into the chapter block
    evidence_end: int
    evidence_sha256: str

    def __post_init__(self) -> None:
        _req_enum(self.claim_type, "claim_type", CLAIM_TYPES)
        if self.canon_ref != UNKNOWN:
            _req_id(self.canon_ref, "canon_ref")
        for f in ("evidence_start", "evidence_end"):
            v = getattr(self, f)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise _schema_error(f"{f}: expected a non-negative int")
        if self.evidence_end <= self.evidence_start:
            raise _schema_error("evidence range: end must exceed start")
        if self.evidence_end - self.evidence_start > MAX_EVIDENCE_BYTES:
            raise _bounds_error(
                f"evidence range: exceeds {MAX_EVIDENCE_BYTES} bytes")
        _req_sha256(self.evidence_sha256, "evidence_sha256")

    def to_canonical_obj(self) -> dict[str, Any]:
        return {
            "claim_type": self.claim_type, "canon_ref": self.canon_ref,
            "evidence_start": self.evidence_start, "evidence_end": self.evidence_end,
            "evidence_sha256": self.evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class PredicateCoverageV1:
    """Extraction coverage for one semantic predicate in one exact chapter."""
    predicate: str
    state: str

    def __post_init__(self) -> None:
        _req_enum(self.predicate, "predicate", SEMANTIC_PREDICATES)
        _req_enum(self.state, "state", COVERAGE_STATES)
        if self.state == COVERAGE_NO_VIOLATIONS_FOUND:
            raise _schema_error(
                "state: NO_VIOLATIONS_FOUND belongs to evaluation, not extraction")

    def to_canonical_obj(self) -> dict[str, Any]:
        return {"predicate": self.predicate, "state": self.state}


@dataclass(frozen=True, slots=True)
class ChapterClaimsV1:
    """§6.2. Observations about ONE chapter version, bound to its exact bytes.

    The binding to `content_sha256` is what makes a cached claim reusable only for the
    exact bytes it was extracted from (§11 cache key). It is also what makes a claim
    invalid the moment the chapter changes (C8).
    """
    schema_version: str
    chapter_index: int
    chapter_id: str
    content_sha256: str
    canon_sha256: str
    extractor_version: str
    model_version: str
    prompt_sha256: str
    predicate_set_version: str
    coverage: tuple[PredicateCoverageV1, ...]
    claims: tuple[ObservedClaimV1, ...]

    def __post_init__(self) -> None:
        if self.schema_version != CLAIMS_SCHEMA_VERSION:
            raise _schema_error(f"schema_version: expected {CLAIMS_SCHEMA_VERSION!r}")
        if isinstance(self.chapter_index, bool) or not isinstance(self.chapter_index, int) \
                or self.chapter_index < 0:
            raise _schema_error("chapter_index: expected a non-negative int")
        if self.chapter_id != UNKNOWN:
            _req_id(self.chapter_id, "chapter_id")
        _req_sha256(self.content_sha256, "content_sha256")
        # `canon_sha256` mirrors the existing chapter_id treatment: UNKNOWN by explicit
        # exemption rather than by weakening _req_sha256. A dataclass cannot see the
        # caller's argument, so it can only enforce the STRUCTURAL half — UNKNOWN implies
        # a no-authority artefact. The stronger contextual rule (UNKNOWN is permitted
        # only when the caller supplied canon=None) lives in parse_chapter_claims, which
        # does receive the canon.
        if self.canon_sha256 != UNKNOWN:
            _req_sha256(self.canon_sha256, "canon_sha256")
        _req_str(self.extractor_version, "extractor_version", max_len=MAX_LABEL_LEN,
                 allow_unknown=False)
        _req_str(self.model_version, "model_version", max_len=MAX_LABEL_LEN,
                 allow_unknown=False)
        _req_sha256(self.prompt_sha256, "prompt_sha256")
        if self.predicate_set_version != PREDICATE_SET_VERSION:
            raise _schema_error(
                f"predicate_set_version: expected {PREDICATE_SET_VERSION!r}")
        if not isinstance(self.coverage, tuple):
            raise _schema_error("coverage: expected tuple")
        if tuple(c.predicate for c in self.coverage) != SEMANTIC_PREDICATES:
            raise _schema_error(
                "coverage: expected every semantic predicate exactly once in canonical order")
        for i, cov in enumerate(self.coverage):
            if not isinstance(cov, PredicateCoverageV1):
                raise _schema_error(
                    f"coverage[{i}]: expected PredicateCoverageV1")
        if not isinstance(self.claims, tuple):
            raise _schema_error("claims: expected tuple")
        if len(self.claims) > MAX_CLAIMS_PER_CHAPTER:
            raise _bounds_error(
                f"claims: {len(self.claims)} exceeds {MAX_CLAIMS_PER_CHAPTER}")
        by_predicate = {p: [] for p in SEMANTIC_PREDICATES}
        for i, claim in enumerate(self.claims):
            if not isinstance(claim, ObservedClaimV1):
                raise _schema_error(f"claims[{i}]: expected ObservedClaimV1")
            by_predicate[_CLAIM_PREDICATE[claim.claim_type]].append(claim)
        for cov in self.coverage:
            rows = by_predicate[cov.predicate]
            if rows and cov.state != COVERAGE_CHECKED:
                raise _schema_error(
                    f"coverage[{cov.predicate}]: claims require CHECKED")
            if not rows and cov.state == COVERAGE_CHECKED:
                raise _schema_error(
                    f"coverage[{cov.predicate}]: zero claims requires NO_CLAIMS_FOUND")
        # Structural half of the UNKNOWN invariant, checked after coverage and claims are
        # known to be well-formed so the failure names the real defect.
        if self.canon_sha256 == UNKNOWN and (
                self.claims
                or any(c.state != COVERAGE_NO_CANON_AUTHORITY for c in self.coverage)):
            raise _schema_error(
                "canon_sha256: UNKNOWN requires NO_CANON_AUTHORITY coverage and no claims")

    @property
    def measured(self) -> bool:
        """True only when every predicate's extraction and evidence validation completed."""
        return all(c.state in _EXTRACTION_MEASURED for c in self.coverage)

    @property
    def coverage_state(self) -> str:
        """Bounded aggregate for callers that need one lifecycle label."""
        states = tuple(c.state for c in self.coverage)
        if len(set(states)) == 1:
            return states[0]
        if all(s in _EXTRACTION_MEASURED for s in states):
            return COVERAGE_CHECKED if self.claims else COVERAGE_NO_CLAIMS_FOUND
        return COVERAGE_INCOMPLETE_EXTRACTION

    def state_for(self, predicate: str) -> str:
        _req_enum(predicate, "predicate", SEMANTIC_PREDICATES)
        return next(c.state for c in self.coverage if c.predicate == predicate)

    def to_canonical_obj(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "chapter_index": self.chapter_index,
            "chapter_id": self.chapter_id,
            "content_sha256": self.content_sha256,
            "canon_sha256": self.canon_sha256,
            "extractor_version": self.extractor_version,
            "model_version": self.model_version,
            "prompt_sha256": self.prompt_sha256,
            "predicate_set_version": self.predicate_set_version,
            "coverage": [c.to_canonical_obj() for c in self.coverage],
            "claims": [c.to_canonical_obj() for c in self.claims],
        }


def parse_chapter_claims(
    payload: Mapping[str, Any],
    *,
    snapshot: FinalChapterSnapshotV1,
    canon: Optional[CanonLiteV1],
) -> ChapterClaimsV1:
    """Convert UNTRUSTED extractor output into a strict artifact, or refuse.

    Refusing is the correct outcome far more often than accepting. Anything that cannot be
    fully validated becomes an incomplete-coverage artifact rather than an exception the
    caller might mistake for "nothing to report" (C7).
    """
    _reject_unknown_fields(payload, _CLAIMS_PAYLOAD_FIELDS, "chapter_claims")
    if payload.get("schema_version") != CLAIMS_SCHEMA_VERSION:
        raise _schema_error(
            f"schema_version: expected {CLAIMS_SCHEMA_VERSION!r}")

    index = payload.get("chapter_index")
    if isinstance(index, bool) or not isinstance(index, int) \
            or not (0 <= index < len(snapshot.blocks)):
        raise _schema_error("chapter_index: outside the snapshot's block range")

    block_meta = snapshot.blocks[index]
    chapter_id = payload.get("chapter_id")
    if chapter_id != block_meta.chapter_id:
        raise _schema_error("chapter_id: does not bind the snapshot block")
    content_sha = _req_sha256(payload.get("content_sha256"), "content_sha256")
    if content_sha != block_meta.content_sha256:
        raise _schema_error("content_sha256: does not bind the snapshot block")

    # ---- canon binding, BOTH directions -----------------------------------
    # Rejecting UNKNOWN when a canon was supplied is necessary but NOT sufficient: a
    # payload naming a DIFFERENT real SHA-256 must also be refused. Both checks run here,
    # before artifact construction and before any cache access, so a mis-bound payload
    # never becomes an object and never reaches a cache key.
    canon_sha = payload.get("canon_sha256")
    if canon is None:
        if canon_sha != UNKNOWN:
            raise _schema_error("canon_sha256: expected UNKNOWN when no canon was supplied")
    else:
        # The two conjuncts are INDEPENDENT, and agreement between payload and canon does
        # not satisfy the first. A canon whose verify_sha256() is False is rejected even
        # when the payload equals its tampered canon_sha256 — the two agree, but on a hash
        # that does not describe the canon's own content. Consistency with a corrupted
        # canon is not provenance; it is two copies of the same wrong fact.
        if not canon.verify_sha256():
            raise _schema_error("canon_sha256: supplied canon is not self-consistent")
        _req_sha256(canon_sha, "canon_sha256")
        if canon_sha != canon.canon_sha256:
            raise _schema_error("canon_sha256: does not bind the supplied canon")
    version = _req_str(payload.get("extractor_version"), "extractor_version",
                       max_len=MAX_LABEL_LEN)
    model_version = _req_str(
        payload.get("model_version"), "model_version", max_len=MAX_LABEL_LEN)
    prompt_sha256 = _req_sha256(payload.get("prompt_sha256"), "prompt_sha256")
    if payload.get("predicate_set_version") != PREDICATE_SET_VERSION:
        raise _schema_error(
            f"predicate_set_version: expected {PREDICATE_SET_VERSION!r}")

    raw_coverage = payload.get("coverage")
    _reject_unknown_fields(raw_coverage, SEMANTIC_PREDICATES, "coverage")
    coverage = tuple(
        PredicateCoverageV1(
            predicate=predicate,
            state=_req_enum(
                raw_coverage[predicate], f"coverage.{predicate}", COVERAGE_STATES))
        for predicate in SEMANTIC_PREDICATES
    )

    raw_claims = payload.get("claims")
    # This boundary accepts JSON, not convenient Python lookalikes. A tuple can never
    # arrive from a provider JSON response; accepting it would make the test-only parser
    # wider than the production wire contract.
    if not isinstance(raw_claims, list):
        raise _schema_error("claims: expected a list")

    block = snapshot.block_bytes(index)
    accepted_by_type = _accepted_canon_ids_by_claim_type(canon)
    parsed: list[ObservedClaimV1] = []
    for i, rc in enumerate(raw_claims):
        _reject_unknown_fields(rc, _CLAIM_FIELDS, f"claims[{i}]")
        quote = rc.get("quote")
        # `type(x) is str`, not isinstance: this value is about to be encoded, searched
        # for and hashed, and a str SUBCLASS with a poisoned __eq__ must not reach any of
        # that. Same discipline as the P0-A verdict canonicaliser.
        if type(quote) is not str or not quote:
            raise _schema_error(f"claims[{i}]: quote must be a non-empty string")
        needle = quote.encode("utf-8")
        # 🔴 EXACTLY ONE OCCURRENCE. Zero means the extractor cited text this chapter does
        #    not contain — a fabricated citation, which is the single most important thing
        #    this parser exists to refuse. More than one means the citation does not
        #    identify a span, and picking the first would silently invent a location the
        #    model never chose. Ambiguity is a refusal, never a guess.
        hits = block.count(needle)
        if hits == 0:
            raise _schema_error(f"claims[{i}]: quote not found in the chapter block")
        if hits > 1:
            raise _schema_error(f"claims[{i}]: quote is ambiguous in the chapter block")
        start = block.find(needle)
        claim = ObservedClaimV1(
            claim_type=rc["claim_type"], canon_ref=rc["canon_ref"],
            evidence_start=start, evidence_end=start + len(needle),
            evidence_sha256=sha256_hex(needle),
        )
        # Retained though the derivation above cannot violate it: UTF-8 is
        # self-synchronising, so a valid encoded needle can only match on a code-point
        # boundary. Keeping the check means the invariant is enforced by code rather than
        # by an argument in a comment, and it survives any future change to how the span
        # is chosen.
        try:
            block[claim.evidence_start: claim.evidence_end].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _schema_error(
                f"claims[{i}]: evidence range splits a UTF-8 code point") from None
        # D-L2-5: an extractor may not mint an id and then rely on it as authority.
        if claim.canon_ref == UNKNOWN or claim.canon_ref not in accepted_by_type[
                claim.claim_type]:
            raise _schema_error(
                f"claims[{i}]: canon_ref is not accepted for {claim.claim_type}")
        parsed.append(claim)

    return ChapterClaimsV1(
        schema_version=CLAIMS_SCHEMA_VERSION, chapter_index=index,
        chapter_id=chapter_id, content_sha256=content_sha, canon_sha256=canon_sha,
        extractor_version=version, model_version=model_version,
        prompt_sha256=prompt_sha256, predicate_set_version=PREDICATE_SET_VERSION,
        coverage=coverage, claims=tuple(parsed),
    )


#: §4.4 — the claim cache key, frozen. Nine elements, this exact order, no omissions, no
#: reordering, no implementer-selected additions. Every element changes the MEANING of the
#: cached claim: the schema it obeys, WHICH CHAPTER it is, the bytes it read, the canon it
#: was judged against, the extractor and model that produced it, the request contract it
#: was asked under, and the predicate vocabulary it may use.
CLAIM_CACHE_KEY_FIELDS = (
    "schema_version", "chapter_index", "chapter_id", "content_sha256", "canon_sha256",
    "extractor_version", "model_version", "prompt_sha256", "predicate_set_version",
)


def claim_cache_key(artifact: ChapterClaimsV1) -> tuple:
    """The frozen nine-element cache identity for one claim artefact.

    `chapter_index` and `chapter_id` are part of the key because both are sent to the
    provider and both are carried on the artefact: without them two chapters with
    IDENTICAL bytes at different identities collide, and a duplicated passage would serve
    one chapter's claims for another, at a different index.
    """
    if not isinstance(artifact, ChapterClaimsV1):
        raise _schema_error("claim_cache_key: expected ChapterClaimsV1")
    if artifact.canon_sha256 == UNKNOWN:
        # UNKNOWN is never cacheable. Not because it could collide — canon_sha256 is
        # itself an element of the key, so UNKNOWN and a real hash produce different keys.
        # It is refused because it identifies NO authoritative input, so the entry names
        # nothing that could be matched against; and because the no-canon result is
        # locally reproducible with zero provider work, so caching it buys nothing and
        # only creates an entry whose meaning depends on an argument the key never records.
        raise _schema_error("claim_cache_key: UNKNOWN canon is not cacheable")
    return tuple(getattr(artifact, name) for name in CLAIM_CACHE_KEY_FIELDS)


def _accepted_canon_ids(canon: Optional[CanonLiteV1]) -> frozenset[str]:
    """Every id an extractor is allowed to REFERENCE. Empty when there is no authority."""
    if canon is None:
        return frozenset()
    ids: set[str] = set()
    ids.update(e.entity_id for e in canon.entities)
    ids.update(a.anchor_id for a in canon.anchors)
    ids.update(ev.event_id for ev in canon.one_time_events)
    return frozenset(ids)


def _accepted_canon_ids_by_claim_type(
    canon: Optional[CanonLiteV1],
) -> dict[str, frozenset[str]]:
    if canon is None:
        return {claim_type: frozenset() for claim_type in CLAIM_TYPES}
    return {
        CLAIM_ENTITY_MENTION: frozenset(e.entity_id for e in canon.entities),
        CLAIM_FIXED_LITERAL: frozenset(a.anchor_id for a in canon.anchors),
        CLAIM_ONE_TIME_EVENT: frozenset(e.event_id for e in canon.one_time_events),
    }


def has_semantic_authority(canon: Optional[CanonLiteV1]) -> bool:
    """Whether any semantic predicate can be evaluated at all (D-L2-5, §11.3).

    L1's canon is STRUCTURAL: its entity, anchor and event tuples are empty, so there is
    nothing for a semantic predicate to contradict. Returning False here is what turns
    that into an honest `NO_CANON_AUTHORITY` instead of a hollow "zero violations".
    """
    return bool(_accepted_canon_ids(canon))


def has_predicate_authority(
    predicate: str, canon: Optional[CanonLiteV1],
) -> bool:
    """Authority is predicate-specific; an entity never authorizes a time/event claim."""
    _req_enum(predicate, "predicate", SEMANTIC_PREDICATES)
    if canon is None:
        return False
    if predicate == PREDICATE_ENTITY_NAME:
        return bool(canon.entities)
    if predicate == PREDICATE_FIXED_LITERAL:
        return bool(canon.anchors)
    return bool(canon.one_time_events)


# ===========================================================================
# §7 deterministic predicate engine — C6: code decides, never the extractor
# ===========================================================================

@dataclass(frozen=True, slots=True)
class ViolationV1:
    """A bounded finding. Code + chapter index only — never a span, never prose (§10)."""
    code: str
    predicate: str
    chapter_index: int          # -1 when the finding is document-global

    def __post_init__(self) -> None:
        _req_enum(self.code, "code", VIOLATION_CODES)
        _req_enum(self.predicate, "predicate", ALL_PREDICATES)
        if self.predicate != _VIOLATION_PREDICATE[self.code]:
            raise _schema_error("predicate: does not match violation code")
        if isinstance(self.chapter_index, bool) or not isinstance(
                self.chapter_index, int) or self.chapter_index < -1:
            raise _schema_error("chapter_index: expected -1 or a non-negative int")

    def to_canonical_obj(self) -> dict[str, Any]:
        return {"code": self.code, "predicate": self.predicate,
                "chapter_index": self.chapter_index}


@dataclass(frozen=True, slots=True)
class PredicateResultV1:
    """One predicate's outcome, always carrying its own denominator."""
    predicate: str
    scope: str
    coverage_state: str
    checked_units: int          # how many units were actually evaluated
    total_units: int            # the denominator
    violations: tuple[ViolationV1, ...]

    def __post_init__(self) -> None:
        _req_enum(self.predicate, "predicate", ALL_PREDICATES)
        _req_enum(self.scope, "scope", PREDICATE_SCOPES)
        if self.scope != _PREDICATE_SCOPE[self.predicate]:
            raise _schema_error(
                f"scope: {self.scope!r} does not match predicate {self.predicate!r}")
        _req_enum(self.coverage_state, "coverage_state", COVERAGE_STATES)
        if self.coverage_state == COVERAGE_NO_CLAIMS_FOUND:
            raise _schema_error(
                "coverage_state: NO_CLAIMS_FOUND belongs to extraction, not evaluation")
        for name in ("checked_units", "total_units"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _schema_error(f"{name}: expected a non-negative int")
        if self.checked_units > self.total_units:
            raise _schema_error("checked_units may not exceed total_units")
        if not isinstance(self.violations, tuple):
            raise _schema_error("violations: expected tuple")
        if self.coverage_state not in _PREDICATE_MEASURED and self.violations:
            raise _schema_error(
                f"{self.predicate}: {self.coverage_state} may not carry violations")
        if len(self.violations) > MAX_VIOLATIONS:
            raise _bounds_error("violations: exceeds MAX_VIOLATIONS")
        if self.coverage_state == COVERAGE_CHECKED and not self.violations:
            raise _schema_error(
                "coverage_state CHECKED with zero violations must be "
                "NO_VIOLATIONS_FOUND")
        if self.coverage_state == COVERAGE_NO_VIOLATIONS_FOUND and self.violations:
            raise _schema_error(
                "coverage_state NO_VIOLATIONS_FOUND contradicts violations")
        for i, violation in enumerate(self.violations):
            if not isinstance(violation, ViolationV1):
                raise _schema_error(f"violations[{i}]: expected ViolationV1")
            if violation.predicate != self.predicate:
                raise _schema_error(
                    f"violations[{i}].predicate does not match result predicate")

    def to_canonical_obj(self) -> dict[str, Any]:
        return {
            "predicate": self.predicate, "scope": self.scope,
            "coverage_state": self.coverage_state,
            "checked_units": self.checked_units, "total_units": self.total_units,
            "violations": [v.to_canonical_obj() for v in self.violations],
        }


def _heading_text(block: bytes) -> str:
    """The heading line's text after `## `, NFC-normalized. Ephemeral, never reported."""
    try:
        first = block.decode("utf-8", errors="strict").split("\n", 1)[0]
    except UnicodeDecodeError:
        return ""
    return _norm_text(first[3:].strip()) if first.startswith("## ") else ""


def evaluate_structure(
    snapshot: FinalChapterSnapshotV1, canon: Optional[CanonLiteV1],
) -> PredicateResultV1:
    """Predicate #1 (§7.1) — chapter count / order / heading identity vs the OUTLINE.

    This is the only predicate evaluable from L1's structural canon, because chapter
    identity is code-owned: it comes from the accepted outline, not from anything the
    model invented (§6.1).
    """
    if canon is None:
        return PredicateResultV1(
            predicate=PREDICATE_STRUCTURE, scope=SCOPE_GLOBAL,
            coverage_state=COVERAGE_NO_CANON_AUTHORITY,
            checked_units=0, total_units=snapshot.chapter_count, violations=())

    expected = canon.chapters
    found = snapshot.blocks
    violations: list[ViolationV1] = []

    if len(found) != len(expected):
        violations.append(ViolationV1(
            code=VIOLATION_CHAPTER_COUNT, predicate=PREDICATE_STRUCTURE,
            chapter_index=-1))

    seen: dict[int, int] = {}
    for b in found:
        if b.heading_number < 0:
            continue
        if b.heading_number in seen:
            violations.append(ViolationV1(
                code=VIOLATION_CHAPTER_DUPLICATE, predicate=PREDICATE_STRUCTURE,
                chapter_index=b.index))
        else:
            seen[b.heading_number] = b.index

    for i, b in enumerate(found):
        if i >= len(expected):
            break
        exp = expected[i]
        identity_unknown = b.chapter_id == UNKNOWN or b.heading_number < 0
        if identity_unknown:
            violations.append(ViolationV1(
                code=VIOLATION_CHAPTER_IDENTITY_UNKNOWN,
                predicate=PREDICATE_STRUCTURE, chapter_index=i))
        elif b.chapter_id != exp.chapter_id:
            violations.append(ViolationV1(
                code=VIOLATION_CHAPTER_ORDER, predicate=PREDICATE_STRUCTURE,
                chapter_index=i))
        # Order: the block in position i must announce the chapter the outline put there.
        # An unparseable heading is UNKNOWN, not a mismatch — saying "wrong" about
        # something unread is a false positive. It is nevertheless INCOMPLETE identity,
        # so it receives its own bounded code and can never become a clean result.
        if b.heading_number >= 0 and exp.order >= 1 and b.heading_number != exp.order:
            violations.append(ViolationV1(
                code=VIOLATION_CHAPTER_ORDER, predicate=PREDICATE_STRUCTURE,
                chapter_index=i))
        # Title: deliberately CONSERVATIVE containment, not equality. A real heading is
        # decorated ("## Bab 3: Judul") while the outline stores the bare title, so an
        # equality test would flag nearly every chapter. Report-only in L2; §7.1 requires
        # adjudicated precision before this could ever gate anything, and that evidence
        # is L2b's job on organic traffic.
        if exp.expected_title != UNKNOWN:
            heading = _heading_text(snapshot.block_bytes(i)).casefold()
            if heading and _norm_text(exp.expected_title).casefold() not in heading:
                violations.append(ViolationV1(
                    code=VIOLATION_CHAPTER_TITLE, predicate=PREDICATE_STRUCTURE,
                    chapter_index=i))

    return PredicateResultV1(
        predicate=PREDICATE_STRUCTURE, scope=SCOPE_GLOBAL,
        coverage_state=(
            COVERAGE_CHECKED if violations else COVERAGE_NO_VIOLATIONS_FOUND),
        checked_units=len(found), total_units=max(len(found), len(expected)),
        violations=tuple(violations[:MAX_VIOLATIONS]),
    )


def evaluate_semantic(
    predicate: str,
    snapshot: FinalChapterSnapshotV1,
    canon: Optional[CanonLiteV1],
    claims_by_index: Mapping[int, ChapterClaimsV1],
) -> PredicateResultV1:
    """Predicates #2-#4 (§7.1). Report-only, and refused outright without authority.

    D-L2-5 / §11.3: extractor observations never become canon by being well-formed or
    repeated. With no accepted semantic authority there is nothing to contradict, so the
    only honest answer is `NO_CANON_AUTHORITY` — emphatically NOT "zero violations".
    """
    _req_enum(predicate, "predicate", SEMANTIC_PREDICATES)
    scope = _PREDICATE_SCOPE[predicate]
    total = snapshot.chapter_count

    if not has_predicate_authority(predicate, canon):
        return PredicateResultV1(
            predicate=predicate, scope=scope,
            coverage_state=COVERAGE_NO_CANON_AUTHORITY,
            checked_units=0, total_units=total, violations=())

    measured: list[ChapterClaimsV1] = []
    failure_states: list[str] = []
    for index, block in enumerate(snapshot.blocks):
        artifact = claims_by_index.get(index)
        if not isinstance(artifact, ChapterClaimsV1):
            failure_states.append(COVERAGE_INCOMPLETE_EXTRACTION)
            continue
        if artifact.chapter_index != index \
                or artifact.chapter_id != block.chapter_id \
                or artifact.content_sha256 != block.content_sha256 \
                or artifact.predicate_set_version != PREDICATE_SET_VERSION:
            failure_states.append(COVERAGE_INVALID_EXTRACTOR_OUTPUT)
            continue
        state = artifact.state_for(predicate)
        if state not in _EXTRACTION_MEASURED:
            failure_states.append(state)
            continue
        measured.append(artifact)
    if len(measured) < total:
        # Partial extraction is partial coverage. It is never a clean subset result.
        state = (
            failure_states[0]
            if failure_states and len(set(failure_states)) == 1
            else COVERAGE_INCOMPLETE_EXTRACTION
        )
        return PredicateResultV1(
            predicate=predicate, scope=scope,
            coverage_state=state,
            checked_units=len(measured), total_units=total, violations=())

    assert canon is not None  # narrowed by has_predicate_authority above
    violations: list[ViolationV1] = []
    claims = [
        (artifact.chapter_index, claim,
         snapshot.block_bytes(artifact.chapter_index)[
             claim.evidence_start:claim.evidence_end].decode("utf-8", errors="strict"))
        for artifact in measured
        for claim in artifact.claims
        if _CLAIM_PREDICATE[claim.claim_type] == predicate
    ]
    if predicate == PREDICATE_ENTITY_NAME:
        by_id = {entity.entity_id: entity for entity in canon.entities}
        for chapter_index, claim, evidence in claims:
            entity = by_id.get(claim.canon_ref)
            if entity is None:
                return PredicateResultV1(
                    predicate=predicate, scope=scope,
                    coverage_state=COVERAGE_UNKNOWN_CANON_REFERENCE,
                    checked_units=0, total_units=total, violations=())
            allowed = {
                _norm_text(name).strip().casefold()
                for name in (entity.canonical_name, *entity.aliases)
            }
            if _norm_text(evidence).strip().casefold() not in allowed:
                violations.append(ViolationV1(
                    code=VIOLATION_ENTITY_NAME, predicate=predicate,
                    chapter_index=chapter_index))
    elif predicate == PREDICATE_FIXED_LITERAL:
        by_id = {anchor.anchor_id: anchor for anchor in canon.anchors}
        for chapter_index, claim, evidence in claims:
            anchor = by_id.get(claim.canon_ref)
            if anchor is None:
                return PredicateResultV1(
                    predicate=predicate, scope=scope,
                    coverage_state=COVERAGE_UNKNOWN_CANON_REFERENCE,
                    checked_units=0, total_units=total, violations=())
            if _norm_text(evidence).strip() != _norm_text(anchor.literal).strip():
                violations.append(ViolationV1(
                    code=VIOLATION_FIXED_LITERAL, predicate=predicate,
                    chapter_index=chapter_index))
    else:
        accepted_events = {event.event_id for event in canon.one_time_events}
        first_seen: set[str] = set()
        for chapter_index, claim, _evidence in claims:
            if claim.canon_ref not in accepted_events:
                return PredicateResultV1(
                    predicate=predicate, scope=scope,
                    coverage_state=COVERAGE_UNKNOWN_CANON_REFERENCE,
                    checked_units=0, total_units=total, violations=())
            if claim.canon_ref in first_seen:
                violations.append(ViolationV1(
                    code=VIOLATION_ONE_TIME_EVENT, predicate=predicate,
                    chapter_index=chapter_index))
            else:
                first_seen.add(claim.canon_ref)

    return PredicateResultV1(
        predicate=predicate, scope=scope,
        coverage_state=(
            COVERAGE_CHECKED if violations else COVERAGE_NO_VIOLATIONS_FOUND),
        checked_units=len(measured), total_units=total,
        violations=tuple(violations[:MAX_VIOLATIONS]))


def evaluate_all(
    snapshot: FinalChapterSnapshotV1,
    canon: Optional[CanonLiteV1],
    claims_by_index: Optional[Mapping[int, ChapterClaimsV1]] = None,
) -> tuple[PredicateResultV1, ...]:
    claims = dict(claims_by_index or {})
    results = [evaluate_structure(snapshot, canon)]
    results.extend(evaluate_semantic(p, snapshot, canon, claims)
                   for p in SEMANTIC_PREDICATES)
    return tuple(results)


# ===========================================================================
# §6.4 ContinuityReportV1
# ===========================================================================

def decide_continuity_status(
    results: Sequence[PredicateResultV1],
    *,
    delivery_binding: str,
    persistence_binding: str,
) -> str:
    """C7 in code: absence of evidence can never become `clean`.

    Order matters. A mismatched binding outranks a clean predicate sweep, because a
    verdict bound to different bytes than the ones delivered is not a verdict about the
    delivered manuscript at all (C8/C9).
    """
    _req_enum(delivery_binding, "delivery_binding", BINDING_STATES)
    _req_enum(persistence_binding, "persistence_binding", BINDING_STATES)

    if delivery_binding == BINDING_MISMATCH or persistence_binding == BINDING_MISMATCH:
        return STATUS_UNRESOLVED
    if tuple(r.predicate for r in results) != ALL_PREDICATES:
        return STATUS_UNCHECKED
    if any(r.violations for r in results):
        return STATUS_VIOLATIONS
    if any(r.coverage_state not in _PREDICATE_MEASURED for r in results):
        return STATUS_UNCHECKED
    if delivery_binding != BINDING_MATCH:
        return STATUS_UNCHECKED
    # §11.2 — `clean` requires BOTH bindings settled. In L2 the persistence binding is
    # deliberately left UNPROVED (L2 never rewrites `narasi_chapters`), so this branch is
    # unreachable today BY DESIGN. That is the declared partial-C9 deviation made visible
    # in every report, instead of living only in a document nobody re-reads.
    if persistence_binding not in (BINDING_MATCH, BINDING_NOT_APPLICABLE):
        return STATUS_UNRESOLVED
    return STATUS_CLEAN


@dataclass(frozen=True, slots=True)
class ContinuityReportV1:
    """§6.4. Bounded values only — safe to log, persist, or ship as telemetry."""
    schema_version: str
    mode: str
    predicate_set_version: str
    materializer_version: str
    canon_sha256: str
    manuscript_sha256: str
    validated_final_sha256: str
    ordered_blocks_sha256: str
    ordered_chapter_hashes: tuple[str, ...]
    chapter_ids: tuple[str, ...]
    chapter_count: int
    repair_rounds: int
    affected_chapter_ids: tuple[str, ...]
    edit_ratios_ppm: tuple[int, ...]
    delivery_binding: str
    persistence_binding: str
    continuity_status: str
    results: tuple[PredicateResultV1, ...]

    def __post_init__(self) -> None:
        if self.schema_version != REPORT_SCHEMA_VERSION:
            raise _schema_error(f"schema_version: expected {REPORT_SCHEMA_VERSION!r}")
        _req_enum(self.mode, "mode", REPORT_MODES)
        if self.predicate_set_version != PREDICATE_SET_VERSION:
            raise _schema_error(
                f"predicate_set_version: expected {PREDICATE_SET_VERSION!r}")
        if self.materializer_version != MATERIALIZER_VERSION:
            raise _schema_error(
                f"materializer_version: expected {MATERIALIZER_VERSION!r}")
        if self.canon_sha256 != UNKNOWN:
            _req_sha256(self.canon_sha256, "canon_sha256")
        _req_sha256(self.manuscript_sha256, "manuscript_sha256")
        _req_sha256(self.validated_final_sha256, "validated_final_sha256")
        if self.validated_final_sha256 != self.manuscript_sha256:
            raise _schema_error(
                "validated_final_sha256 must equal the evaluated manuscript_sha256")
        _req_sha256(self.ordered_blocks_sha256, "ordered_blocks_sha256")
        if not isinstance(self.ordered_chapter_hashes, tuple):
            raise _schema_error("ordered_chapter_hashes: expected tuple")
        if not isinstance(self.chapter_ids, tuple):
            raise _schema_error("chapter_ids: expected tuple")
        if isinstance(self.chapter_count, bool) or not isinstance(
                self.chapter_count, int) or self.chapter_count < 0:
            raise _schema_error("chapter_count: expected a non-negative int")
        if len(self.ordered_chapter_hashes) != self.chapter_count \
                or len(self.chapter_ids) != self.chapter_count:
            raise _schema_error(
                "chapter_count must bind ordered_chapter_hashes and chapter_ids")
        for i, digest in enumerate(self.ordered_chapter_hashes):
            _req_sha256(digest, f"ordered_chapter_hashes[{i}]")
        for i, chapter_id in enumerate(self.chapter_ids):
            if chapter_id != UNKNOWN:
                _req_id(chapter_id, f"chapter_ids[{i}]")
        expected_ordered = _digest(
            "canon_lite_l2.ordered_blocks",
            [{"chapter_id": chapter_id, "content_sha256": digest}
             for chapter_id, digest in zip(
                 self.chapter_ids, self.ordered_chapter_hashes, strict=True)],
        )
        if expected_ordered != self.ordered_blocks_sha256:
            raise _schema_error(
                "ordered_blocks_sha256 does not bind chapter ids and hashes")
        if isinstance(self.repair_rounds, bool) \
                or not isinstance(self.repair_rounds, int) \
                or not (0 <= self.repair_rounds <= MAX_REPAIR_ROUNDS):
            raise _schema_error(
                f"repair_rounds: expected int in 0..{MAX_REPAIR_ROUNDS}")
        # 🔴 ONLY ASSIST MAY REPAIR — NOT "ANY MODE THAT IS NOT SHADOW".
        #    The field was locked to 0 for every mode while only L2 existed.
        #    L3-ASSIST needs it above 0, and the first narrowing let shadow out
        #    only, which quietly left `mode="enforce", repair_rounds=1` a valid
        #    report: a schema-level claim that a project which does not exist had
        #    performed repairs. An engine that refuses enforce is not enough —
        #    a report is written, read and audited far from the engine, and the
        #    schema is the last place that can say the claim is impossible.
        #    Allow-list, not deny-list: when L3-ENFORCE is eventually built, this
        #    line is where it has to be admitted deliberately.
        if self.repair_rounds != 0 and self.mode != "assist":
            raise _schema_error(
                f"repair_rounds: only assist may repair; {self.mode!r} may not")
        if not isinstance(self.affected_chapter_ids, tuple) \
                or not isinstance(self.edit_ratios_ppm, tuple):
            raise _schema_error("repair fields: expected tuples")
        if len(self.affected_chapter_ids) != len(self.edit_ratios_ppm):
            raise _schema_error("repair fields: chapter ids and edit ratios must align")
        if self.repair_rounds == 0 and (
                self.affected_chapter_ids or self.edit_ratios_ppm):
            raise _schema_error("repair fields: rounds=0 means nothing was repaired")
        for i, chapter_id in enumerate(self.affected_chapter_ids):
            _req_id(chapter_id, f"affected_chapter_ids[{i}]")
        for i, ratio in enumerate(self.edit_ratios_ppm):
            if isinstance(ratio, bool) or not isinstance(ratio, int) \
                    or not (0 <= ratio <= 1_000_000):
                raise _schema_error(f"edit_ratios_ppm[{i}]: expected int in 0..1000000")
        _req_enum(self.delivery_binding, "delivery_binding", BINDING_STATES)
        _req_enum(self.persistence_binding, "persistence_binding", BINDING_STATES)
        _req_enum(self.continuity_status, "continuity_status", CONTINUITY_STATUSES)
        if not isinstance(self.results, tuple):
            raise _schema_error("results: expected tuple")
        if tuple(r.predicate for r in self.results) != ALL_PREDICATES:
            raise _schema_error(
                "results: expected every predicate exactly once in canonical order")
        for i, result in enumerate(self.results):
            if not isinstance(result, PredicateResultV1):
                raise _schema_error(f"results[{i}]: expected PredicateResultV1")
            for violation in result.violations:
                if violation.chapter_index < -1 \
                        or violation.chapter_index >= self.chapter_count:
                    raise _schema_error(
                        f"results[{i}]: violation chapter index outside report")
        expected_status = decide_continuity_status(
            self.results, delivery_binding=self.delivery_binding,
            persistence_binding=self.persistence_binding)
        if self.continuity_status != expected_status:
            raise _schema_error(
                "continuity_status does not match results and binding states")

    def to_canonical_obj(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "predicate_set_version": self.predicate_set_version,
            "materializer_version": self.materializer_version,
            "canon_sha256": self.canon_sha256,
            "manuscript_sha256": self.manuscript_sha256,
            "validated_final_sha256": self.validated_final_sha256,
            "ordered_blocks_sha256": self.ordered_blocks_sha256,
            "ordered_chapter_hashes": list(self.ordered_chapter_hashes),
            "chapter_ids": list(self.chapter_ids),
            "chapter_count": self.chapter_count,
            "repair_rounds": self.repair_rounds,
            "affected_chapter_ids": list(self.affected_chapter_ids),
            "edit_ratios_ppm": list(self.edit_ratios_ppm),
            "delivery_binding": self.delivery_binding,
            "persistence_binding": self.persistence_binding,
            "continuity_status": self.continuity_status,
            "results": [r.to_canonical_obj() for r in self.results],
        }


def verify_delivery_binding(
    snapshot: FinalChapterSnapshotV1, result: Mapping[str, Any],
) -> str:
    """Compare the snapshot's bytes against what the delivery path still holds.

    Read-only by contract (§11.2): this COMPARES and reports. It never writes the snapshot
    bytes back into `result`, even when they match — substituting is L3's authority, and a
    byte-identity proof is not a licence to take it early.
    """
    key = resolve_manuscript_key(result)
    if key is None or key != snapshot.source_key:
        return BINDING_MISMATCH
    text = result.get(key)
    if not isinstance(text, str):
        return BINDING_MISMATCH
    return (BINDING_MATCH
            if sha256_hex(text.encode("utf-8")) == snapshot.manuscript_sha256
            else BINDING_MISMATCH)


def build_report(
    snapshot: FinalChapterSnapshotV1,
    canon: Optional[CanonLiteV1],
    *,
    mode: str,
    result: Mapping[str, Any],
    claims_by_index: Optional[Mapping[int, ChapterClaimsV1]] = None,
    repair_rounds: int = 0,
    affected_chapter_ids: tuple[str, ...] = (),
    edit_ratios_ppm: tuple[int, ...] = (),
) -> ContinuityReportV1:
    if not isinstance(snapshot, FinalChapterSnapshotV1):
        raise _schema_error("snapshot: expected FinalChapterSnapshotV1")
    if canon is not None and not isinstance(canon, _cl.CanonLiteV1):
        raise _schema_error("canon: expected CanonLiteV1 or None")
    if not isinstance(result, Mapping):
        raise _schema_error("result: expected a mapping")
    if claims_by_index is not None and not isinstance(claims_by_index, Mapping):
        raise _schema_error("claims_by_index: expected a mapping or None")
    results = evaluate_all(snapshot, canon, claims_by_index)
    delivery = verify_delivery_binding(snapshot, result)
    # L2 never proves the durable-row binding, and says so rather than implying success.
    persistence = BINDING_UNPROVED
    return ContinuityReportV1(
        schema_version=REPORT_SCHEMA_VERSION,
        mode=_req_enum(mode, "mode", REPORT_MODES),
        predicate_set_version=PREDICATE_SET_VERSION,
        materializer_version=MATERIALIZER_VERSION,
        canon_sha256=(canon.canon_sha256 if canon is not None else UNKNOWN),
        manuscript_sha256=snapshot.manuscript_sha256,
        validated_final_sha256=snapshot.manuscript_sha256,
        ordered_blocks_sha256=snapshot.ordered_blocks_sha256,
        ordered_chapter_hashes=tuple(b.content_sha256 for b in snapshot.blocks),
        chapter_ids=tuple(b.chapter_id for b in snapshot.blocks),
        chapter_count=snapshot.chapter_count,
        repair_rounds=repair_rounds,
        affected_chapter_ids=affected_chapter_ids,
        edit_ratios_ppm=edit_ratios_ppm,
        delivery_binding=delivery,
        persistence_binding=persistence,
        continuity_status=decide_continuity_status(
            results, delivery_binding=delivery, persistence_binding=persistence),
        results=results,
    )


def report_telemetry(
    report: Optional[ContinuityReportV1], *, l2_status: str, mode: str,
) -> dict[str, Any]:
    """The ONLY observable projection of an L2 report (§10, C12).

    Carries hashes, bounded codes, counts and their denominators. Never a manuscript byte,
    heading, title, provider error, prompt, or model response.
    """
    status = _req_enum(l2_status, "l2_status",
                       ("present", "absent", "invalid", "skipped"))
    checked_mode = _req_enum(mode, "mode", REPORT_MODES)
    if report is None:
        if status == "present":
            raise _schema_error("l2_status: present requires a report")
        return {
            "l2_status": status, "schema_version": REPORT_SCHEMA_VERSION,
            "mode": checked_mode,
            "predicate_set_version": PREDICATE_SET_VERSION,
            "materializer_version": MATERIALIZER_VERSION,
            "manuscript_sha256": UNKNOWN, "validated_final_sha256": UNKNOWN,
            "chapter_count": None,
            "delivery_binding": BINDING_NOT_APPLICABLE,
            "persistence_binding": BINDING_NOT_APPLICABLE,
            "continuity_status": STATUS_UNCHECKED,
            "coverage": {},
        }
    if status != "present":
        raise _schema_error(f"l2_status: {status} requires report=None")
    if report.mode != checked_mode:
        raise _schema_error("mode: does not match report.mode")
    return {
        "l2_status": status,
        "schema_version": report.schema_version,
        "mode": report.mode,
        "predicate_set_version": report.predicate_set_version,
        "materializer_version": report.materializer_version,
        "manuscript_sha256": report.manuscript_sha256,
        "validated_final_sha256": report.validated_final_sha256,
        "chapter_count": report.chapter_count,
        "delivery_binding": report.delivery_binding,
        "persistence_binding": report.persistence_binding,
        "continuity_status": report.continuity_status,
        # Denominator ALWAYS travels with the numerator — a bare count of 0 would be
        # unreadable under the zero rule.
        "coverage": {r.predicate: {"state": r.coverage_state,
                                   "checked": r.checked_units,
                                   "total": r.total_units,
                                   "violations": len(r.violations)}
                     for r in report.results},
    }


def report_digest(report: ContinuityReportV1) -> str:
    """Domain-separated hash of the report's canonical form."""
    return _digest("canon_lite_l2.continuity_report", report.to_canonical_obj())
