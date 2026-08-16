# -*- coding: utf-8 -*-
"""F6 — resolution accounting and deterministic post-repair verification.

🔴 THE OUTCOME THIS MODULE MAKES UNREPRESENTABLE.

        detected → repair failed / no-op → original still delivered

   v9 shipped exactly that, and nothing in the system contradicted it: the legacy lane raised
   `revised` without comparing the result to the original, so a byte-identical output counted as
   a repair. Here a hard violation has two outcomes and no third: `resolved` when a verifier
   proved the defect gone, `unresolved` otherwise — and `unresolved` blocks delivery.

🔴 HARD-BLOCK IS THE FALLBACK, NOT THE PRODUCT. Blocking everything would satisfy the invariant
   and help nobody. The point is that the positive path is reachable AND that a failure cannot
   be mistaken for it.

The accounting is deliberately dumb arithmetic over verdicts someone else produced. Verdicts
come from deterministic verifiers (below), never from a model's opinion about its own repair.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

import narasi_gate as _ngate

__all__ = [
    "F6_CLASSES", "F6AccountingError", "violation_identity",
    "resolution_accounting", "verify_tense_resolved", "verify_teleport_resolved",
    "verify_beat_resolved", "verify_ceiling_resolved", "chapter_word_bounds",
    "changed_chapters_from_blocks", "outline_beat_claim",
]

#: The hard-violation classes F6 owns. A class not listed here is not an F6 violation and is
#: not subject to this accounting.
F6_CLASSES = ("tense_drift", "teleport", "final_beat", "beat_execution", "chapter_ceiling")

#: Classes where a chapter can hold AT MOST ONE instance, so `class + chapter` identifies it:
#: a chapter has one dominant tense and one word count. Every other class can occur several
#: times in one chapter and therefore needs a server-owned discriminator.
_F6_SINGLE_INSTANCE_CLASSES = frozenset({"tense_drift", "chapter_ceiling"})

#: The only two verdicts. Anything else — a typo, a missing entry, a model's "looks fine" —
#: reads as `unresolved`, because absence of evidence is not evidence of repair.
_RESOLVED = "resolved"
_UNRESOLVED = "unresolved"


class F6AccountingError(RuntimeError):
    """The books do not balance. Raised rather than publishing numbers that do not add up."""


def violation_identity(violation: Any) -> Optional[str]:
    """A stable id for one hard violation, or None when it cannot be identified safely.

    🔴 NOT DERIVED FROM EVIDENCE. Verification runs against a manuscript whose bytes have
    changed — that is the whole point of a repair. An identity built from evidence prose would
    make the same defect look like a NEW violation after the rewrite, and the original would
    quietly leave the books instead of being marked unresolved.

    🔴 BUT `class + chapter` IS NOT AN IDENTITY EITHER. That was the first version, and two
    DIFFERENT teleports in chapter 2 collapsed into one `teleport:2`: `detected` dropped from
    two to one, the second defect left the books entirely, and resolving the first allowed
    delivery. Only classes that can occur at most once per chapter may be identified that way;
    every other class must carry a SERVER-OWNED `f6_claim` discriminator — built the way F5's
    `_f5_claim` is, from what this process knows the claim is about, never parsed from prose.

    A multi-instance violation with no claim returns None: it cannot be told apart from the
    next one, so it cannot be tracked across a repair, so nothing may vouch for it. The
    accounting still counts it — as unresolved."""
    if not isinstance(violation, dict):
        return None
    vclass = str(violation.get("f6_class") or "").strip().lower()
    if vclass not in F6_CLASSES:
        return None

    # 🔴 CHAPTERS ARE 1-BASED INTEGERS, EVERYWHERE IN THIS SYSTEM. An earlier version accepted
    # anything and rendered a bad value as `?` or as itself, so `tense_drift:0` existed, could
    # be "repaired" by a change set containing 0, and opened delivery. A finding whose chapter
    # is not a real chapter is malformed, and a malformed finding cannot be verified — so it
    # gets no identity and is counted unresolved.
    chapter = violation.get("chapter")
    if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
        return None

    # 🔴 A SINGLE-INSTANCE CLASS IGNORES ANY CLAIM THAT SHOWS UP. Reading it made the SAME
    # violation change identity the moment a claim was attached (`tense_drift:2` →
    # `tense_drift:2:same`), so a detection and its own post-repair verification could be about
    # two "different" violations and the original would leave the books.
    if vclass in _F6_SINGLE_INSTANCE_CLASSES:
        return f"{vclass}:{chapter}"

    claim = str(violation.get("f6_claim") or "").strip().lower()
    if not claim:
        return None
    return f"{vclass}:{chapter}:{claim}"


def _chapter_of(violation: Any) -> Optional[int]:
    chapter = (violation or {}).get("chapter") if isinstance(violation, dict) else None
    if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
        return None
    return chapter


#: The ONE preamble this process writes for itself: the metadata header `_apply_v3_gates` adds
#: to the assembled book after the pre-repair snapshot is taken. `narration_api._result_payload`
#: knows the same two prefixes; they are the server's framing, and nothing else is.
_SERVER_PREAMBLE_PREFIXES = ("> **Gaya:**", "> **Style:**")


def _is_server_framing(preamble: Any) -> bool:
    """Is this leading block the server's own header, and ONLY that (or nothing at all)?

    🔴 `startswith` WAS NOT ENOUGH, AND THAT IS THE WHOLE POINT. The header and anything
    prepended after it land in the SAME leading block, so text injected below it still made the
    block "start with" the known prefix and sailed through. The header is a markdown metadata
    quote closed by a rule (`narration_api` writes `> **Gaya:** …` lines and ends the block with
    `\n\n---\n\n`), so every non-blank line has to be part of that shape — a quoted line or the
    rule itself — and at least one of them has to carry a prefix this process wrote."""
    text = str(preamble or "").strip()
    if not text:
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not any(line.startswith(_SERVER_PREAMBLE_PREFIXES) for line in lines):
        return False
    return all(line.startswith(">") or set(line) == {"-"} for line in lines)


def changed_chapters_from_blocks(before_blocks, after_blocks) -> "Optional[set]":
    """Which chapters actually changed, by comparing BYTES the server holds on both sides.

    🔴 THE CHANGE SET MUST BE OBSERVED, NEVER ASSERTED. `resolution_accounting` refuses to
    resolve a violation whose chapter did not change — but that is only worth anything if the
    set comes from a comparison rather than from a caller's claim. A repair that returns the
    original bytes has changed nothing, whatever the lane reports; treating its own report as
    truth is v9's `revised=1` in a new place.

    Returns 1-BASED CHAPTER numbers. Returns None — "cannot be determined" — when the block
    counts differ, because a book that gained or lost a chapter did not have "chapter N edited"
    happen to it, and reporting a structural break as an edit would let a deletion read as a
    repair. `resolution_accounting` treats None as "nothing may be vouched for".

    🔴 THE PREAMBLE IS COMPARED SEPARATELY AND IS NOT A CHAPTER. `split_chapter_blocks` returns
    any text before the first heading as its own leading block. Numbering every block as a
    chapter made an edit to Chapter 2 report `{3}` on a book with a preamble — an off-by-one
    that points repairs and verdicts at the wrong chapter. A change in the preamble itself
    belongs to no chapter at all, so it too yields None rather than being attributed to one."""
    if not isinstance(before_blocks, (list, tuple)) or not isinstance(after_blocks, (list, tuple)):
        return None

    def _split_preamble(blocks):
        """Separate a leading non-heading block from the chapters. Done INDEPENDENTLY on each
        side: the gates add a `> **Gaya:** …` metadata header to the assembled book after the
        pre-repair snapshot is taken, so a preamble legitimately exists on one side and not the
        other. Requiring the raw block lists to line up made every real job compare 3 blocks
        against 4 and report "cannot be determined" — blocking everything."""
        blocks = list(blocks)
        if blocks and not _ngate.chapter_heading_line(blocks[0]):
            return blocks[0], blocks[1:]
        return "", blocks

    # 🔴 ONLY THE SERVER'S OWN FRAMING MAY BE NORMALISED AWAY. The trim above exists for ONE
    # header this process writes itself, and it used to drop ANY leading text on either side —
    # so a repair could prepend arbitrary prose to the manuscript and the comparison would not
    # see it at all: the targeted chapter resolved, delivery opened, and the injected text
    # shipped. A preamble that is not the known header is CONTENT, and content that appeared
    # from nowhere is a structural failure, not a chapter edit.
    #
    # The trim itself has to stay — keeping the preamble as a block would number it as chapter
    # one and shift every chapter after it, which is the off-by-one this comparator already had
    # once.
    preamble_before, before_blocks = _split_preamble(before_blocks)
    preamble_after, after_blocks = _split_preamble(after_blocks)
    if not _is_server_framing(preamble_before) or not _is_server_framing(preamble_after):
        return None
    # 🔴 AND IT MAY ONLY APPEAR, NEVER CHANGE. The production case is the header being ABSENT
    # from the snapshot and PRESENT in the delivered book, because the gates write it after the
    # snapshot is taken. Two DIFFERENT headers means something rewrote the server's own framing
    # between those two points, and that is not a chapter edit either.
    if preamble_before.strip() and preamble_after.strip() and preamble_before != preamble_after:
        return None
    if len(before_blocks) != len(after_blocks):
        return None
    offset = 0

    # 🔴 THE HEADING IS FRAMING, NOT CONTENT. Comparing whole blocks only, `## Chapter 2` →
    # `## Bab 99` read as an ordinary edit of chapter 2: the change set said `{2}`, the verifier
    # vouched, and delivery opened on a book whose chapter heading had been destroyed. A repair
    # may rewrite prose and nothing else, so a heading that moved, changed or vanished — and a
    # book whose chapters were reordered — is a STRUCTURAL failure, reported as "cannot be
    # determined" rather than attributed to any chapter.
    changed = set()
    for index, (before, after) in enumerate(
            zip(before_blocks[offset:], after_blocks[offset:]), 1):
        if _ngate.chapter_heading_line(before) != _ngate.chapter_heading_line(after):
            return None
        if before != after:
            changed.add(index)
    return changed


def _identities(violations: Iterable[Any]) -> list[str]:
    """Identities in first-seen order, deduplicated: two sightings are one violation."""
    seen: list[str] = []
    for violation in violations or ():
        identity = violation_identity(violation)
        if identity is not None and identity not in seen:
            seen.append(identity)
    return seen


def resolution_accounting(*, detected: Iterable[Any], targeted: Iterable[Any],
                          verdicts: dict, repair_attempts: int, provider_calls: int,
                          changed_chapters: Iterable[int], chapter_count: int,
                          co_targeted_chapters: Iterable[int] = (),
                          _force_resolved: Optional[int] = None) -> dict:
    """Close the books on one F6 pass.

    `verdicts` maps `violation_identity` → `"resolved"` / `"unresolved"`; anything missing or
    unrecognised counts as unresolved. A violation that was DETECTED but never TARGETED is
    unresolved too — "we didn't get to it" is not a pass.

    🔴 A VERDICT IS A CLAIM ABOUT A REPAIR, SO THE REPAIR HAS TO EXIST. The first version
    honoured the string alone: `repair_attempts=0, provider_calls=0, chapters_changed=0` with
    `{"tense_drift:2": "resolved"}` returned `resolved=1, delivery_blocked=False` — the state
    this module claims to make unrepresentable, constructed in one call. A resolution is now
    corroborated against `changed_chapters`: the violation's OWN chapter must be in it. A
    defect in the manuscript cannot be gone from a chapter nobody rewrote.

    🔴 `changed_chapters` IS THE SINGLE SOURCE. The published `chapters_changed` is derived from
    it rather than passed alongside it — two numbers that can disagree are exactly where a false
    resolution hides.

    Raises `F6AccountingError` unless `resolved + unresolved == detected`. The invariant is
    checked rather than assumed: if a later edit lets a violation escape the books, this must
    fail loudly instead of publishing a total that looks complete."""
    # 🔴 A COUNT IS A NON-NEGATIVE INTEGER. `repair_attempts=-7, provider_calls=-9` were once
    # published verbatim. A negative count is not a small count — it is a broken instrument,
    # and so is a bool or a string that happens to compare.
    for name, value in (("repair_attempts", repair_attempts),
                        ("provider_calls", provider_calls)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise F6AccountingError(
                f"F6 counter {name}={value!r} is not a non-negative integer")

    # 🔴 THE BOOK'S LENGTH IS SERVER-OWNED, AND WITHOUT IT ANY INTEGER IS A CHAPTER.
    # `chapter >= 1` was the only bound, so `tense_drift:999` with `changed_chapters={999}`
    # resolved and shipped a three-chapter book. Everything below is now checked against the
    # real chapter count.
    if isinstance(chapter_count, bool) or not isinstance(chapter_count, int) or chapter_count < 1:
        raise F6AccountingError(
            f"F6 chapter_count={chapter_count!r} is not a positive integer")

    # `None` means the byte comparison could not be made (a structural break, say). Nothing
    # may be vouched for then, so the change set is empty and every target stays unresolved.
    changed = {c for c in (changed_chapters or ())
               if isinstance(c, int) and not isinstance(c, bool) and c >= 1}
    beyond = sorted(c for c in changed if c > chapter_count)
    if beyond:
        raise F6AccountingError(
            f"F6 change set names chapter(s) {beyond} in a {chapter_count}-chapter book; "
            f"a byte comparison cannot report a chapter that does not exist")

    # 🔴 COUNTERS THAT CONTRADICT EACH OTHER ARE A BROKEN INSTRUMENT, NOT A PASS. Membership in
    # `changed_chapters` was once the whole corroboration, so a caller could report zero repair
    # attempts, zero provider calls AND a changed chapter — asserting a repair that never
    # happened — and the books resolved it. An attempt cannot happen without a provider call.
    #
    # 🔴 BUT "A CHAPTER CHANGED" DOES NOT IMPLY "F6 REPAIRED IT", AND RAISING ON THAT WAS WRONG.
    # The post-gates dedup guard, the canon-lite assist repair and the F1 scrub all rewrite the
    # manuscript after detection has finished with it — none of them is an F6 repair attempt.
    # The first version raised `counters contradict` there, so a CLEAN book that the dedup guard
    # happened to touch failed its accounting and was refused, under a message naming the wrong
    # cause. What the invariant actually protects is a RESOLUTION: `resolved_ids` below requires
    # `repair_attempts >= 1`, so nothing can be vouched for on the strength of a change no F6
    # repair produced — which is the audit's finding, enforced where it belongs.
    if int(repair_attempts) >= 1 and int(provider_calls) < 1:
        raise F6AccountingError(
            f"F6 counters contradict: repair_attempts={repair_attempts} with "
            f"provider_calls={provider_calls}; a repair attempt is a provider call")
    detected_list = [v for v in (detected or ())
                     if isinstance(v, dict)
                     and str(v.get("f6_class") or "").strip().lower() in F6_CLASSES]
    # A finding about a chapter the book does not have is malformed, exactly like one whose
    # chapter is not an integer: it cannot be verified, so it gets no identity — and it is
    # still counted, as unresolved. Each such occurrence counts separately; merging two
    # unknowns is the loss the identity rules exist to prevent.
    def _usable(violation) -> bool:
        chapter = _chapter_of(violation)
        return (violation_identity(violation) is not None
                and chapter is not None and chapter <= chapter_count)

    usable = [v for v in detected_list if _usable(v)]
    detected_ids = _identities(usable)
    orphans = len(detected_list) - len(usable)
    targeted_ids = [i for i in _identities(targeted) if i in detected_ids]

    # The chapter each identity belongs to, so a verdict can be checked against the repair.
    chapter_by_id: dict = {}
    for violation in usable:
        chapter_by_id.setdefault(violation_identity(violation), _chapter_of(violation))

    # 🔴 A TARGETED REPAIR MAY ONLY TOUCH WHAT IT TARGETED. Requiring merely that the target's
    # own chapter appears in the change set let a lane rewrite chapters 1, 2 and 3 for one
    # chapter-2 finding and still resolve — breaking both targeted repair and untouched-chapter
    # byte identity. A chapter that changed without being targeted is COLLATERAL, and a run
    # that produced any is a run whose output nobody can vouch for: no resolutions at all.
    # 🔴 F6 DOES NOT OWN THE REVISE IT RIDES ON. F1-F5 merge their findings into the SAME call,
    # so a chapter the consistency critic or the thread tracker legitimately asked for is a
    # chapter that was ASKED FOR — reading it as collateral refused jobs where every repair
    # worked. `co_targeted_chapters` is what those other gates targeted, supplied by the caller
    # that built the merged request; it widens what may change, and changes NOTHING about what
    # may be resolved, which still needs this violation's own chapter and its own verdict.
    allowed = {c for c in (chapter_by_id.get(i) for i in targeted_ids) if c is not None}
    allowed |= {c for c in (co_targeted_chapters or ())
                if isinstance(c, int) and not isinstance(c, bool) and c >= 1}
    collateral = sorted(changed - allowed)

    resolved_ids = [] if (collateral or int(repair_attempts) < 1) else [
        i for i in targeted_ids
        if isinstance(verdicts.get(i), str)
        and verdicts.get(i).strip().lower() == _RESOLVED
        and chapter_by_id.get(i) in changed
    ]
    unresolved_ids = [i for i in detected_ids if i not in resolved_ids]

    n_detected = len(detected_ids) + orphans
    n_resolved = len(resolved_ids) if _force_resolved is None else int(_force_resolved)
    n_unresolved = len(unresolved_ids) + orphans
    if n_resolved + n_unresolved != n_detected:
        raise F6AccountingError(
            f"F6 partition broken: resolved={n_resolved} + unresolved={n_unresolved} "
            f"!= detected={n_detected}; every detected violation needs exactly one verdict")

    return {
        "violations_detected": n_detected,
        "violations_targeted": len(targeted_ids),
        "repair_attempts": int(repair_attempts),
        "provider_calls": int(provider_calls),
        "chapters_changed": len(changed),
        "violations_resolved": n_resolved,
        "violations_unresolved": n_unresolved,
        "violations_unidentifiable": orphans,
        "collateral_chapters": collateral,
        "delivery_blocked": n_unresolved > 0,
        "resolved_ids": resolved_ids,
        "unresolved_ids": unresolved_ids,
    }


def verify_tense_resolved(tense_by_chapter_before, tense_by_chapter_after, *,
                          chapter: int, chapter_count: int,
                          changed_chapters: Iterable[int] = ()) -> bool:
    """Did the repair actually remove the tense outlier at `chapter`, without moving it?

    🔴 "THE CHAPTER CHANGED" IS NOT "THE DEFECT IS GONE" — v9's `revised=1` asserted exactly
    that without checking.

    🔴 AND "THE OLD TARGET IS NO LONGER AN OUTLIER" IS NOT ENOUGH EITHER. The first version
    checked only that, and two repairs passed that should not have:
      · `["past","past","present"]` for a target of 2 — the drift MOVED to chapter 3, and the
        books called the book clean;
      · `["past","past"]` for a three-chapter book — chapter 3 had been DELETED, and a shorter
        census read as a cleaner one.
    So the post-repair census must still describe the same book (`chapter_count` entries,
    validated against the actual chapters), and it must not contain any outlier that was not
    already there before the repair.

    🔴 AND THE "BEFORE" MUST BE READ, NOT ACCEPTED. An earlier version took `outliers_before`
    as a caller-supplied tuple, so the caller could assert whatever made its own repair pass:
    handing it `(2, 3)` made a drift that MOVED to chapter 3 read as pre-existing. Both
    censuses are now passed in and BOTH are validated here.

    🔴 A CHAPTER NOBODY CHANGED MUST KEEP ITS LABEL. `["present","present","present"]` once
    passed for a chapter-2 repair while chapters 1 and 3 were supposed to be byte-identical —
    their labels had moved, which means either the repair edited chapters it must not have or
    the census is unreliable. Either way nothing may be vouched for.

    A chapter that already drifted before the repair stays its own unresolved violation and
    blocks delivery on its own account; it must not additionally make a genuine repair
    elsewhere read as failed. That precision is what `changed_chapters` buys.

    An unreadable or invalid census on EITHER side is not a resolution: a verifier that cannot
    see is a verifier that must not vouch."""
    if not isinstance(chapter, int) or isinstance(chapter, bool):
        return False
    if not isinstance(chapter_count, int) or isinstance(chapter_count, bool) or chapter_count < 1:
        return False
    before = _ngate.tense_census(tense_by_chapter_before, chapter_count=chapter_count)
    if not before.get("valid"):
        return False
    census = _ngate.tense_census(tense_by_chapter_after, chapter_count=chapter_count)
    if not census.get("valid"):
        return False
    # 🔴 `valid` MEANS THE ANSWER WAS WELL-FORMED, NOT THAT THE BOOK IS SOUND. With no strict
    # majority the census reports NO outliers — correctly, since there is no minority to name —
    # and reading that as "consistent" made `["past","past","present","present"]` a resolution
    # for a book split two-and-two. A book with no dominant tense IS the drift.
    if not census.get("majority"):
        return False
    # 🔴 NO RANGE GUARD HERE, DELIBERATELY. One used to read
    # `if chapter < 1 or chapter > chapter_count: return False`, and a mutant that deleted it
    # SURVIVED — because it cannot be reached: the `chapter not in outliers_before` check below
    # already refuses every out-of-range chapter, since outliers are drawn from a census
    # validated to hold exactly `chapter_count` entries and are therefore always 1..N. Two
    # mechanisms for one rule is the failure this workstream keeps paying for; the outlier
    # membership check is the single door.
    labels_before = before.get("per_chapter") or []
    labels_after = census.get("per_chapter") or []
    changed = {c for c in (changed_chapters or ())
               if isinstance(c, int) and not isinstance(c, bool)}

    # The chapter must actually have been the defect: a "resolution" of something the
    # before-census never flagged is a claim about a violation that was not there.
    outliers_before = set(before.get("outliers") or [])
    if chapter not in outliers_before:
        return False

    # Every chapter outside the change set is supposed to have come back byte-identical, so
    # its label must be identical too. A moved label means the repair reached further than it
    # was allowed to, or the census cannot be trusted — neither vouches for anything.
    for index in range(1, chapter_count + 1):
        if index not in changed and labels_before[index - 1] != labels_after[index - 1]:
            return False

    outliers_after = set(census.get("outliers") or [])
    if chapter in outliers_after:
        return False
    return not (outliers_after - outliers_before)


def _positive_int(value: Any) -> bool:
    """A real 1-or-more integer — not a bool, not a float that compares, not a numeric string.

    🔴 ONE NAME FOR A PREDICATE THE NEW CLASSES ALL NEED. `resolution_accounting` and
    `verify_tense_resolved` still spell it inline: each of those lines is the target of its own
    mutant, and rewriting them would delete evidence rather than add any. Everything added after
    them calls this instead — four byte-identical copies of a guard cannot be mutated
    independently, so a harness pointed at one of them matches four times and is refused, which
    is how a guard ends up unfalsifiable while looking well tested."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


