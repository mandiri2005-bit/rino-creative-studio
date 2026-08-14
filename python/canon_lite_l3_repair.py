"""L3-ASSIST Stage 2 — TARGETED REPAIR.

Stage 1 put one canon in front of every chapter worker and proved every delivered
chapter was written against it. That prevents the defects it can prevent; it does
not fix the ones that still get through. Stage 2 is the repair half: take the
continuity report L2 already produces, fix the chapters it names, and re-check.

THE ACCEPTANCE RULE, WHICH IS THE WHOLE MODULE
----------------------------------------------
A candidate is STAGED, never applied on arrival. It is accepted only when all
three of these hold on the re-evaluated manuscript:

  1. every targeted violation for that chapter is GONE;
  2. no NEW violation appeared — anywhere in the book, not just in that chapter;
  3. coverage did not REGRESS — no predicate stopped being measurable.

Any one of them failing rejects the candidate whole and moves to the next
attempt. After two attempts the original bytes stand and the chapter's outcome is
`unresolved`.

Each of the three exists because the other two cannot see its failure. (1) alone
accepts a rewrite that fixes chapter 7 and contradicts chapter 3. (2) alone
accepts a candidate that changed nothing relevant — no new violations, and the
old one still there. (3) alone is what catches the quiet one: a candidate that
makes a predicate *unmeasurable* removes the violation from the report without
removing it from the book, and reads as a clean fix in every count that only
looks at violation totals.

WHAT THIS MODULE WILL NOT DO, AND WHY EACH ONE MATTERS
------------------------------------------------------

* **It never blocks delivery, and it refuses to run under `enforce`.**
  `continuity_unresolved` is an OUTCOME, not a refusal. Gating delivery on it is
  L3-ENFORCE, a separate deferred project — and the single easiest thing to
  import here by accident, because everything needed to block is sitting right
  there once the re-check has run. `mode="enforce"` raises rather than quietly
  behaving like assist.

* **It never repairs a structure violation.** `chapter_count_mismatch`,
  `chapter_order_mismatch`, `chapter_title_mismatch`, `chapter_number_duplicated`
  and `chapter_identity_unreadable` say the manuscript does not match the
  accepted outline. That is an assembly defect. Rewriting prose until the symptom
  disappears would hide it while leaving the book structurally wrong, and the
  report would go from "we know the structure is broken" to "clean".

* **It never rewrites more than the guard allows.** A provider asked to fix one
  contradicted name can return a rewritten chapter. That is not a repair, it is a
  regeneration wearing a repair's name. The candidate is REJECTED whole, never
  trimmed toward the boundary.

* **It never keeps a partially applied repair.** A candidate lands whole or the
  original bytes stand.

* **It never trusts a repaired chapter's old claims, nor an extractor's word for
  which chapter it read.** `ChapterClaimsV1` is bound to its chapter index, id,
  content hash and canon hash; all four are checked against what was actually
  staged. An extractor that answers about a different chapter, or about a
  different canon, is answering a question nobody asked.

* **It re-checks EVERYTHING, not just what it touched.** Re-extraction is
  targeted — a full re-extraction per attempt would multiply the provider bill by
  the number of attempts. Re-EVALUATION is total: chapter, adjacent-pair and
  global predicates all run again over the rebuilt manuscript, because a local
  fix can create a distant defect.

BYTE DISCIPLINE
---------------
The repaired manuscript is rebuilt from the snapshot's own byte ranges and
re-materialized from scratch, so the final report's `manuscript_sha256` hashes
bytes that actually exist. Nothing is carried forward. The caller must then
substitute those exact bytes and call `verify_applied()` — the Stage-2 analogue
of Stage 1's census, for the same reason: every hash in here describes what this
module produced, and none of them says anything about what was delivered.
"""

from __future__ import annotations

import unicodedata
import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Awaitable, Mapping, Optional, Protocol, Sequence

import canon_lite as _cl
import canon_lite_l2 as _l2

_LOG = logging.getLogger("canon_lite_l3_repair")

__all__ = [
    "REPAIR_SCHEMA_VERSION",
    "MAX_REPAIR_ITERATIONS",
    "DEFAULT_MAX_EDIT_RATIO_PPM",
    "REPAIRABLE_PREDICATES",
    "REPAIR_MODES",
    "OUTCOMES",
    "OUTCOME_NOTHING_TO_REPAIR",
    "OUTCOME_REPAIRED",
    "OUTCOME_UNRESOLVED",
    "REASONS",
    "REASON_NONE",
    "REASON_INEFFECTIVE",
    "REASON_REGRESSED",
    "REASON_COVERAGE_REGRESSED",
    "REASON_GUARD_REJECTED",
    "REASON_PROVIDER_FAILED",
    "REASON_REEXTRACTION_FAILED",
    "REASON_INVALID_CANDIDATE",
    "REASON_EXTRACTOR_IDENTITY",
    "RepairError",
    "ChapterRepairV1",
    "RepairRunV1",
    "edit_ratio_ppm",
    "exceeds_edit_guard",
    "repairable_targets",
    "repair_manuscript",
    "verify_applied",
    "run_telemetry",
]

