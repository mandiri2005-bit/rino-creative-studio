"""narasi_f8.py — F8: chapter-SEAM continuity. Pure census, identity, verification, accounting.

🔴 WHAT F8 IS. A seam is the transition between two adjacent chapters. v9 shipped with two
   elisions: 1→2 summarised the thaw off-page instead of enacting it, and 2→3 jumped from the
   rooftop straight into the boardroom — decision, evidence gathering, preparation, travel and
   elapsed time all missing. F8 detects those, routes them to the EXISTING structural
   addressed-patch actuator, and then proves from the delivered bytes that the transition is
   actually there.

🔴 THE TERMINAL LOOP, AND WHY BLOCKING IS THE FALLBACK RATHER THAN THE GOAL.
   detect → identify the exact transition → route to the authorised actuator → apply a bounded
   repair → prove server-owned bytes changed at the addressed unit → re-observe → prove the
   original defect is gone → prove no collateral defect appeared → account → allow delivery only
   if every hard finding resolved. A run that merely detects, routes, receives an "accepted"
   operation, or produces a revised string with no attributable byte change has NOT repaired
   anything, and this module refuses to call it resolved.

🔴 THIS MODULE IS NOT A SECOND PATCH ENGINE. No provider call, no prompt, no patch protocol, no
   manuscript mutation. It validates an observation, names a seam, decides whether a claimed
   repair really landed, and produces bounded accounting.

🔴 EVERY FAIL-OPEN PATH HERE WAS REPRODUCED BEFORE IT WAS CLOSED. The first version of this
   module shipped six of them, and each one had the same shape: something the CALLER supplied
   was treated as authority. The rules that follow all push authority back to the server —
     · the unresolved universe comes from the validated before-census, never from the caller's
       target list, so routing zero targets cannot erase a detected defect;
     · comparison is per DIMENSION, so a repair cannot swap `causal` for `location` and read as
       progress;
     · the authorisation set is a required argument derived from canonical routing, never
       defaulted to "whatever changed" — which would declare every edit authorised by the fact
       that it happened;
     · a changed chapter is not a seam repair without the addressed-patch lane's own attribution;
     · seam identity is recomputed server-side and a caller-supplied one that disagrees is
       refused rather than believed;
     · an invalid chapter count is UNPROVED, not "no seam to check".

🔴 EVERYTHING PERSISTED IS BOUNDED AND CLOSED. Counts, 1-based chapter indices, bounded
   identities, and a reason from a fixed vocabulary. Never prose, quotes, titles, or a model's
   free-text reason.
"""
from __future__ import annotations

SCHEMA_VERSION = "narasi.f8.seam.v1"

#: The three independent ways a chapter transition can be elided. Reported separately because a
#: repair that supplies one does not supply the others: v9's 2→3 seam had a location jump AND a
#: time jump AND no causal decision, and a bridge that only stamps "three weeks later" closes
#: exactly ONE of them — legitimately, that one is closed — while reading, superficially, like a
#: fix for all three.
DIMENSIONS = ("causal", "location", "time")

#: `explicit`  — the transition is accounted for on the page.
#: `continuous`— nothing changed in that dimension, so no bridge is required.
#: `missing`   — an off-page transition or an unsupported jump.
#:
#: 🔴 WHAT `explicit` MEANS FOR TIME, PRECISELY. An on-page interval — "Three weeks later" —
#: CLOSES the time dimension. A time ellipsis is a legitimate narrative device, and a gate that
#: demanded every interval be dramatised scene by scene would refuse sound books by default.
#: What it does NOT do is close the other two: the same opening can state its interval and
#: still elide the decision that led there and the move that got there. The dimensions are
#: judged separately for exactly this reason.
STATUSES = frozenset({"explicit", "continuous", "missing"})

#: `continuous` is NOT a weaker `explicit`: a chapter that stays in the same room at the same
#: hour genuinely needs no bridge, and demanding one would send a sound chapter to a rewrite.
SOUND_STATUSES = frozenset({"explicit", "continuous"})

#: A book is bounded at 200 chapters (the tense/teleport census bound — they describe the same
#: chapters), so seams are bounded at 199.
MAX_CHAPTERS = 200