# ---------------------------------------------------------------------------
# Server-owned claims for the multi-instance classes
# ---------------------------------------------------------------------------
def outline_beat_claim(source: Any, outline_sizes: Any) -> str:
    """`outline_beat:<chapter>|<ordinal>` for a finding, or "" when it cannot be admitted.

    🔴 THE SAME DOOR F5 USES, AND DELIBERATELY THE SAME TOKEN SHAPE. `teleport`, `final_beat`
    and `beat_execution` can each occur several times in one chapter, so `class + chapter` is
    not an identity for them — that collapse is audit finding #2, where two different teleports
    in chapter 2 became one `teleport:2`, `detected` fell from two to one and resolving the
    first opened delivery. What tells them apart has to be SERVER-OWNED, and the accepted
    outline this process rendered is the structure already available: every one of these
    defects happens during some outlined beat.

    🔴 BOUNDED MEANS CHECKED AGAINST THE PACKET WE WROTE, NOT TRUSTED. `outline_sizes` maps
    chapter → beat count, counted from the rendered packet. A reference is admitted only when
    both values are integers and the ordinal falls inside 1..N for that chapter. Anything else
    — absent, a string, a bool, out of range, a chapter with no packet — yields "", which
    leaves the finding unidentifiable and therefore counted as UNRESOLVED. Inventing an
    identity for a reference nobody could verify is how a violation leaves the books."""
    if not isinstance(source, dict) or not isinstance(outline_sizes, dict):
        return ""
    chapter = source.get("outline_chapter")
    beat = source.get("outline_beat")
    if (isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1
            or isinstance(beat, bool) or not isinstance(beat, int) or beat < 1):
        return ""
    total = outline_sizes.get(chapter)
    if isinstance(total, bool) or not isinstance(total, int) or beat > total:
        return ""
    return f"outline_beat:{chapter}|{beat}"