REPAIR_SCHEMA_VERSION = "canon_lite_l3_repair_v1"

#: §Stage-2 contract: **max two internal repair iterations per chapter**. A hard
#: ceiling, not a default — there is no parameter to raise it. IMPORTED, never
#: redefined: `ContinuityReportV1` bounds the same number, and a second copy here
#: would be a second ceiling that drifts the moment one of them moves.
MAX_REPAIR_ITERATIONS = _l2.MAX_REPAIR_ROUNDS

#: The minimal-edit boundary, in parts per million of the chapter's NFC-normalized
#: CHARACTER count. A candidate strictly above it is rejected; a candidate exactly
#: at it is allowed. 15% is generous for a genuine targeted fix (a name, a number,
#: a deleted sentence) and far below a rewrite.
DEFAULT_MAX_EDIT_RATIO_PPM = 150_000

#: Structure is excluded on purpose — see the module docstring.
REPAIRABLE_PREDICATES = frozenset(_l2.SEMANTIC_PREDICATES)

#: Repair belongs to assist alone. Shadow observes and may never change a byte the
#: reader receives; enforce is a separate deferred project and is REFUSED, not
#: silently treated as assist.
REPAIR_MODES = ("assist",)

OUTCOME_NOTHING_TO_REPAIR = "nothing_to_repair"
OUTCOME_REPAIRED = "repaired"
OUTCOME_UNRESOLVED = "unresolved"
OUTCOMES = (OUTCOME_NOTHING_TO_REPAIR, OUTCOME_REPAIRED, OUTCOME_UNRESOLVED)

#: Why the last attempt failed. Bounded labels: this is telemetry, and a provider
#: message or a manuscript span may never reach it (C12/§10).
REASON_NONE = "none"
REASON_INEFFECTIVE = "ineffective"
REASON_REGRESSED = "introduced_new_violation"
REASON_COVERAGE_REGRESSED = "coverage_regressed"
REASON_GUARD_REJECTED = "guard_rejected"
#: The REPAIR CANDIDATE call raised. Rare by construction: `RepairProvider`
#: implementations are contracted to DECLINE (return None) rather than raise on an
#: ordinary provider fault — see `L3AssistSession.repair_provider`'s own docstring —
#: so a raise reaching here means something outside that contract (budget
#: accounting, a lazy import, worker construction) failed, not a generation miss.
REASON_PROVIDER_FAILED = "provider_failed"
#: 🔴 SPLIT FROM `REASON_PROVIDER_FAILED` — THEY MEANT DIFFERENT FAILURES AND SHARED ONE
#:    WORD. Live canary `19444d6` showed `unresolved_reasons: {'provider_failed': 3}` on
#:    all three chapters with the REPAIR call never having failed (its own
#:    `l3_repair_generation_error` log line never fired) — the actual failure was in
#:    RE-EXTRACTING the candidate's claims to verify the repair, a call this engine
#:    treats as a fault (`break`, no retry) exactly like a generation fault, but which is
#:    a different subsystem (the QC extraction path) with its own bounded reason
#:    vocabulary logged at `L3AssistSession.extract_chapter`
#:    (`error_code=l3_repair_reextraction_error`). An operator reading ONE coarse word
#:    could not tell "the writer failed" from "the checker failed" — now they can.
REASON_REEXTRACTION_FAILED = "reextraction_failed"
REASON_INVALID_CANDIDATE = "invalid_candidate"
REASON_EXTRACTOR_IDENTITY = "extractor_identity_mismatch"
REASONS = (
    REASON_NONE, REASON_INEFFECTIVE, REASON_REGRESSED, REASON_COVERAGE_REGRESSED,
    REASON_GUARD_REJECTED, REASON_PROVIDER_FAILED, REASON_REEXTRACTION_FAILED,
    REASON_INVALID_CANDIDATE, REASON_EXTRACTOR_IDENTITY,
)


class RepairError(_l2.CanonLiteL2Error):
    """Raised for a broken caller contract, never for a bad repair candidate."""


def _schema_error(msg: str) -> Exception:
    return RepairError(msg)


class RepairProvider(Protocol):
    """Returns replacement bytes for ONE chapter, or None to decline."""

    def __call__(self, *, chapter_index: int, chapter_id: str, block_bytes: bytes,
                 violation_codes: tuple[str, ...],
                 canon: Optional[_cl.CanonLiteV1],
                 attempt: int) -> Awaitable[Optional[bytes]]: ...


class ChapterExtractor(Protocol):
    """Re-extracts claims for ONE chapter's exact bytes.

    Deliberately per-chapter: the engine is structurally incapable of asking for a
    full re-extraction, so "re-extract only what changed" is enforced by the shape
    of the dependency rather than by remembering to pass the right argument.
    """

    def __call__(self, *, chapter_index: int, chapter_id: str, block_bytes: bytes,
                 canon: Optional[_cl.CanonLiteV1],
                 attempt: int) -> Awaitable[_l2.ChapterClaimsV1]: ...


# ===========================================================================
# Measurement
# ===========================================================================

