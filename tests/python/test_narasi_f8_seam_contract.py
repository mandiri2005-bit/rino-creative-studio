"""F8 — the PURE seam contract: census validation, targeting, verification, accounting.

🔴 EVERY ROW HERE IS A FAIL-OPEN PATH THAT WAS REPRODUCED IN THE HELPER BEFORE IT WAS FIXED.
   The first version of `narasi_f8.py` was written before any test existed — a real process
   error, recorded rather than dressed up. These tests were written afterwards, run RED against
   that helper, and only then was the contract fixed. Each block below names the defect it
   pins so a future edit cannot quietly re-open it.

🔴 THE GOVERNING ASYMMETRY. A seam defect is only gone when the server can PROVE it gone. Every
   ambiguity here resolves to unresolved/UNPROVED and blocks delivery: an empty target list, a
   census that cannot be validated, an unauthorised chapter edit, a dimension that moved rather
   than closed, a chapter that changed for some other lane's reason. Blocking is the fallback,
   never the goal — but it is what an unproven repair gets.
"""
from __future__ import annotations

import importlib
import json
import sys

import pytest


def _live(name):
    return sys.modules.get(name) or importlib.import_module(name)


f8 = _live("narasi_f8")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def row(a, b, causal="explicit", location="explicit", time="explicit"):
    return {"chapter_a": a, "chapter_b": b,
            "causal": causal, "location": location, "time": time}


def census(rows, chapters=3):
    return f8.seam_census(rows, chapter_count=chapters)


def clean3():
    return census([row(1, 2), row(2, 3)])


def broken_23():
    return census([row(1, 2),
                   row(2, 3, causal="missing", location="missing", time="missing")])


def target(a, b):
    """A server-shaped routing target. Identity is NOT supplied — the server owns it."""
    return {"chapter_a": a, "chapter_b": b}


# ---------------------------------------------------------------------------
# P1 — a detected seam that was never routed must NOT pass delivery
# ---------------------------------------------------------------------------
def test_a_detected_seam_with_no_target_still_blocks_delivery():
    """🔴 ROUTING ZERO TARGETS MUST NEVER ERASE A DETECTED DEFECT. The unresolved universe comes
    from the validated before-census, not from the caller's target list. A target list describes
    attempted routing; it is not authority over which defects exist."""
    before = census([row(1, 2, causal="missing"), row(2, 3)])
    verdict = f8.verify(before=before, after=before, targeted=[],
                        changed_chapters=set(), allowed_chapters=set(), attribution={})
    assert verdict["unresolved"] == ["seam:1|2"]
    assert verdict["blocked"] is True


def test_a_second_detected_seam_that_was_not_routed_still_blocks():
    before = census([row(1, 2, causal="missing"), row(2, 3, time="missing")])
    after = census([row(1, 2), row(2, 3, time="missing")])
    verdict = f8.verify(before=before, after=after, targeted=[target(1, 2)],
                        changed_chapters={2}, allowed_chapters={2},
                        attribution={"seam:1|2"})
    assert verdict["resolved"] == ["seam:1|2"]
    assert verdict["unresolved"] == ["seam:2|3"]
    assert verdict["blocked"] is True


# ---------------------------------------------------------------------------
# P1 — a dimension may not be SUBSTITUTED for another
# ---------------------------------------------------------------------------
def test_a_repair_that_swaps_one_missing_dimension_for_another_is_not_resolved():
    """🔴 THE SEAM IS RESOLVED ONLY WHEN NO DIMENSION REMAINS MISSING. A bridge that supplies the
    causal decision while dropping the location change reads, dimension-by-dimension, like
    progress — and delivers a transition that is still elided."""
    before = census([row(1, 2, causal="missing"), row(2, 3)])
    after = census([row(1, 2, location="missing"), row(2, 3)])
    verdict = f8.verify(before=before, after=after, targeted=[target(1, 2)],
                        changed_chapters={2}, allowed_chapters={2},
                        attribution={"seam:1|2"})
    assert verdict["resolved"] == []
    assert verdict["unresolved"] == ["seam:1|2"]
    assert verdict["blocked"] is True


def test_a_newly_missing_dimension_on_an_already_broken_seam_is_a_new_defect():
    """New defects are DIMENSION-sensitive: seam-level set arithmetic cannot see a dimension
    appearing on a seam that was already in the broken set."""
    before = census([row(1, 2, causal="missing"), row(2, 3)])
    after = census([row(1, 2, causal="missing", time="missing"), row(2, 3)])
    verdict = f8.verify(before=before, after=after, targeted=[target(1, 2)],
                        changed_chapters={2}, allowed_chapters={2},
                        attribution={"seam:1|2"})
    assert "seam:1|2:time" in verdict["new_defects"]
    assert verdict["blocked"] is True