# ---------------------------------------------------------------------------
# teleport — an untransitioned location change, verified by a count reaching zero
# ---------------------------------------------------------------------------
def verify_teleport_resolved(teleports_before, teleports_after, *, chapter: int,
                             chapter_count: int,
                             changed_chapters: Iterable[int] = ()) -> bool:
    """Did the repair remove the untransitioned location change(s) in `chapter`?

    🔴 "A NEW LOCATION WORD APPEARS" IS NOT "THE TRANSITION IS THERE". The verifier the brief
    warns about would search the repaired chapter for the destination and call it bridged; a
    repair that deleted the arrival entirely would pass that. What is checked instead is the
    server's own arithmetic over a census: the chapter's count of untransitioned moves has to
    reach ZERO, and no chapter may end up with more than it started with.

    🔴 A COUNT THAT MERELY FELL IS NOT A RESOLUTION. Two teleports in chapter 2 becoming one
    leaves a teleport in the delivered book. Both violations in that chapter stay unresolved
    together, which is the conservative reading and the only one that cannot ship a defect.

    🔴 AND A CHAPTER NOBODY CHANGED MUST KEEP ITS COUNT — the same rule the tense verifier
    enforces on labels. A count that moved in an untouched chapter means either the repair
    reached further than it was allowed to or the observation is unreliable; neither vouches
    for anything."""
    if not isinstance(chapter, int) or isinstance(chapter, bool):
        return False
    if not _positive_int(chapter_count):
        return False
    before = _ngate.teleport_census(teleports_before, chapter_count=chapter_count)
    if not before.get("valid"):
        return False
    after = _ngate.teleport_census(teleports_after, chapter_count=chapter_count)
    if not after.get("valid"):
        return False
    counts_before = before.get("per_chapter") or []
    counts_after = after.get("per_chapter") or []
    changed = {c for c in (changed_chapters or ())
               if isinstance(c, int) and not isinstance(c, bool)}

    # The chapter must actually have been the defect. As in the tense verifier, this is also
    # the single door that refuses an out-of-range chapter: the offender list is drawn from a
    # census validated to hold exactly `chapter_count` entries.
    if chapter not in set(before.get("offenders") or []):
        return False
    for index in range(1, chapter_count + 1):
        if index not in changed and counts_before[index - 1] != counts_after[index - 1]:
            return False
    if counts_after[chapter - 1] != 0:
        return False
    # A repair that pushed the defect into a neighbouring chapter resolved nothing.
    return not any(counts_after[i] > counts_before[i] for i in range(chapter_count))