def _nfc(data: bytes) -> str:
    return unicodedata.normalize("NFC", data.decode("utf-8", errors="strict"))


def edit_ratio_ppm(before: bytes, after: bytes) -> int:
    """How much of the chapter changed, in ppm of NFC UNICODE CHARACTERS.

    🔴 CHARACTERS, NOT BYTES, AND THE DIFFERENCE IS NOT COSMETIC. In UTF-8 an
       Indonesian or Javanese passage costs 1 byte for ASCII and 2–4 for anything
       accented, so a byte ratio charges a repair more for editing "menaklukkan
       Ratu Kalinyamat" than the same-length edit in plain ASCII. The guard would
       then be tighter for exactly the prose this product is written in. NFC also
       means a pure normalization difference measures as zero: recomposing "é" is
       not an edit a reader can see.

    The denominator is the larger side, so deleting most of a chapter scores as a
    large edit rather than a small one.
    """
    if not isinstance(before, bytes) or not isinstance(after, bytes):
        raise _schema_error("edit_ratio_ppm: expected bytes")
    try:
        a, b = _nfc(before), _nfc(after)
    except UnicodeDecodeError as exc:
        raise _schema_error("edit_ratio_ppm: candidate is not valid UTF-8") from exc
    if a == b:
        return 0
    span = max(len(a), len(b))
    if span == 0:
        return 0
    matched = sum(block.size for block in
                  SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks())
    changed = max(0, span - matched)
    return min(1_000_000, round(changed * 1_000_000 / span))


def exceeds_edit_guard(ratio_ppm: int, max_ppm: int) -> bool:
    """The boundary, as one comparison with one home.

    STRICTLY greater. A candidate exactly at the limit is inside it — `>=` would
    make the documented boundary off by one, and that is the kind of error that
    only ever shows up as "the guard is a bit tighter than the docs say".
    """
    return int(ratio_ppm) > int(max_ppm)


def repairable_targets(
    report: _l2.ContinuityReportV1,
) -> dict[int, tuple[str, ...]]:
    """`{chapter_index: (violation_code, ...)}` for what repair is allowed to touch.

    Two filters, both load-bearing. A violation with `chapter_index == -1` is
    document-global and names no chapter to rewrite. A structure violation names
    one but must not be rewritten — including `chapter_title_mismatch`, which is
    the tempting one precisely because it looks local and looks easy.
    """
    if not isinstance(report, _l2.ContinuityReportV1):
        raise _schema_error("report: expected ContinuityReportV1")
    targets: dict[int, list[str]] = {}
    for result in report.results:
        if result.predicate not in REPAIRABLE_PREDICATES:
            continue
        for violation in result.violations:
            if violation.chapter_index < 0:
                continue
            targets.setdefault(violation.chapter_index, []).append(violation.code)
    return {index: tuple(sorted(codes)) for index, codes in sorted(targets.items())}


def _violation_set(report: _l2.ContinuityReportV1) -> frozenset[tuple[str, str, int]]:
    return frozenset(
        (v.code, v.predicate, v.chapter_index)
        for r in report.results for v in r.violations)


def _coverage_regressed(before: _l2.ContinuityReportV1,
                        after: _l2.ContinuityReportV1) -> bool:
    """Did any predicate become LESS measured than it already was?

    🔴 THE FAILURE THAT LOOKS LIKE A FIX. A candidate that makes a predicate
       unmeasurable — mangling the text the extractor keys on, say — removes the
       violation from the report without removing it from the book. Violation
       counts go down, and nothing else notices.
    """
    prior = {r.predicate: r for r in before.results}
    for result in after.results:
        was = prior.get(result.predicate)
        if was is None:
            continue
        if was.coverage_state in _l2._PREDICATE_MEASURED \
                and result.coverage_state not in _l2._PREDICATE_MEASURED:
            return True
        if result.checked_units < was.checked_units:
            return True
    return False


# ===========================================================================
# Evidence
# ===========================================================================