def test_a_clean_seam_that_breaks_during_repair_is_a_new_defect():
    verdict = f8.verify(before=broken_23(),
                        after=census([row(1, 2, location="missing"), row(2, 3)]),
                        targeted=[target(2, 3)], changed_chapters={3},
                        allowed_chapters={3}, attribution={"seam:2|3"})
    assert "seam:1|2:location" in verdict["new_defects"]
    assert verdict["blocked"] is True


# ---------------------------------------------------------------------------
# P1 — collateral authorisation is SERVER-OWNED and mandatory
# ---------------------------------------------------------------------------
def test_the_authorisation_set_is_a_required_argument():
    """🔴 `allowed = allowed if allowed is not None else changed` MAKES COLLATERAL IMPOSSIBLE.
    Defaulting the authorisation to the observed change set declares every edit authorised by
    the fact that it happened."""
    with pytest.raises(TypeError):
        f8.verify(before=broken_23(), after=clean3(), targeted=[target(2, 3)],
                  changed_chapters={1, 3}, attribution={"seam:2|3"})


def test_a_chapter_that_changed_without_authorisation_is_collateral():
    verdict = f8.verify(before=broken_23(), after=clean3(), targeted=[target(2, 3)],
                        changed_chapters={1, 3}, allowed_chapters={3},
                        attribution={"seam:2|3"})
    assert verdict["collateral"] == [1]
    assert verdict["resolved"] == []
    assert verdict["blocked"] is True


@pytest.mark.parametrize("bad", [True, False, 1.0, "2", 0, -1, 201, None])
def test_an_invalid_member_of_either_chapter_set_is_unproved_not_filtered(bad):
    """🔴 SILENT FILTERING IS HOW AN UNAUTHORISED EDIT BECOMES INVISIBLE. A `"1"` dropped from
    the change set is a chapter that changed and stopped being counted."""
    for kwargs in ({"changed_chapters": {3, bad}, "allowed_chapters": {3}},
                   {"changed_chapters": {3}, "allowed_chapters": {3, bad}}):
        verdict = f8.verify(before=broken_23(), after=clean3(), targeted=[target(2, 3)],
                            attribution={"seam:2|3"}, **kwargs)
        assert verdict["blocked"] is True
        assert verdict["reason"] == "invalid_chapter_set"
        assert verdict["resolved"] == []


# ---------------------------------------------------------------------------
# P1 — attribution: the structural operation must have landed at the seam
# ---------------------------------------------------------------------------
def test_a_changed_chapter_without_structural_attribution_is_not_a_seam_repair():
    """🔴 `chapter_b in changed` PROVES ONLY THAT SOMETHING IN THE CHAPTER MOVED. A legacy tense
    repair or an L3 rewrite in the same chapter would satisfy it while the seam opening was
    never touched. The verifier needs the addressed-patch lane's own attribution."""
    verdict = f8.verify(before=broken_23(), after=clean3(), targeted=[target(2, 3)],
                        changed_chapters={3}, allowed_chapters={3}, attribution=set())
    assert verdict["resolved"] == []
    assert verdict["unresolved"] == ["seam:2|3"]
    assert verdict["blocked"] is True


def test_attribution_for_a_different_seam_does_not_resolve_this_one():
    verdict = f8.verify(before=broken_23(), after=clean3(), targeted=[target(2, 3)],
                        changed_chapters={3}, allowed_chapters={3},
                        attribution={"seam:1|2"})
    assert verdict["resolved"] == []
    assert verdict["blocked"] is True


def test_a_seam_whose_chapter_never_changed_is_not_resolved_even_when_attributed():
    """🔴 THE WITNESS THAT REACHES THE BYTE RULE. With attribution ABSENT, the attribution guard
    refuses first and the changed-byte requirement is never exercised — a mutant that deletes it
    survives while looking covered. Attribution present + nothing changed is the only shape that
    can fail on this rule alone."""
    verdict = f8.verify(before=broken_23(), after=clean3(), targeted=[target(2, 3)],
                        changed_chapters=set(), allowed_chapters={3},
                        attribution={"seam:2|3"})
    assert verdict["resolved"] == []
    assert verdict["unresolved"] == ["seam:2|3"]
    assert verdict["blocked"] is True