#: Persisted id lists are capped: a jobs row is not a log.
MAX_PERSISTED_IDS = 40

#: Why a census could not be used. Closed, and every one BLOCKS delivery — none may ever be read
#: as "no seam defect".
UNPROVED_REASONS = frozenset({
    "absent",                 # the field was not reported at all
    "not_a_list",             # scalar, dict or string where a list was required
    "wrong_length",           # not exactly chapter_count - 1 rows
    "not_a_row",              # an entry that is not a mapping
    "invalid_chapter",        # bool, float, string, zero, negative, or out of range
    "not_adjacent",           # reversed, skipped, duplicated, or out-of-order pair
    "invalid_status",         # a dimension value outside the closed vocabulary
    "missing_dimension",      # a row that does not carry all three dimensions
    "too_many_seams",         # beyond MAX_CHAPTERS
    "invalid_chapter_count",  # the server could not frame the book at all
})

#: Only a VALID one-chapter book is legitimately not applicable. An invalid, missing, bool,
#: float, string, zero, negative or over-limit count is UNPROVED — the earlier version collapsed
#: both into `not_applicable`, so a book the server could not frame was waved through.
NOT_APPLICABLE = "not_applicable"

#: The closed verdict vocabulary. Anything else is a bug, never "some other reason".
#: 🔴 A DISAGREEMENT IS NOT A MALFORMED ROW. Encoding a free-text-vs-census conflict as
#: `before_not_a_row` said the census was unparseable when it parsed perfectly — the two
#: observations simply contradicted each other, which is its own fact and deserves its own name.
BOUNDARY_CONFLICT = "boundary_observation_conflict"

VERDICT_REASONS = frozenset(
    {"ok", "invalid_targets", "invalid_chapter_set", BOUNDARY_CONFLICT}
    | {f"before_{r}" for r in UNPROVED_REASONS}
    | {f"after_{r}" for r in UNPROVED_REASONS}
)

#: What may be persisted. `unknown` is the fixed code an out-of-vocabulary reason is mapped to —
#: mapping is safe, echoing a caller string is not.
ACCOUNTING_REASONS = frozenset(
    VERDICT_REASONS | {"accounting_contradiction", "unknown", "census_unproved"})