@dataclass(frozen=True, slots=True)
class ChapterRepairV1:
    """What happened to ONE chapter. Bounded labels, hashes and counts only."""
    chapter_index: int
    chapter_id: str
    attempted_codes: tuple[str, ...]
    iterations: int
    outcome: str
    last_reason: str
    accepted: bool
    edit_ratio_ppm: int
    before_sha256: str
    after_sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.chapter_index, bool) or not isinstance(
                self.chapter_index, int) or self.chapter_index < 0:
            raise _schema_error("chapter_index: expected a non-negative int")
        if self.chapter_id != _cl.UNKNOWN:
            _cl._req_id(self.chapter_id, "chapter_id")
        for code in self.attempted_codes:
            _cl._req_enum(code, "attempted_codes", _l2.VIOLATION_CODES)
        if isinstance(self.iterations, bool) or not isinstance(self.iterations, int) \
                or not (0 <= self.iterations <= MAX_REPAIR_ITERATIONS):
            raise _schema_error(
                f"iterations: expected int in 0..{MAX_REPAIR_ITERATIONS}")
        _cl._req_enum(self.outcome, "outcome", OUTCOMES)
        _cl._req_enum(self.last_reason, "last_reason", REASONS)
        if not isinstance(self.accepted, bool):
            raise _schema_error("accepted: expected bool")
        if self.accepted != (self.outcome == OUTCOME_REPAIRED):
            raise _schema_error("accepted must hold exactly when outcome is repaired")
        if self.accepted and self.last_reason != REASON_NONE:
            raise _schema_error("an accepted repair has no failure reason")
        if self.outcome == OUTCOME_UNRESOLVED and self.last_reason == REASON_NONE:
            raise _schema_error("an unresolved chapter must say why")
        if isinstance(self.edit_ratio_ppm, bool) or not isinstance(
                self.edit_ratio_ppm, int) or not (0 <= self.edit_ratio_ppm <= 1_000_000):
            raise _schema_error("edit_ratio_ppm: expected int in 0..1000000")
        _cl._req_sha256(self.before_sha256, "before_sha256")
        _cl._req_sha256(self.after_sha256, "after_sha256")
        if not self.accepted:
            if self.after_sha256 != self.before_sha256:
                raise _schema_error(
                    "a rejected repair must leave the chapter's bytes exactly as they were")
            if self.edit_ratio_ppm != 0:
                raise _schema_error(
                    "a rejected repair edited nothing, so its ratio must be 0")
        if self.accepted and self.after_sha256 == self.before_sha256:
            raise _schema_error(
                "an accepted repair that changed no bytes is not a repair")

    def to_canonical_obj(self) -> dict[str, Any]:
        return {
            "chapter_index": self.chapter_index, "chapter_id": self.chapter_id,
            "attempted_codes": list(self.attempted_codes),
            "iterations": self.iterations, "outcome": self.outcome,
            "last_reason": self.last_reason, "accepted": self.accepted,
            "edit_ratio_ppm": self.edit_ratio_ppm,
            "before_sha256": self.before_sha256, "after_sha256": self.after_sha256,
        }


@dataclass(frozen=True, slots=True)
class RepairRunV1:
    """The whole repair pass. `report` is the RE-CHECK, never the report we began from."""
    schema_version: str
    mode: str
    rounds: int
    chapters: tuple[ChapterRepairV1, ...]
    manuscript_sha256_before: str
    manuscript_sha256_after: str
    repaired_manuscript: bytes
    report: _l2.ContinuityReportV1

    def __post_init__(self) -> None:
        if self.schema_version != REPAIR_SCHEMA_VERSION:
            raise _schema_error(f"schema_version: expected {REPAIR_SCHEMA_VERSION!r}")
        _cl._req_enum(self.mode, "mode", REPAIR_MODES)
        if isinstance(self.rounds, bool) or not isinstance(self.rounds, int) \
                or not (0 <= self.rounds <= MAX_REPAIR_ITERATIONS):
            raise _schema_error(f"rounds: expected int in 0..{MAX_REPAIR_ITERATIONS}")
        if not isinstance(self.chapters, tuple):
            raise _schema_error("chapters: expected tuple")
        seen = [c.chapter_index for c in self.chapters]
        if seen != sorted(set(seen)):
            raise _schema_error("chapters: expected unique, ascending chapter indices")
        expected_rounds = max((c.iterations for c in self.chapters), default=0)
        if self.rounds != expected_rounds:
            raise _schema_error(
                "rounds must be the highest iteration count any chapter used")
        _cl._req_sha256(self.manuscript_sha256_before, "manuscript_sha256_before")
        _cl._req_sha256(self.manuscript_sha256_after, "manuscript_sha256_after")
        if not isinstance(self.repaired_manuscript, bytes):
            raise _schema_error("repaired_manuscript: expected bytes")
        # 🔴 THE HASH IS OF BYTES THAT EXIST. Carrying a hash computed earlier would
        #    let the manuscript and its identity drift apart silently.
        if _cl.sha256_hex(self.repaired_manuscript) != self.manuscript_sha256_after:
            raise _schema_error(
                "manuscript_sha256_after does not hash the repaired manuscript")
        if not any(c.accepted for c in self.chapters) \
                and self.manuscript_sha256_after != self.manuscript_sha256_before:
            raise _schema_error(
                "the manuscript changed although no chapter repair was accepted")
        if not isinstance(self.report, _l2.ContinuityReportV1):
            raise _schema_error("report: expected ContinuityReportV1")
        if self.report.manuscript_sha256 != self.manuscript_sha256_after:
            raise _schema_error(
                "the re-check report is not bound to the repaired manuscript")
        if self.report.repair_rounds != self.rounds:
            raise _schema_error("report.repair_rounds disagrees with the run")

    @property
    def continuity_status(self) -> str:
        return self.report.continuity_status

    @property
    def changed(self) -> bool:
        return self.manuscript_sha256_after != self.manuscript_sha256_before

    def to_canonical_obj(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "mode": self.mode,
            "rounds": self.rounds,
            "chapters": [c.to_canonical_obj() for c in self.chapters],
            "manuscript_sha256_before": self.manuscript_sha256_before,
            "manuscript_sha256_after": self.manuscript_sha256_after,
            "report": self.report.to_canonical_obj(),
        }