def test_a_fully_attributed_and_verified_seam_resolves_and_allows_delivery():
    verdict = f8.verify(before=broken_23(), after=clean3(), targeted=[target(2, 3)],
                        changed_chapters={3}, allowed_chapters={3},
                        attribution={"seam:2|3"})
    assert verdict["resolved"] == ["seam:2|3"]
    assert verdict["unresolved"] == []
    assert verdict["new_defects"] == []
    assert verdict["blocked"] is False


# ---------------------------------------------------------------------------
# P1 — target identity is SERVER-COMPUTED, never caller-supplied
# ---------------------------------------------------------------------------
def test_a_caller_supplied_identity_that_contradicts_its_chapter_pair_is_unproved():
    """🔴 `target["seam"]` WAS TRUSTED WHILE `seam_identity()` COERCED THROUGH `int()`. A target
    naming one seam and carrying another's chapters could resolve a defect it never touched."""
    verdict = f8.verify(before=broken_23(), after=clean3(),
                        targeted=[{"chapter_a": 2, "chapter_b": 3, "seam": "seam:1|2"}],
                        changed_chapters={3}, allowed_chapters={3},
                        attribution={"seam:2|3"})
    assert verdict["blocked"] is True
    assert verdict["reason"] == "invalid_targets"


@pytest.mark.parametrize("bad", [
    {"chapter_a": 3, "chapter_b": 2},          # reversed
    {"chapter_a": 1, "chapter_b": 3},          # not adjacent
    {"chapter_a": True, "chapter_b": 2},       # bool is an int in Python
    {"chapter_a": 1.0, "chapter_b": 2},        # float
    {"chapter_a": "1", "chapter_b": 2},        # string
    {"chapter_a": 0, "chapter_b": 1},          # zero
    {"chapter_a": 3, "chapter_b": 4},          # beyond a three-chapter book
    {"chapter_b": 3},                          # incomplete row
    "seam:2|3",                                # not a row at all
])
def test_a_malformed_target_row_is_unproved_not_ignored(bad):
    verdict = f8.verify(before=broken_23(), after=clean3(), targeted=[bad],
                        changed_chapters={3}, allowed_chapters={3},
                        attribution={"seam:2|3"})
    assert verdict["blocked"] is True
    assert verdict["reason"] == "invalid_targets"


def test_duplicate_targets_are_unproved():
    verdict = f8.verify(before=broken_23(), after=clean3(),
                        targeted=[target(2, 3), target(2, 3)],
                        changed_chapters={3}, allowed_chapters={3},
                        attribution={"seam:2|3"})
    assert verdict["blocked"] is True
    assert verdict["reason"] == "invalid_targets"


# ---------------------------------------------------------------------------
# P2 — chapter_count: NOT_APPLICABLE is only a VALID one-chapter book
# ---------------------------------------------------------------------------
def test_a_single_chapter_book_is_not_applicable():
    result = census([], chapters=1)
    assert result["applicable"] is False
    assert result["valid"] is True
    assert result["reason"] == f8.NOT_APPLICABLE


@pytest.mark.parametrize("bad", [0, -1, None, True, False, 1.0, "3", [3]])
def test_an_invalid_chapter_count_is_unproved_not_not_applicable(bad):
    """🔴 `0` AND `201` BOTH READ AS NOT_APPLICABLE, so a book the server could not frame was
    waved through as "no seam to check"."""
    result = f8.seam_census([], chapter_count=bad)
    assert result["valid"] is False
    assert result["applicable"] is True
    assert result["reason"] == "invalid_chapter_count"


def test_a_book_over_the_chapter_bound_is_unproved_and_reachable():
    """The `too_many_seams` branch was unreachable — `_is_chapter()` rejected the count first."""
    result = f8.seam_census([], chapter_count=f8.MAX_CHAPTERS + 1)
    assert result["valid"] is False
    assert result["reason"] == "too_many_seams"


def test_the_chapter_bound_itself_is_measurable():
    rows = [row(i, i + 1) for i in range(1, f8.MAX_CHAPTERS)]
    assert f8.seam_census(rows, chapter_count=f8.MAX_CHAPTERS)["valid"] is True