# ---------------------------------------------------------------------------
# final_beat / beat_execution — an outlined beat that has to actually happen
# ---------------------------------------------------------------------------
def verify_beat_resolved(states_before, states_after, *, chapter: int, beat: int,
                         outline_sizes: Any, require_order: bool = False) -> bool:
    """Did the outlined beat `(chapter, beat)` actually get EXECUTED by the repair?

    🔴 A PROMISE IS NOT AN EXECUTION, AND THAT IS THE WHOLE CLASS. "I will give a deposition"
    reads like a resolution to any check that asks whether the deposition is mentioned. The
    census is three-valued for exactly this reason, and only `executed` counts — `promised`
    leaves the violation unresolved and blocks, which is what the brief asks for.

    🔴 THE BEAT MUST NOT HAVE BEEN DONE ALREADY. A "resolution" of a beat the before-census
    reported as `executed` is a claim about a violation that was not there — the same rule the
    tense verifier enforces with `outliers_before`.

    🔴 NOTHING MAY REGRESS. A repair that executes the final beat by dropping an earlier one
    has traded one defect for another; every beat that was `executed` before must still be
    `executed` after.

    🔴 `require_order` IS WHAT SEPARATES `beat_execution` FROM `final_beat`. The brief's chain
    is `surrender → recordings inadmissible → deposition executed`: a deposition that happens
    before the surrender it depends on is not the outlined beat, it is a different scene
    wearing its name. So for `beat_execution` every beat PRECEDING the target in outline order
    must be `executed` too. `final_beat` asks only that the ending's own decision happened."""
    if (isinstance(chapter, bool) or not isinstance(chapter, int)
            or isinstance(beat, bool) or not isinstance(beat, int)):
        return False
    before = _ngate.beat_census(states_before, outline_sizes=outline_sizes)
    if not before.get("valid"):
        return False
    after = _ngate.beat_census(states_after, outline_sizes=outline_sizes)
    if not after.get("valid"):
        return False
    states_b = before.get("states") or {}
    states_a = after.get("states") or {}
    key = (chapter, beat)
    # An unknown key cannot be in a census validated to cover exactly the outlined beats, so
    # this is also the single door that refuses a beat the accepted outline does not have.
    if states_b.get(key) == "executed" or key not in states_b:
        return False
    if states_a.get(key) != "executed":
        return False
    for other, state in states_b.items():
        if state == "executed" and states_a.get(other) != "executed":
            return False
    if require_order:
        for other in states_a:
            if other < key and states_a.get(other) != "executed":
                return False
    return True