def _is_chapter(value) -> bool:
    """A usable 1-based chapter number. `True` is an `int` in Python and is not one."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and 1 <= value <= MAX_CHAPTERS)


def seam_identity(chapter_a, chapter_b) -> str:
    """The server-owned name of a seam. Deterministic, and never the model's prose.

    🔴 STRICT ON PURPOSE — IT USED TO COERCE THROUGH `int()`. A helper that quietly turns
    `"2"`, `2.0` or `True` into a chapter number lets a malformed target name a real seam."""
    if not (_is_chapter(chapter_a) and _is_chapter(chapter_b)
            and chapter_b == chapter_a + 1):
        raise ValueError("seam identity requires an adjacent 1-based chapter pair")
    return f"seam:{chapter_a}|{chapter_b}"


def dimension_identity(chapter_a, chapter_b, dimension: str) -> str:
    """`seam:<a>|<b>:<dimension>` — the unit comparison actually happens in.

    🔴 SEAM-LEVEL SET ARITHMETIC CANNOT SEE A SUBSTITUTION. `causal` closing while `location`
    opens leaves the seam in the broken set both before and after, so a seam-level diff reports
    no new defect and the seam reads repaired. Dimensions are compared individually."""
    if dimension not in DIMENSIONS:
        raise ValueError("unknown seam dimension")
    return f"{seam_identity(chapter_a, chapter_b)}:{dimension}"


def _refused(reason: str) -> dict:
    return {"valid": False, "applicable": True, "reason": reason, "rows": [], "missing": {},
            "missing_dims": frozenset(), "seams": [], "chapter_count": 0}


def seam_census(rows, *, chapter_count) -> dict:
    """Validate a model-supplied seam census against the chapters the SERVER knows exist.

    Returns ``{"valid", "applicable", "reason", "rows", "missing", "missing_dims", "seams",
    "chapter_count"}``.

    🔴 THE ROWS MUST COVER EVERY ADJACENT PAIR, IN ORDER, OR THE CENSUS MEANS NOTHING. Entry N
    claims to be the seam between chapter N and N+1. A census that skips a pair, repeats one,
    reverses one, or is the wrong length cannot be indexed, and every verdict read off it would
    describe a different transition. That is one failure, not four.

    🔴 `chapter_count` MUST COME FROM SERVER-OWNED FINAL CHAPTER FRAMING, not from the model and
    not from the outline. It is the only side of this comparison the model cannot move."""
    if isinstance(chapter_count, bool) or not isinstance(chapter_count, int):
        return _refused("invalid_chapter_count")
    if chapter_count < 1:
        return _refused("invalid_chapter_count")
    if chapter_count > MAX_CHAPTERS:
        return _refused("too_many_seams")
    if chapter_count == 1:
        # A valid one-chapter book has no seam. Not a defect, and not something to invent a
        # failure for — but distinct from a book that could not be framed at all.
        return {"valid": True, "applicable": False, "reason": NOT_APPLICABLE, "rows": [],
                "missing": {}, "missing_dims": frozenset(), "seams": [],
                "chapter_count": chapter_count}

    expected = chapter_count - 1
    if rows is None:
        return _refused("absent")
    if not isinstance(rows, list):
        return _refused("not_a_list")
    if len(rows) != expected:
        return _refused("wrong_length")

    out_rows, missing, missing_dims, seams = [], {}, set(), []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            return _refused("not_a_row")
        first, second = row.get("chapter_a"), row.get("chapter_b")
        if not _is_chapter(first) or not _is_chapter(second):
            return _refused("invalid_chapter")
        # Position IS the seam: row N must be exactly (N, N+1). Checking adjacency alone would
        # accept a census that is internally tidy and describes the wrong book.
        if first != index or second != index + 1:
            return _refused("not_adjacent")
        normalised = {"chapter_a": first, "chapter_b": second}
        gone = []
        for dimension in DIMENSIONS:
            if dimension not in row:
                return _refused("missing_dimension")
            status = row.get(dimension)
            if not isinstance(status, str) or status not in STATUSES:
                return _refused("invalid_status")
            normalised[dimension] = status
            if status == "missing":
                gone.append(dimension)
                missing_dims.add(dimension_identity(first, second, dimension))
        identity = seam_identity(first, second)
        out_rows.append(normalised)
        seams.append(identity)
        if gone:
            missing[identity] = gone
    return {"valid": True, "applicable": True, "reason": "ok", "rows": out_rows,
            "missing": missing, "missing_dims": frozenset(missing_dims), "seams": seams,
            "chapter_count": chapter_count}


def detect(census: dict, *, openings=None) -> list:
    """One server-owned violation per broken seam. Targets the LATER chapter.

    🔴 THE FIX ALWAYS BELONGS TO THE OPENING. A missing bridge is repaired by writing the
    transition into the chapter that opens after it, not by rewriting the chapter that already
    ended correctly.

    🔴 ONE VIOLATION PER SEAM, NOT ONE PER DIMENSION. Three missing dimensions at one seam are
    one broken transition; three violations would budget one repair three times and let a patch
    that fixed only the time stamp report two thirds of a success.

    🔴 THE MISSING SET IS READ FROM THE CENSUS, NOT RECOMPUTED HERE. An earlier version derived
    it a second time from the row, and a mutation that stopped the census recording `missing` at
    all left detection working — two mechanisms for one rule, each hiding the other's removal.
    `seam_census()` is the single place a dimension becomes `missing`."""
    if not census.get("valid") or not census.get("applicable"):
        return []
    missing = census.get("missing") or {}
    openings = openings or {}
    found = []
    for row in census.get("rows") or []:
        first, second = row["chapter_a"], row["chapter_b"]
        identity = seam_identity(first, second)
        gone = missing.get(identity)
        if not gone:
            continue
        found.append({
            "type": "chapter_boundary_break",
            "f8_class": "chapter_seam",
            "severity": "high",
            "chapter": second,
            "seam": identity,
            "chapter_a": first,
            "chapter_b": second,
            "missing": list(gone),
            "evidence": _evidence(openings.get(second), second),
            "fix": _directive(first, second, gone),
        })
    return found


#: The opening excerpt handed to the actuator as a locator. Bounded, and taken from the chapter
#: being REPAIRED — never from the preceding chapter: injecting the previous chapter's tail is
#: forbidden by the V5-FINAL routing constraints, and the addressed-patch lane addresses units
#: by id anyway.
MAX_EVIDENCE_CHARS = 180

#: The prompt builder truncates at 420; staying inside it is the contract.
MAX_DIRECTIVE_CHARS = 420


def _evidence(opening, chapter_b) -> str:
    """A literal substring of the chapter being repaired, plus the deterministic `@chN` locator.

    🔴 IT MUST BE REAL MANUSCRIPT TEXT, NOT A PARAPHRASE. The chunked-revise path matches
    evidence as a literal substring; a description of the problem would never match and the
    violation would land in the UNMAPPED bucket. The `@chN` suffix is the belt-and-braces
    locator the pre-existing boundary finding already uses, for the case where a later mutator
    has touched the seam text since this was captured."""
    text = " ".join(str(opening or "").split())[:MAX_EVIDENCE_CHARS]
    return f"{text} @ch{int(chapter_b)}".strip()


#: What each dimension actually asks the actuator to write. Naming the dimension alone
#: ("causal is missing") tells a provider nothing it can act on.
#: 🔴 KEPT SHORT ON PURPOSE. `_narasi_structural_patch_revise` truncates `fix` at 420
#: characters, and the first version of this directive ran past it — the closing instruction
#: ("enact it, do not summarise") was cut off in the prompt the provider actually received,
#: which is the one sentence the whole repair turns on.
_DIMENSION_DIRECTIVE = {
    "causal": "the decision that leads here",
    "location": "the move itself, not just the new place",
    "time": "the interval, stated on the page",
}


def _directive(chapter_a, chapter_b, gone) -> str:
    """The instruction the structural actuator actually receives.

    🔴 THIS FIELD IS THE WHOLE INSTRUCTION. `_narasi_structural_patch_revise` builds its prompt
    from `evidence` and `fix` and NOTHING else — a violation that omits them reaches the
    provider as `- [high/chapter_boundary_break]  -> FIX:`, an empty directive against a chapter
    the model is seeing without any statement of what is wrong with it. Naming the missing
    dimensions is the difference between a repair request and a shrug."""
    wanted = [d for d in DIMENSIONS if d in set(gone or ())]
    parts = "; ".join(f"{d} — {_DIMENSION_DIRECTIVE[d]}" for d in wanted)
    text = (
        f"Chapter {int(chapter_a)}->{int(chapter_b)} transition is elided. Add the smallest "
        f"on-page bridge at this chapter's opening supplying: {parts}. "
        "Enact it, do not summarise it. Keep the outlined beat order, leave every other unit "
        "untouched, invent no new event."
    )
    assert len(text) <= MAX_DIRECTIVE_CHARS, "directive would be truncated in the prompt"
    return text


def _validate_targets(targeted, chapter_count):
    """Canonicalise routing targets, or None when any of them is unusable.

    🔴 A TARGET LIST IS AN ACCOUNT OF ATTEMPTED ROUTING, NOT AN AUTHORITY OVER WHAT EXISTS. It
    is validated strictly and its identity is RECOMPUTED here; a caller-supplied `seam` that
    disagrees with its own chapter pair is refused rather than believed."""
    canonical, seen = [], set()
    for entry in targeted or ():
        if not isinstance(entry, dict):
            return None
        first, second = entry.get("chapter_a"), entry.get("chapter_b")
        if not _is_chapter(first) or not _is_chapter(second):
            return None
        if second != first + 1 or second > chapter_count:
            return None
        identity = seam_identity(first, second)
        if entry.get("seam") is not None and entry.get("seam") != identity:
            return None
        if identity in seen:
            return None
        seen.add(identity)
        canonical.append({"chapter_a": first, "chapter_b": second, "seam": identity})
    return canonical


def _validate_chapter_set(values, chapter_count):
    """A set of 1-based chapters, or None when any member is unusable.

    🔴 SILENT FILTERING IS HOW AN UNAUTHORISED EDIT BECOMES INVISIBLE. A `"1"` quietly dropped
    from the change set is a chapter that changed and stopped being counted."""
    out = set()
    for value in values or ():
        if not _is_chapter(value) or value > chapter_count:
            return None
        out.add(value)
    return out


def verify(*, before: dict, after: dict, targeted, changed_chapters, allowed_chapters,
           attribution) -> dict:
    """Decide, from two VALIDATED censuses plus server-owned change and authorisation sets,
    which detected seams are genuinely resolved.

    `allowed_chapters` and `attribution` are REQUIRED. The authorisation set must be derived
    from canonical routing targets across the authorised lanes — never from the observed change
    set, which would make collateral impossible by construction. `attribution` is the set of
    seam identities the addressed-patch lane accepted an operation for AND whose addressed unit
    actually changed bytes.

    Returns ``{"resolved", "unresolved", "unproved", "collateral", "new_defects", "blocked",
    "reason"}`` — identities only, no prose.

    🔴 SIX THINGS MUST ALL HOLD BEFORE A SEAM IS `resolved`:
      1. it was genuinely broken BEFORE (a target that was never broken proves nothing);
      2. the after-census is VALID (an omitted row must never read as a fixed seam — the single
         easiest way for a model to "resolve" everything is to say less);
      3. NO dimension of that seam is missing afterwards — not merely the ones that were missing
         before, or a substitution passes as a repair;
      4. the addressed-patch lane attributes an accepted, byte-changing operation to THAT seam;
      5. the targeted chapter's bytes actually changed;
      6. nothing outside the authorisation set changed.

    🔴 COLLATERAL INVALIDATES EVERY RESOLUTION IN THE RUN, not just its own chapter: the same
    uncontrolled edit produced the others."""
    empty = {"resolved": [], "unresolved": [], "unproved": [], "collateral": [],
             "new_defects": [], "blocked": True, "reason": "ok"}

    if not before.get("valid"):
        return {**empty, "reason": f"before_{before.get('reason') or 'absent'}"}
    if not after.get("valid"):
        return {**empty, "reason": f"after_{after.get('reason') or 'absent'}"}

    chapter_count = int(before.get("chapter_count") or 0)
    canonical = _validate_targets(targeted, chapter_count)
    if canonical is None:
        return {**empty, "reason": "invalid_targets"}
    changed = _validate_chapter_set(changed_chapters, chapter_count)
    allowed = _validate_chapter_set(allowed_chapters, chapter_count)
    if changed is None or allowed is None:
        return {**empty, "reason": "invalid_chapter_set"}

    attributed = {a for a in (attribution or ()) if isinstance(a, str)}
    before_missing = before.get("missing") or {}
    after_missing = after.get("missing") or {}

    # Dimension-sensitive: a dimension that opens on an already-broken seam is a NEW defect.
    new_defects = sorted(frozenset(after.get("missing_dims") or ())
                         - frozenset(before.get("missing_dims") or ()))
    collateral = sorted(changed - allowed)

    # 🔴 THE UNRESOLVED UNIVERSE IS THE DETECTED SET, NOT THE TARGET LIST. Routing zero targets
    #    must never erase a detected defect.
    detected = set(before_missing)
    proved = set()
    for entry in canonical:
        identity = entry["seam"]
        if identity not in before_missing:
            continue                                  # never broken → nothing to resolve
        if after_missing.get(identity):
            continue                                  # any dimension still missing
        if identity not in attributed:
            continue                                  # no addressed-patch evidence
        if entry["chapter_b"] not in changed:
            continue                                  # the observation moved, the bytes did not
        if collateral:
            continue                                  # the whole run is untrustworthy
        proved.add(identity)

    unresolved = sorted(detected - proved)
    # 🔴 COLLATERAL GUARDS F8'S OWN RESOLUTION CLAIMS, IT IS NOT A GENERAL CHANGE POLICE.
    # An edit outside the authorisation set contaminates every resolution produced by the same
    # run, so with targets it blocks and zeroes them. With NO targets there is no claim to
    # contaminate — a server-owned late mutator that touched a chapter is F6's judgement to
    # make, not F8's, and refusing it here would fail books whose seams are provably sound.
    # A seam that mutator actually BROKE is still caught, by `new_defects`.
    return {"resolved": sorted(proved), "unresolved": unresolved, "unproved": [],
            "collateral": collateral, "new_defects": new_defects,
            "blocked": bool(unresolved or new_defects or (collateral and targeted)),
            "reason": "ok"}


def accounting(*, census_before: dict, targeted, verdict: dict, attempts, provider_calls,
               accepted_operations, byte_changing) -> dict:
    """The bounded, versioned block that goes into the durable result payload.

    🔴 AN ACCOUNTING THAT CANNOT BE COMPLETED IS ITSELF A BLOCKER. A run reporting one resolved
    seam and zero byte-changing repairs has disproved its own claim, and publishing it as a
    success would make the accounting the least trustworthy thing in the payload."""
    def _count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else -1

    canonical = _validate_targets(targeted, int(census_before.get("chapter_count") or 0))
    detected = sorted(census_before.get("missing") or {})
    resolved = sorted(set(verdict.get("resolved") or []))
    unresolved = sorted(set(verdict.get("unresolved") or []))
    unproved = sorted(set(verdict.get("unproved") or []))
    collateral = sorted(set(verdict.get("collateral") or []))
    new_defects = sorted(set(verdict.get("new_defects") or []))

    n_attempts = _count(attempts)
    n_calls = _count(provider_calls)
    n_accepted = _count(accepted_operations)
    n_bytes = _count(byte_changing)
    n_targeted = len(canonical) if canonical is not None else -1
    n_detected = len(detected)

    applicable = bool(census_before.get("applicable"))
    census_valid = bool(census_before.get("valid"))

    reason = verdict.get("reason")
    reason = reason if reason in VERDICT_REASONS else "unknown"
    if not census_valid:
        reason = "census_unproved" if reason == "ok" else reason

    contradictions = [
        min(n_attempts, n_calls, n_accepted, n_bytes, n_targeted) < 0,
        len(resolved) > n_bytes,
        n_bytes > n_accepted,
        n_accepted > n_attempts,
        n_attempts > n_targeted,
        n_targeted > n_detected,
        # 🔴 NOT `calls >= attempts`: ONE merged revise repairs every routed seam in a
        # single physical call, so a 1:1 ratio is not a property this lane has. What
        # must hold is that an attempt COST something — attempting without paying is
        # the shape that would let a lane report repairs it never asked for.
        n_attempts > 0 and n_calls < 1,
        applicable and census_valid and n_detected != len(resolved) + len(unresolved),
        bool(collateral) and bool(resolved),
    ]
    contradicted = any(contradictions)
    if contradicted:
        reason = "accounting_contradiction"

    blocked = bool(verdict.get("blocked")) or contradicted or not census_valid \
        or bool(new_defects) or bool(unproved) or reason == "unknown"

    def _ids(values):
        return sorted(values)[:MAX_PERSISTED_IDS]

    return {
        "schema_version": SCHEMA_VERSION,
        "applicable": applicable,
        "census_valid": census_valid,
        "seams_detected": n_detected,
        "seams_targeted": max(n_targeted, 0),
        "repair_attempts": max(n_attempts, 0),
        "provider_calls": max(n_calls, 0),
        "accepted_operations": max(n_accepted, 0),
        "byte_changing_repairs": max(n_bytes, 0),
        "seams_resolved": len(resolved),
        "seams_unresolved": len(unresolved),
        "seams_unproved": len(unproved),
        "collateral_chapters": _ids(collateral),
        "new_defects": _ids(new_defects),
        "detected_ids": _ids(detected),
        "resolved_ids": _ids(resolved),
        "unresolved_ids": _ids(unresolved),
        "reason": reason,
        "delivery_blocked": blocked,
    }