# ---------------------------------------------------------------------------
# census validation (kept from the first version, still required)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rows,reason", [
    (None, "absent"),
    ("seam", "not_a_list"),
    ({"chapter_a": 1}, "not_a_list"),
    ([], "wrong_length"),
    ([row(1, 2)], "wrong_length"),
    ([row(1, 2), row(2, 3), row(3, 4)], "wrong_length"),
    (["x", row(2, 3)], "not_a_row"),
    ([row(1, 3), row(2, 3)], "not_adjacent"),
    ([row(2, 3), row(1, 2)], "not_adjacent"),
    ([row(1, 2), row(1, 2)], "not_adjacent"),
    ([row(True, 2), row(2, 3)], "invalid_chapter"),
    ([row(1.0, 2), row(2, 3)], "invalid_chapter"),
    ([row(0, 1), row(2, 3)], "invalid_chapter"),
    ([row(1, 2, causal="unknown"), row(2, 3)], "invalid_status"),
    ([row(1, 2, causal=None), row(2, 3)], "invalid_status"),
    ([{"chapter_a": 1, "chapter_b": 2, "causal": "explicit"}, row(2, 3)], "missing_dimension"),
])
def test_a_census_that_cannot_be_indexed_is_unproved(rows, reason):
    result = census(rows)
    assert result["valid"] is False
    assert result["reason"] == reason
    assert result["reason"] in f8.UNPROVED_REASONS


def test_detection_targets_the_later_chapter_and_names_the_seam():
    found = f8.detect(broken_23())
    assert len(found) == 1
    assert found[0]["chapter"] == 3
    assert found[0]["seam"] == "seam:2|3"
    assert found[0]["severity"] == "high"
    assert sorted(found[0]["missing"]) == ["causal", "location", "time"]


def test_one_violation_per_seam_not_one_per_dimension():
    found = f8.detect(broken_23())
    assert len([v for v in found if v["seam"] == "seam:2|3"]) == 1


def test_an_unproved_census_detects_nothing_but_must_not_read_as_clean():
    unproved = census(None)
    assert f8.detect(unproved) == []
    verdict = f8.verify(before=unproved, after=clean3(), targeted=[],
                        changed_chapters=set(), allowed_chapters=set(), attribution=set())
    assert verdict["blocked"] is True
    assert verdict["reason"].startswith("before_")


def test_an_omitted_after_census_cannot_resolve_anything():
    verdict = f8.verify(before=broken_23(), after=census(None), targeted=[target(2, 3)],
                        changed_chapters={3}, allowed_chapters={3},
                        attribution={"seam:2|3"})
    assert verdict["resolved"] == []
    assert verdict["blocked"] is True
    assert verdict["reason"].startswith("after_")


# ---------------------------------------------------------------------------
# P2 — accounting: closed vocabulary, complete counts, enforced invariants
# ---------------------------------------------------------------------------
def _acct(**kw):
    base = {"census_before": broken_23(), "targeted": [target(2, 3)],
            "verdict": f8.verify(before=broken_23(), after=clean3(),
                                 targeted=[target(2, 3)], changed_chapters={3},
                                 allowed_chapters={3}, attribution={"seam:2|3"}),
            "attempts": 1, "provider_calls": 1, "accepted_operations": 1,
            "byte_changing": 1}
    base.update(kw)
    return f8.accounting(**base)


def test_the_accounting_block_carries_every_required_count():
    block = _acct()
    for key in ("schema_version", "seams_detected", "seams_targeted", "repair_attempts",
                "provider_calls", "accepted_operations", "byte_changing_repairs",
                "seams_resolved", "seams_unresolved", "seams_unproved",
                "collateral_chapters", "new_defects", "delivery_blocked"):
        assert key in block, key
    assert block["seams_detected"] == 1
    assert block["seams_resolved"] == 1
    assert block["delivery_blocked"] is False
    json.dumps(block)


def test_the_reason_comes_from_a_closed_vocabulary_never_caller_prose():
    """🔴 `str(verdict.get("reason"))[:64]` PERSISTED WHATEVER IT WAS HANDED."""
    forged = {"resolved": [], "unresolved": ["seam:2|3"], "unproved": [], "collateral": [],
              "new_defects": [], "blocked": True,
              "reason": "model said: 'the rooftop scene, e.g. Ha-neul walked away'"}
    block = _acct(verdict=forged)
    assert block["reason"] in f8.ACCOUNTING_REASONS
    assert "rooftop" not in json.dumps(block)
    assert "Ha-neul" not in json.dumps(block)