# ---------------------------------------------------------------------------
# chapter_ceiling — the one class the server measures itself, end to end
# ---------------------------------------------------------------------------
def chapter_word_bounds(body: Any, chapter_count: int) -> dict:
    """The per-chapter word contract THIS SERVER issued: `{chapter: (floor, ceiling)}`.

    🔴 THE CEILING IS NOT A NUMBER F6 INVENTED. It is `word_target × 1.1`, the same `word_max`
    the generator was instructed to write inside (`laozhang_api`'s chapter prompt builds
    `word_min`/`word_max` from exactly this arithmetic). A ceiling F6 chose for itself would be
    a second contract, and the chapter would be judged against a bound nobody asked it to meet.

    🔴 AND THE FLOOR IS WHY AN EMPTY CANDIDATE CANNOT PASS. Verification asks whether the
    repaired chapter is at or under the ceiling — and a chapter reduced to nothing is very
    much under it. `word_min` is the other half of the contract the generator already had, so
    a reducer that deletes the chapter fails the same check that a reducer which did nothing
    fails.

    A chapter the request declares no target for simply has no ceiling: there is no contract to
    exceed, and inventing one would block a job over a bound it was never given. Chapters
    beyond `chapter_count` are dropped — the request copy can be longer than the book."""
    bounds: dict = {}
    if not _positive_int(chapter_count):
        return bounds
    chapters = (body or {}).get("chapters") if isinstance(body, dict) else None
    if not isinstance(chapters, list):
        return bounds
    for index, chapter in enumerate(chapters[:chapter_count], 1):
        if not isinstance(chapter, dict):
            continue
        raw = chapter.get("word_target") or chapter.get("words")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
            continue
        bounds[index] = (int(raw * 0.9), int(raw * 1.1))
    return bounds