# ===========================================================================
# Candidate validation
# ===========================================================================

def _heading_line(data: bytes) -> Optional[str]:
    try:
        first = data.decode("utf-8", errors="strict").split("\n", 1)[0]
    except UnicodeDecodeError:
        return None
    return unicodedata.normalize("NFC", first).strip() if first.startswith("## ") else None


_INVALID_CANDIDATE_STAGES = frozenset({
    "pre.type",
    "pre.empty",
    "pre.utf8",
    "pre.heading",
    "post.materialize",
    "post.block_count",
    "post.chapter_identity",
})


def _validate_candidate_detailed(original: bytes, candidate: Any) -> tuple[Optional[bytes], Optional[str]]:
    """Return candidate plus the one-to-one pre-validation branch code."""
    if candidate is None or not isinstance(candidate, (bytes, bytearray)):
        return None, "pre.type"
    data = bytes(candidate)
    if not data.strip():
        return None, "pre.empty"
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, "pre.utf8"
    before_heading = _heading_line(original)
    if before_heading is not None and _heading_line(data) != before_heading:
        return None, "pre.heading"
    return data, None


def _validate_candidate(original: bytes, candidate: Any) -> Optional[bytes]:
    """`bytes` when the candidate is a usable chapter, `None` when it is not.

    The heading must survive verbatim — a repair that renames or drops a chapter
    heading is a STRUCTURAL edit arriving through the semantic door, and it would
    move the very block identity the whole report is indexed by.
    """
    data, _stage = _validate_candidate_detailed(original, candidate)
    return data


def _log_invalid_candidate(stage: str, *, chapter_index: int, attempt: int,
                           original: bytes, candidate: Any) -> None:
    """Bounded observability only: no prose, raw exception, hash, or request id."""
    output_bytes = len(candidate) if isinstance(candidate, (bytes, bytearray)) else 0
    _LOG.warning(
        "l3 invalid candidate stage=%s chapter_index=%d attempt=%d input_bytes=%d output_bytes=%d",
        stage, chapter_index, attempt, len(original), output_bytes)


def _binding_ok(fresh: Any, *, chapter_index: int, chapter_id: str,
                candidate: bytes, canon: Optional[_cl.CanonLiteV1]) -> bool:
    """Do the fresh claims describe THIS chapter, THESE bytes, under THIS canon?

    🔴 ALL FOUR, NOT JUST THE CONTENT HASH. An extractor that answers with the
       right bytes under the wrong index or the wrong canon has answered a
       question nobody asked, and its verdict would then be spliced in as though
       it were about the chapter being repaired. `ChapterClaimsV1` carries all
       four precisely so none of them has to be assumed.
    """
    if not isinstance(fresh, _l2.ChapterClaimsV1):
        return False
    if fresh.chapter_index != chapter_index:
        return False
    if fresh.chapter_id != chapter_id:
        return False
    if fresh.content_sha256 != _cl.sha256_hex(candidate):
        return False
    expected_canon = canon.canon_sha256 if canon is not None else _cl.UNKNOWN
    return fresh.canon_sha256 == expected_canon


def _rebuild(snapshot: _l2.FinalChapterSnapshotV1,
             blocks: Sequence[bytes]) -> bytes:
    """Prefix + the (possibly repaired) blocks, in order. No normalization, ever."""
    if len(blocks) != len(snapshot.blocks):
        raise _schema_error("rebuild: block count changed")
    return snapshot.manuscript_bytes[:snapshot.prefix_byte_end] + b"".join(blocks)


# ===========================================================================
# The repair pass
# ===========================================================================