@pytest.mark.parametrize("kw,why", [
    ({"byte_changing": 0}, "resolved <= byte_changing"),
    ({"accepted_operations": 0}, "byte_changing <= accepted"),
    ({"attempts": 0}, "accepted <= attempts"),
    ({"provider_calls": 0}, "provider_calls >= attempts"),
])
def test_an_accounting_contradiction_blocks_delivery(kw, why):
    """🔴 AN ACCOUNTING THAT CANNOT BE COMPLETED IS ITSELF A BLOCKER. A run reporting one
    resolved seam and zero byte-changing repairs has disproved its own claim."""
    block = _acct(**kw)
    assert block["delivery_blocked"] is True, why
    assert block["reason"] == "accounting_contradiction"


def test_collateral_forces_zero_resolutions_and_a_block():
    verdict = f8.verify(before=broken_23(), after=clean3(), targeted=[target(2, 3)],
                        changed_chapters={1, 3}, allowed_chapters={3},
                        attribution={"seam:2|3"})
    block = _acct(verdict=verdict, byte_changing=1)
    assert block["collateral_chapters"] == [1]
    assert block["seams_resolved"] == 0
    assert block["delivery_blocked"] is True


def test_detected_equals_resolved_plus_unresolved_when_applicable():
    before = census([row(1, 2, causal="missing"), row(2, 3, time="missing")])
    after = census([row(1, 2), row(2, 3, time="missing")])
    verdict = f8.verify(before=before, after=after, targeted=[target(1, 2)],
                        changed_chapters={2}, allowed_chapters={2},
                        attribution={"seam:1|2"})
    block = f8.accounting(census_before=before, targeted=[target(1, 2)], verdict=verdict,
                          attempts=1, provider_calls=1, accepted_operations=1,
                          byte_changing=1)
    assert block["seams_detected"] == block["seams_resolved"] + block["seams_unresolved"]
    assert block["delivery_blocked"] is True


def test_an_unproved_census_blocks_and_reports_no_false_zero():
    verdict = f8.verify(before=census(None), after=clean3(), targeted=[],
                        changed_chapters=set(), allowed_chapters=set(), attribution=set())
    block = f8.accounting(census_before=census(None), targeted=[], verdict=verdict,
                          attempts=0, provider_calls=0, accepted_operations=0,
                          byte_changing=0)
    assert block["census_valid"] is False
    assert block["delivery_blocked"] is True


def test_identities_are_deduplicated_before_counting():
    before = census([row(1, 2, causal="missing"), row(2, 3)])
    verdict = f8.verify(before=before, after=before, targeted=[],
                        changed_chapters=set(), allowed_chapters=set(), attribution=set())
    block = f8.accounting(census_before=before, targeted=[], verdict=verdict,
                          attempts=0, provider_calls=0, accepted_operations=0,
                          byte_changing=0)
    assert block["unresolved_ids"] == ["seam:1|2"]
    assert block["seams_unresolved"] == 1


def test_an_unproved_census_blocks_even_when_the_verdict_claims_otherwise():
    """🔴 ACCOUNTING TAKES THE CENSUS AND THE VERDICT SEPARATELY, so a verdict claiming `ok` over
    a census that could not be validated is exactly the caller-supplied hazard. `verify()` would
    never produce this pair — which is why the rule needs its own witness rather than borrowing
    one that `verify()` already blocked."""
    forged = {"resolved": [], "unresolved": [], "unproved": [], "collateral": [],
              "new_defects": [], "blocked": False, "reason": "ok"}
    block = f8.accounting(census_before=census(None), targeted=[], verdict=forged,
                          attempts=0, provider_calls=0, accepted_operations=0, byte_changing=0)
    assert block["census_valid"] is False
    assert block["delivery_blocked"] is True
    assert block["reason"] == "census_unproved"


def test_a_verdict_claiming_both_collateral_and_resolutions_is_a_contradiction():
    forged = {"resolved": ["seam:2|3"], "unresolved": [], "unproved": [], "collateral": [1],
              "new_defects": [], "blocked": False, "reason": "ok"}
    block = _acct(verdict=forged)
    assert block["reason"] == "accounting_contradiction"
    assert block["delivery_blocked"] is True


def test_counts_that_do_not_add_up_are_a_contradiction():
    """A detected seam that is neither resolved nor unresolved has been lost in the arithmetic."""
    forged = {"resolved": [], "unresolved": [], "unproved": [], "collateral": [],
              "new_defects": [], "blocked": False, "reason": "ok"}
    block = _acct(verdict=forged, attempts=0, provider_calls=0, accepted_operations=0,
                  byte_changing=0)
    assert block["reason"] == "accounting_contradiction"
    assert block["delivery_blocked"] is True