def _valid_counts(counts, chapter_count: int) -> "Optional[list]":
    if not isinstance(counts, (list, tuple)) or len(counts) != chapter_count:
        return None
    out = []
    for count in counts:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        out.append(count)
    return out


def verify_ceiling_resolved(counts_before, counts_after, *, chapter: int, bounds: Any,
                            chapter_count: int,
                            changed_chapters: Iterable[int] = ()) -> bool:
    """Is `chapter` now inside the word contract, measured on the delivered bytes?

    🔴 THE ONLY F6 VERIFIER WITH NO MODEL IN IT. Both counts come from
    `narasi_gate.chapter_word_counts`, which is arithmetic over the manuscript. A reducer
    cannot talk its way past this one.

    🔴 "SHORTER" IS NOT "SHORT ENOUGH". The candidate is accepted only when the final count is
    genuinely `<= ceiling`; a chapter that came back 20 words lighter and still over is
    unresolved, and unresolved blocks. An empty or gutted chapter fails the floor.

    🔴 AND A REDUCTION MAY NOT PUSH ANOTHER CHAPTER OVER. Every chapter that was inside its own
    ceiling before must still be inside it after — the same "no new outlier" rule the tense and
    teleport verifiers apply."""
    if not isinstance(chapter, int) or isinstance(chapter, bool):
        return False
    if not _positive_int(chapter_count):
        return False
    if not isinstance(bounds, dict):
        return False
    before = _valid_counts(counts_before, chapter_count)
    after = _valid_counts(counts_after, chapter_count)
    if before is None or after is None:
        return False
    changed = {c for c in (changed_chapters or ())
               if isinstance(c, int) and not isinstance(c, bool)}
    pair = bounds.get(chapter)
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return False
    floor, ceiling = pair
    # The chapter must actually have been over its ceiling: this is the single door that
    # refuses an out-of-range chapter, since `bounds` only holds chapters 1..chapter_count.
    if before[chapter - 1] <= ceiling:
        return False
    for index in range(1, chapter_count + 1):
        if index not in changed and before[index - 1] != after[index - 1]:
            return False
    if not floor <= after[chapter - 1] <= ceiling:
        return False
    for index, limits in bounds.items():
        if not isinstance(limits, (list, tuple)) or len(limits) != 2:
            continue
        if before[index - 1] <= limits[1] < after[index - 1]:
            return False
    return True