async def repair_manuscript(
    snapshot: _l2.FinalChapterSnapshotV1,
    canon: Optional[_cl.CanonLiteV1],
    *,
    mode: str,
    result: Mapping[str, Any],
    claims_by_index: Optional[Mapping[int, _l2.ChapterClaimsV1]],
    repair_provider: RepairProvider,
    extract_chapter: ChapterExtractor,
    max_edit_ratio_ppm: int = DEFAULT_MAX_EDIT_RATIO_PPM,
) -> RepairRunV1:
    """Repair the chapters the continuity report names, then re-check the whole book.

    Never raises for a failed repair — a provider that errors, declines, returns
    nonsense, overshoots the guard, fixes nothing or breaks something else is a
    REJECTED candidate and a reported outcome. `RepairError` is reserved for the
    caller breaking the contract, `mode="enforce"` among them.
    """
    if not isinstance(snapshot, _l2.FinalChapterSnapshotV1):
        raise _schema_error("snapshot: expected FinalChapterSnapshotV1")
    # Refused explicitly, and before anything else: an enforce caller must find out
    # that this engine has no enforce behaviour, not receive assist behaviour under
    # an enforce label.
    if mode == "enforce":
        raise _schema_error(
            "mode=enforce: L3-ENFORCE is a separate deferred project; "
            "this engine implements assist only")
    if mode not in REPAIR_MODES:
        # Raised as a RepairError like every other contract breach here, rather
        # than delegated to `_req_enum`: a caller catching this module's errors
        # should not have to know that one refusal arrives from a different tree.
        raise _schema_error(f"mode: {mode!r} not in {REPAIR_MODES}")
    if not callable(repair_provider) or not callable(extract_chapter):
        raise _schema_error("repair_provider and extract_chapter must be callables")
    if isinstance(max_edit_ratio_ppm, bool) or not isinstance(max_edit_ratio_ppm, int) \
            or not (0 < max_edit_ratio_ppm <= 1_000_000):
        raise _schema_error("max_edit_ratio_ppm: expected int in 1..1000000")

    claims: dict[int, _l2.ChapterClaimsV1] = dict(claims_by_index or {})
    before_sha = snapshot.manuscript_sha256
    source_key = snapshot.source_key

    # ── THE STATE EVERY DECISION IS MADE AGAINST ─────────────────────────────
    #
    # 🔴 THE BOOK MOVES WHILE WE REPAIR IT, AND EVERY JUDGEMENT HAS TO MOVE WITH
    #    IT. Freezing the target list and the comparison baseline at the first
    #    report is not conservative, it is wrong in both directions:
    #
    #      * a repair to chapter A can REMOVE chapter B's violation — B's
    #        one-time event stops being a duplicate the moment A's telling goes.
    #        Against a frozen target list the provider is still paid to "repair" B,
    #        and against a frozen violation set any edit it returns clears
    #        condition (1) for free, so an arbitrary rewrite lands as a repair.
    #      * a repair to chapter A can also FIX something chapter B then breaks
    #        again. Against a frozen baseline that re-breakage is not "new" — it
    #        was in the original report — so B's candidate is accepted and the
    #        book ends up back where it started, with two repairs recorded.
    #
    #    So the current report, its violation set and its coverage are carried
    #    forward, and every acceptance promotes ALL of the state at once.
    current_blocks = [snapshot.manuscript_bytes[b.byte_start:b.byte_end]
                      for b in snapshot.blocks]
    current_claims = dict(claims)
    current_snapshot = snapshot
    current_result: Mapping[str, Any] = dict(result)
    current_report = _l2.build_report(snapshot, canon, mode=mode, result=result,
                                      claims_by_index=current_claims)

    # The chapters this pass MAY touch, fixed here so the pass is finite and cannot
    # chase violations it creates. Whether each one still NEEDS touching is decided
    # per chapter, from the current report, immediately before the provider call.
    candidate_chapters = sorted(repairable_targets(current_report))
    records: list[ChapterRepairV1] = []

    for index in candidate_chapters:
        if not (0 <= index < len(current_blocks)):
            raise _schema_error("report names a chapter outside the snapshot")
        block = current_snapshot.blocks[index]
        original = current_blocks[index]

        # Re-read the live targets. An earlier repair may already have settled this
        # chapter, and paying a provider to fix a violation that no longer exists
        # is the cheapest of the two failures it causes.
        codes = repairable_targets(current_report).get(index, ())
        if not codes:
            records.append(ChapterRepairV1(
                chapter_index=index, chapter_id=block.chapter_id,
                attempted_codes=(), iterations=0,
                outcome=OUTCOME_NOTHING_TO_REPAIR, last_reason=REASON_NONE,
                accepted=False, edit_ratio_ppm=0,
                before_sha256=block.content_sha256,
                after_sha256=block.content_sha256))
            continue

        current_violations = _violation_set(current_report)
        # Did the caller materialize with a canon? Read it off the snapshot rather
        # than assuming either way; see the note at the staged materialization.
        _ids_resolved = any(b.chapter_id != _cl.UNKNOWN
                            for b in current_snapshot.blocks)
        iterations = 0
        reason = REASON_NONE
        accepted_bytes: Optional[bytes] = None
        accepted_claims: Optional[_l2.ChapterClaimsV1] = None
        accepted_ratio = 0
        promoted: Optional[tuple] = None

        # 🔴 THE CEILING IS THE LOOP BOUND ITSELF, not a counter checked inside the
        #    body. A cap enforced by an `if` is one early `continue` away from being
        #    skipped, and this loop has several.
        for attempt in range(1, MAX_REPAIR_ITERATIONS + 1):
            iterations = attempt
            try:
                raw = await repair_provider(
                    chapter_index=index, chapter_id=block.chapter_id,
                    block_bytes=original, violation_codes=codes,
                    canon=canon, attempt=attempt)
            except Exception:  # noqa: BLE001 - a provider fault is a rejected repair
                reason = REASON_PROVIDER_FAILED
                break

            candidate, invalid_stage = _validate_candidate_detailed(original, raw)
            if candidate is None:
                reason = REASON_INVALID_CANDIDATE
                _log_invalid_candidate(
                    invalid_stage or "pre.type", chapter_index=index, attempt=attempt,
                    original=original, candidate=raw)
                continue
            if candidate == original:
                reason = REASON_INEFFECTIVE
                continue

            ratio = edit_ratio_ppm(original, candidate)
            if exceeds_edit_guard(ratio, max_edit_ratio_ppm):
                # Rejected WHOLE. Trimming toward the boundary would leave the
                # reader with something that is neither the chapter nor the repair.
                reason = REASON_GUARD_REJECTED
                continue

            # The candidate's OWN claims — the old ones describe bytes that no
            # longer exist.
            try:
                fresh = await extract_chapter(
                    chapter_index=index, chapter_id=block.chapter_id,
                    block_bytes=candidate, canon=canon, attempt=attempt)
            except Exception:  # noqa: BLE001 - the adapter already logged a bounded
                # code for WHY (error_code=l3_repair_reextraction_error); this engine
                # stays provider-agnostic and records only WHICH phase failed.
                reason = REASON_REEXTRACTION_FAILED
                break
            if not _binding_ok(fresh, chapter_index=index,
                               chapter_id=block.chapter_id, candidate=candidate,
                               canon=canon):
                reason = REASON_EXTRACTOR_IDENTITY
                continue

            # ── STAGE, then judge on the whole book AS IT STANDS NOW ─────────
            staged_blocks = list(current_blocks)
            staged_blocks[index] = candidate
            staged_bytes = _rebuild(current_snapshot, staged_blocks)
            try:
                staged_text = staged_bytes.decode("utf-8")
                # 🔴 MATERIALIZED THE SAME WAY AS THE SNAPSHOT IT IS COMPARED
                #    AGAINST — which the incoming snapshot itself tells us.
                #    `materialize_final_snapshot` resolves chapter ids only when a
                #    canon is supplied; supply it when the caller did and the ids
                #    line up, get it wrong in EITHER direction and every block's
                #    id disagrees with its claims, `evaluate_semantic` reads every
                #    artefact as INVALID_EXTRACTOR_OUTPUT, coverage collapses, and
                #    every candidate is refused as a regression. The symptom is a
                #    repair engine that silently never repairs — which is exactly
                #    how this was found, from the delivery seam and not from here.
                staged_snapshot = _l2.materialize_final_snapshot(
                    {source_key: staged_text},
                    canon=(canon if _ids_resolved else None))
            except Exception:  # noqa: BLE001 - a candidate that will not materialize
                reason = REASON_INVALID_CANDIDATE
                _log_invalid_candidate(
                    "post.materialize", chapter_index=index, attempt=attempt,
                    original=original, candidate=candidate)
                continue
            if staged_snapshot is None or len(staged_snapshot.blocks) != len(current_snapshot.blocks):
                # Every judgement below indexes chapters by position, so a candidate
                # that changes how many blocks the manuscript has would silently be
                # compared against the wrong ones.
                reason = REASON_INVALID_CANDIDATE
                _log_invalid_candidate(
                    "post.block_count", chapter_index=index, attempt=attempt,
                    original=original, candidate=candidate)
                continue
            if tuple(b.chapter_id for b in staged_snapshot.blocks) != \
                    tuple(b.chapter_id for b in current_snapshot.blocks):
                reason = REASON_INVALID_CANDIDATE
                _log_invalid_candidate(
                    "post.chapter_identity", chapter_index=index, attempt=attempt,
                    original=original, candidate=candidate)
                continue
            staged_claims = dict(current_claims)
            staged_claims[index] = fresh
            for i, blk in enumerate(staged_snapshot.blocks):
                prior = staged_claims.get(i)
                if prior is not None and prior.content_sha256 != blk.content_sha256:
                    staged_claims.pop(i)
            staged_result = dict(current_result)
            staged_result[source_key] = staged_text
            staged_report = _l2.build_report(
                staged_snapshot, canon, mode=mode, result=staged_result,
                claims_by_index=staged_claims)

            staged_violations = _violation_set(staged_report)
            # (1) every LIVE targeted violation for THIS chapter must be gone.
            if any(v for v in staged_violations
                   if v[2] == index and v[0] in codes):
                reason = REASON_INEFFECTIVE
                continue
            # (2) nothing new, anywhere — measured against the book as it stands
            #     NOW, so a defect an earlier repair removed cannot be reintroduced
            #     for free just because it appeared in the first report.
            if staged_violations - current_violations:
                reason = REASON_REGRESSED
                continue
            # (3) nothing became less measurable than it is NOW — coverage an
            #     earlier repair recovered is coverage this one may not spend.
            if _coverage_regressed(current_report, staged_report):
                reason = REASON_COVERAGE_REGRESSED
                continue

            accepted_bytes = candidate
            accepted_claims = fresh
            accepted_ratio = ratio
            reason = REASON_NONE
            # Promoted as ONE value below: a half-promoted state (new blocks with
            # the old report, say) would make every later chapter's judgement a
            # comparison between two different books.
            promoted = (staged_blocks, staged_claims, staged_snapshot,
                        staged_result, staged_report)
            break

        if promoted is not None:
            # ATOMIC PROMOTION. Blocks, claims, snapshot, result and report move
            # together or not at all — see the note at the accept site.
            (current_blocks, current_claims, current_snapshot,
             current_result, current_report) = promoted
        records.append(ChapterRepairV1(
            chapter_index=index, chapter_id=block.chapter_id,
            attempted_codes=codes, iterations=iterations,
            outcome=(OUTCOME_REPAIRED if accepted_bytes is not None
                     else OUTCOME_UNRESOLVED),
            last_reason=reason, accepted=accepted_bytes is not None,
            edit_ratio_ppm=accepted_ratio,
            before_sha256=block.content_sha256,
            after_sha256=(_cl.sha256_hex(accepted_bytes) if accepted_bytes is not None
                          else block.content_sha256),
        ))

    changed = {r.chapter_index for r in records if r.accepted}
    repaired = (_rebuild(current_snapshot, current_blocks) if changed
                else snapshot.manuscript_bytes)

    # ── THE FINAL REPORT ─────────────────────────────────────────────────────
    #
    # Rebuilt rather than reused, only so it can carry `repair_rounds`,
    # `affected_chapter_ids` and `edit_ratios_ppm` — the promoted `current_report`
    # is already a report of exactly this manuscript, because every accepted
    # candidate was judged on a book that CONTAINED all the repairs accepted before
    # it. (An earlier draft of this comment claimed each candidate was judged
    # against a book containing none of the others. That was never true, and under
    # the promoted state model it is not even close.)
    #
    # Which makes the rebuild a place a bug could hide, so it is checked: the same
    # inputs must produce the same verdict.
    rounds = max((r.iterations for r in records), default=0)
    final = _l2.build_report(
        current_snapshot, canon, mode=mode, result=current_result,
        claims_by_index=current_claims, repair_rounds=rounds,
        affected_chapter_ids=tuple(r.chapter_id for r in records
                                   if r.accepted and r.chapter_id != _cl.UNKNOWN),
        edit_ratios_ppm=tuple(r.edit_ratio_ppm for r in records
                              if r.accepted and r.chapter_id != _cl.UNKNOWN))
    # 🔴 COMPARED ON THE WHOLE VERDICT, NOT JUST THE VIOLATIONS. Two reports can
    #    carry identical violation sets and still disagree about everything that
    #    made them meaningful — a predicate that stopped being measured, a
    #    denominator that moved, a delivery binding that no longer matches the
    #    bytes. Each of those is a difference the acceptance decisions were never
    #    tested against, and a violation-set comparison waves all of them through.
    #    The repair bookkeeping fields are excluded because they are the one thing
    #    the rebuild exists to add.
    def _verdict(report: _l2.ContinuityReportV1) -> tuple:
        return (
            tuple(r.to_canonical_obj() for r in report.results),
            report.delivery_binding, report.persistence_binding,
            report.continuity_status, report.manuscript_sha256,
            report.ordered_blocks_sha256, report.chapter_ids,
            report.chapter_count, report.canon_sha256,
        )

    if _verdict(final) != _verdict(current_report):
        raise _schema_error(
            "final report disagrees with the state every acceptance was judged on")

    return RepairRunV1(
        schema_version=REPAIR_SCHEMA_VERSION,
        mode=mode,
        rounds=rounds,
        chapters=tuple(records),
        manuscript_sha256_before=before_sha,
        manuscript_sha256_after=_cl.sha256_hex(repaired),
        repaired_manuscript=repaired,
        report=final,
    )