def test_persisted_id_lists_are_capped_by_a_book_that_exceeds_the_cap():
    """🔴 A ONE-ID FIXTURE CANNOT TELL BOUNDED FROM UNBOUNDED. The cap needs more ids than it
    allows before its removal is observable at all."""
    count = f8.MAX_PERSISTED_IDS + 10
    rows = [row(i, i + 1, causal="missing") for i in range(1, count + 2)]
    before = f8.seam_census(rows, chapter_count=count + 2)
    assert before["valid"] is True
    verdict = f8.verify(before=before, after=before, targeted=[], changed_chapters=set(),
                        allowed_chapters=set(), attribution=set())
    block = f8.accounting(census_before=before, targeted=[], verdict=verdict, attempts=0,
                          provider_calls=0, accepted_operations=0, byte_changing=0)
    assert block["seams_unresolved"] == count + 1
    assert len(block["unresolved_ids"]) == f8.MAX_PERSISTED_IDS
    assert len(block["detected_ids"]) == f8.MAX_PERSISTED_IDS


def test_the_persisted_block_holds_no_prose_and_is_bounded():
    block = _acct()
    blob = json.dumps(block)
    assert "evidence" not in blob and "fix" not in blob and "summary" not in blob
    for key in ("detected_ids", "resolved_ids", "unresolved_ids", "collateral_chapters",
                "new_defects"):
        assert len(block[key]) <= f8.MAX_PERSISTED_IDS


# ---------------------------------------------------------------------------
# the directive the ACTUATOR actually receives (round-2 audit, P1 #1)
# ---------------------------------------------------------------------------
def test_a_violation_carries_a_directive_the_actuator_can_act_on():
    """🔴 `_narasi_structural_patch_revise` BUILDS ITS PROMPT FROM `evidence` AND `fix` ALONE.
    Without them the provider receives `- [high/chapter_boundary_break]  -> FIX:` — an empty
    instruction against a chapter it is seeing with no statement of what is wrong. Every green
    test in the suite passed anyway, because they replaced the dispatcher."""
    found = f8.detect(broken_23(), openings={3: "Ruang rapat itu penuh ketika ia masuk."})
    v = found[0]
    assert v["evidence"], "no locator reaches the actuator"
    assert v["fix"], "no directive reaches the actuator"
    assert "Ruang rapat" in v["evidence"]
    assert v["evidence"].endswith("@ch3"), "the deterministic locator is missing"
    for dimension in ("causal", "location", "time"):
        assert dimension in v["fix"], f"{dimension} is missing but never named"
    assert "summarise" in v["fix"] or "not summarise" in v["fix"]


def test_the_directive_names_only_the_dimensions_that_are_missing():
    found = f8.detect(census([row(1, 2, time="missing"), row(2, 3)]), openings={2: "Tiga minggu."})
    fix = found[0]["fix"]
    assert "time —" in fix
    assert "causal —" not in fix and "location —" not in fix


def test_the_evidence_is_bounded_and_never_the_previous_chapters_tail():
    long_opening = "kata " * 500
    found = f8.detect(broken_23(), openings={3: long_opening})
    assert len(found[0]["evidence"]) <= f8.MAX_EVIDENCE_CHARS + 8


def test_a_missing_opening_still_produces_a_usable_locator():
    found = f8.detect(broken_23(), openings={})
    assert found[0]["evidence"] == "@ch3"
    assert found[0]["fix"]


# ---------------------------------------------------------------------------
# collateral is scoped to runs that CLAIM resolutions (round-2)
# ---------------------------------------------------------------------------
def test_collateral_without_targets_does_not_block_a_provably_sound_book():
    """A server-owned late mutator touching a chapter is F6's judgement, not F8's. What F8 must
    still catch is a seam that mutator BROKE — `new_defects` does that."""
    verdict = f8.verify(before=clean3(), after=clean3(), targeted=[],
                        changed_chapters={1}, allowed_chapters=set(), attribution=set())
    assert verdict["collateral"] == [1]
    assert verdict["blocked"] is False


def test_a_seam_broken_by_an_unauthorised_late_mutation_still_blocks():
    verdict = f8.verify(before=clean3(),
                        after=census([row(1, 2, location="missing"), row(2, 3)]),
                        targeted=[], changed_chapters={1}, allowed_chapters=set(),
                        attribution=set())
    assert verdict["new_defects"] == ["seam:1|2:location"]
    assert verdict["blocked"] is True