def verify_applied(run: RepairRunV1, result: Mapping[str, Any]) -> str:
    """Did the delivery path actually receive the repaired bytes? `MATCH`/`MISMATCH`.

    🔴 THE STAGE-2 ANALOGUE OF STAGE 1'S CENSUS, AND FOR THE SAME REASON. Every
       hash above describes what this module produced. None of them says anything
       about what the caller then delivered. A repair that was computed, reported
       and never substituted is indistinguishable from one that landed — unless
       someone re-reads the delivery path and compares.
    """
    if not isinstance(run, RepairRunV1):
        raise _schema_error("run: expected RepairRunV1")
    key = _l2.resolve_manuscript_key(result)
    if key is None:
        return _l2.BINDING_MISMATCH
    text = result.get(key)
    if not isinstance(text, str):
        return _l2.BINDING_MISMATCH
    return (_l2.BINDING_MATCH
            if _cl.sha256_hex(text.encode("utf-8")) == run.manuscript_sha256_after
            else _l2.BINDING_MISMATCH)


def run_telemetry(run: RepairRunV1) -> dict[str, Any]:
    """Bounded labels, counts and hashes. No prose, no name, no manuscript span."""
    by_reason: dict[str, int] = {}
    for record in run.chapters:
        if record.accepted:
            continue
        by_reason[record.last_reason] = by_reason.get(record.last_reason, 0) + 1
    return {
        "schema_version": run.schema_version,
        "mode": run.mode,
        "rounds": run.rounds,
        "chapters_targeted": len(run.chapters),
        "chapters_repaired": sum(1 for r in run.chapters if r.accepted),
        "unresolved_reasons": dict(sorted(by_reason.items())),
        "max_edit_ratio_ppm": max((r.edit_ratio_ppm for r in run.chapters), default=0),
        "manuscript_changed": run.changed,
        "continuity_status": run.continuity_status,
        "manuscript_sha256_after": run.manuscript_sha256_after,
    }
