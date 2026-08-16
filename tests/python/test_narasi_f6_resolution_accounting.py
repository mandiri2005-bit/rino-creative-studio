"""F6 resolution accounting — every detected hard violation ends `resolved` or `unresolved`.

🔴 THE SHAPE THIS FORBIDS.

        detected → repair failed / no-op → original still delivered

   That is what v9 did, and no counter in the system said so: the legacy lane incremented
   `revised` without comparing the result to the original, so a byte-identical output counted as
   a repair. A violation is `resolved` only when a verifier proved the defect gone AND the
   chapter it lives in actually changed; everything else is `unresolved`, which blocks delivery.

🔴 THREE WAYS THE FIRST VERSION OF THIS MODULE STILL PRODUCED A FALSE `resolved`, all found by
   Rino's audit and all closed here:

   1. A verdict string was enough. `repair_attempts=0, provider_calls=0, chapters_changed=0`
      with `{"tense_drift:2": "resolved"}` returned `resolved=1, delivery_blocked=False` — the
      "unrepresentable" state, constructed directly. A resolution now has to be corroborated by
      the chapter having changed, so the counter cannot contradict the manuscript.
   2. Identity was `class + chapter`, so two DIFFERENT teleports in chapter 2 collapsed into one
      `teleport:2`. Worse than double-counting: `detected` dropped to 1, the second violation
      left the books entirely, and resolving the first allowed delivery.
   3. The tense verifier only asked whether the OLD target was still an outlier, so a repair that
      moved the drift to chapter 3, or one that deleted chapter 3, both read as resolved.

🔴 THE PARTITION INVARIANT IS THE POINT. `resolved + unresolved == detected`, always. A
   violation that was detected but never targeted — or that has no stable identity to verify by
   — is unresolved, and blocks delivery. "We didn't get to it" and "we couldn't tell" are not
   passes.

🔴 HARD-BLOCK IS THE FALLBACK, NOT THE PRODUCT. Blocking every job would satisfy the invariant
   and help nobody, so the positive path is tested first and must stay reachable.
"""
from __future__ import annotations

import pytest

import narasi_f6 as f6


def _v(vclass, chapter, **extra):
    return {"f6_class": vclass, "chapter": chapter, **extra}


def _acct(**kw):
    """`chapter_count` is REQUIRED by the accounting — a book's length is server-owned and
    without it any integer is a chapter. Tests that are not about the bound get a roomy
    default from here; the ones that ARE about it pass their own and win, because
    `setdefault` does not override."""
    kw.setdefault("chapter_count", 10)
    return f6.resolution_accounting(**kw)


def _tele(chapter, claim, **extra):
    """A teleport carries a server-owned claim; it is a multi-instance class."""
    return _v("teleport", chapter, f6_claim=claim, **extra)


# ---------------------------------------------------------------------------
# Identity: same violation before and after, DIFFERENT violations kept apart
# ---------------------------------------------------------------------------
def test_identity_is_stable_across_the_repair_that_rewrote_the_chapter():
    """🔴 WITHOUT A STABLE ID THERE IS NO ACCOUNTING. Verification runs on a manuscript whose
    bytes have changed; an identity built from evidence prose would make the same defect look
    like a NEW violation and the original would quietly leave the books."""
    before = _v("tense_drift", 2, evidence="chapter 2 is present tense")
    after = _v("tense_drift", 2, evidence="totally different wording after the rewrite")
    assert f6.violation_identity(before) == f6.violation_identity(after)


def test_two_different_teleports_in_one_chapter_are_two_violations():
    """🔴 THE COLLAPSE THAT ALLOWED DELIVERY. `class + chapter` made these one violation:
    `detected` fell to 1, the second defect vanished, and resolving the first shipped the book."""
    a = _tele(2, "hanok|office", evidence="hanok -> office")
    b = _tele(2, "office|rooftop", evidence="office -> rooftop")
    assert f6.violation_identity(a) != f6.violation_identity(b)

    out = _acct(
        detected=[a, b], targeted=[a, b],
        verdicts={f6.violation_identity(a): "resolved"},
        repair_attempts=1, provider_calls=1, changed_chapters={2})
    assert out["violations_detected"] == 2
    assert out["violations_resolved"] == 1
    assert out["violations_unresolved"] == 1
    assert out["delivery_blocked"] is True


def test_a_multi_instance_violation_without_a_server_claim_has_no_identity():
    """🔴 NO STABLE IDENTITY MEANS NO VERIFICATION IS POSSIBLE. A teleport that cannot be told
    apart from the next one cannot be tracked across a repair, so it must not be verifiable —
    and it must still be counted, as unresolved."""
    assert f6.violation_identity(_v("teleport", 2)) is None

    orphan = _v("teleport", 2, evidence="somewhere to somewhere")
    out = _acct(detected=[orphan], targeted=[orphan], verdicts={},
                                   repair_attempts=1, provider_calls=1, changed_chapters={2})
    assert out["violations_detected"] == 1
    assert out["violations_unresolved"] == 1
    assert out["delivery_blocked"] is True


def test_two_unidentifiable_violations_are_still_two():
    """They cannot be merged either — merging on "both unknown" is the same loss again."""
    out = _acct(
        detected=[_v("teleport", 2, evidence="a"), _v("teleport", 2, evidence="b")],
        targeted=[], verdicts={}, repair_attempts=0, provider_calls=0, changed_chapters=set())
    assert out["violations_detected"] == 2
    assert out["violations_unresolved"] == 2


def test_single_instance_classes_need_no_claim():
    """A chapter has ONE dominant tense and ONE word count, so class+chapter identifies them."""
    assert f6.violation_identity(_v("tense_drift", 2)) == "tense_drift:2"
    assert f6.violation_identity(_v("chapter_ceiling", 2)) == "chapter_ceiling:2"


def test_a_single_instance_identity_ignores_any_claim_that_appears():
    """🔴 AUDIT REPRO. The same violation changed identity the moment a claim was attached
    (`tense_drift:2` → `tense_drift:2:same`), so a detection and its post-repair verification
    could be about "different" violations and the original would leave the books."""
    bare = f6.violation_identity(_v("tense_drift", 2))
    with_claim = f6.violation_identity(_v("tense_drift", 2, f6_claim="same"))
    other_claim = f6.violation_identity(_v("tense_drift", 2, f6_claim="something else"))
    assert bare == with_claim == other_claim == "tense_drift:2"


@pytest.mark.parametrize("chapter", [0, -1, None, "2", 2.0, True, False])
def test_a_chapter_that_is_not_a_1_based_integer_is_unidentifiable(chapter):
    """🔴 AUDIT REPRO. `tense_drift:0` could be "repaired" by a change set containing 0 and
    then opened delivery. Chapters are 1-based everywhere else in this system; anything else
    is a malformed finding, and a malformed finding cannot be verified."""
    assert f6.violation_identity(_v("tense_drift", chapter)) is None


def test_an_illegal_chapter_still_blocks_delivery():
    """Unidentifiable is not invisible: it is counted, and it is unresolved."""
    detected = [_v("tense_drift", 0)]
    out = _acct(
        detected=detected, targeted=detected, verdicts={"tense_drift:0": "resolved"},
        repair_attempts=1, provider_calls=1, changed_chapters={0})
    assert out["violations_detected"] == 1
    assert out["violations_unresolved"] == 1
    assert out["delivery_blocked"] is True


def test_identity_separates_classes_and_chapters():
    ids = {
        f6.violation_identity(_v("tense_drift", 2)),
        f6.violation_identity(_v("tense_drift", 3)),
        f6.violation_identity(_tele(2, "x")),
    }
    assert len(ids) == 3


def test_a_violation_without_a_class_has_no_identity():
    assert f6.violation_identity({"chapter": 2}) is None
    assert f6.violation_identity(None) is None


# ---------------------------------------------------------------------------
# A resolution must be corroborated by a repair that actually happened
# ---------------------------------------------------------------------------
def test_a_verdict_alone_cannot_resolve_anything():
    """🔴 THE AUDIT REPRO. Nothing was attempted, nothing was called, nothing changed — and the
    books said resolved. A verdict is a claim about a repair; without the repair it is nothing."""
    detected = [_v("tense_drift", 2)]
    out = _acct(
        detected=detected, targeted=detected,
        verdicts={"tense_drift:2": "resolved"},
        repair_attempts=0, provider_calls=0, changed_chapters=set())
    assert out["violations_resolved"] == 0
    assert out["violations_unresolved"] == 1
    assert out["delivery_blocked"] is True


def test_a_repair_that_ran_and_changed_nothing_resolves_nothing():
    """🔴 v9's EXACT FAILURE, WITH EVERY COUNTER LEGITIMATE. A repair was attempted, a provider
    was called, and the bytes came back identical. The attempt counters cannot catch this —
    they are all non-zero and honest — so the only thing standing between a no-op and a
    `resolved` verdict is the corroboration that the violation's OWN chapter is in the observed
    change set.

    🔴 AND THAT IS WHY THIS ROW EXISTS SEPARATELY FROM `test_a_verdict_alone_cannot_resolve_
    anything`. That one reports zero attempts, which the repair-attempt gate refuses on its own
    — so it cannot see whether the change-set check is still there."""
    detected = [_v("tense_drift", 2)]
    out = _acct(
        detected=detected, targeted=detected, verdicts={"tense_drift:2": "resolved"},
        repair_attempts=1, provider_calls=1, changed_chapters=set())
    assert out["violations_resolved"] == 0, out
    assert out["violations_unresolved"] == 1
    assert out["delivery_blocked"] is True


def test_a_resolution_needs_ITS_OWN_chapter_to_have_changed():
    """Repairing chapter 3 does not resolve a violation in chapter 2, however green the verdict.

    TWO targets, one repaired: with a single target the collateral rule would refuse the run
    outright and this rule would never be reached — the mutant that deleted it survived against
    exactly that weaker fixture."""
    a = _v("tense_drift", 2)
    b = _v("tense_drift", 3)
    out = _acct(
        detected=[a, b], targeted=[a, b],
        verdicts={"tense_drift:2": "resolved", "tense_drift:3": "resolved"},
        repair_attempts=1, provider_calls=1, changed_chapters={3})
    assert out["collateral_chapters"] == []
    assert out["violations_resolved"] == 1, "only chapter 3 was actually repaired"
    assert out["unresolved_ids"] == ["tense_drift:2"]
    assert out["delivery_blocked"] is True


def test_the_positive_path_resolves_every_target_and_allows_delivery():
    detected = [_v("tense_drift", 2), _v("final_beat", 3, f6_claim="outline_beat:3|2")]
    out = _acct(
        detected=detected, targeted=detected,
        verdicts={f6.violation_identity(v): "resolved" for v in detected},
        repair_attempts=2, provider_calls=2, changed_chapters={2, 3})
    assert out["violations_detected"] == 2
    assert out["violations_targeted"] == 2
    assert out["violations_resolved"] == 2
    assert out["violations_unresolved"] == 0
    assert out["delivery_blocked"] is False
    assert out["chapters_changed"] == 2
    assert out["provider_calls"] == 2


def test_chapters_changed_is_derived_so_it_cannot_contradict_the_repairs():
    """One source of truth: the set of chapters that changed. A separate integer could disagree
    with it, and the disagreeing copy is the one a false resolution hides behind."""
    out = _acct(detected=[], targeted=[], verdicts={},
                                   repair_attempts=3, provider_calls=3,
                                   changed_chapters={2, 3, 5})
    assert out["chapters_changed"] == 3


# ---------------------------------------------------------------------------
# ...and the counters must not contradict each other
# ---------------------------------------------------------------------------
def test_a_chapter_that_changed_without_a_repair_resolves_nothing():
    """🔴 AUDIT REPRO. `repair_attempts=0, provider_calls=0, changed_chapters={2}` resolved the
    violation and allowed delivery: membership in a set the CALLER supplied was the whole
    check, so a caller could assert a repair that never happened.

    🔴 AND IT IS A REFUSAL TO RESOLVE, NOT A RAISE. The first fix raised here — but a chapter
    CAN change without F6 repairing it: the post-gates dedup guard, the canon-lite assist repair
    and the F1 scrub all rewrite the manuscript after detection, and none of them is an F6
    attempt. Raising refused clean books those mutators touched, naming the wrong cause. The
    rule belongs on the resolution: nothing may be vouched for on a change no repair produced."""
    detected = [_v("tense_drift", 2)]
    out = _acct(
        detected=detected, targeted=detected, verdicts={"tense_drift:2": "resolved"},
        repair_attempts=0, provider_calls=0, changed_chapters={2})
    assert out["violations_resolved"] == 0, out
    assert out["violations_unresolved"] == 1
    assert out["delivery_blocked"] is True


def test_a_book_nobody_repaired_but_something_else_edited_is_not_an_error():
    """The other half: with nothing detected, a post-gates edit is not a broken instrument and
    must not fail the accounting — it is simply a change F6 cannot vouch for."""
    out = _acct(detected=[], targeted=[], verdicts={},
                repair_attempts=0, provider_calls=0, changed_chapters={2})
    assert out["violations_detected"] == 0
    assert out["delivery_blocked"] is False


def test_a_repair_attempt_cannot_happen_without_a_provider_call():
    detected = [_v("tense_drift", 2)]
    with pytest.raises(f6.F6AccountingError):
        _acct(
            detected=detected, targeted=detected, verdicts={"tense_drift:2": "resolved"},
            repair_attempts=1, provider_calls=0, changed_chapters={2})


def test_a_repair_that_rewrote_chapters_nobody_targeted_resolves_nothing():
    """🔴 AUDIT REPRO. One target in chapter 2 with `changed_chapters={1,2,3}` resolved and
    shipped. Targeted repair means the chapters nobody targeted come back byte-identical; a
    lane that rewrote all three either overreached or reported someone else's edit as its own.
    Neither is a repair of chapter 2, and neither may open delivery."""
    detected = [_v("tense_drift", 2)]
    out = _acct(
        detected=detected, targeted=detected, verdicts={"tense_drift:2": "resolved"},
        repair_attempts=1, provider_calls=1, changed_chapters={1, 2, 3})
    assert out["collateral_chapters"] == [1, 3]
    assert out["violations_resolved"] == 0
    assert out["violations_unresolved"] == 1
    assert out["delivery_blocked"] is True


def test_a_repair_confined_to_its_targets_is_accepted():
    """The positive path must stay reachable: two targets, two chapters changed, nothing else."""
    detected = [_v("tense_drift", 2), _v("chapter_ceiling", 3)]
    out = _acct(
        detected=detected, targeted=detected,
        verdicts={"tense_drift:2": "resolved", "chapter_ceiling:3": "resolved"},
        repair_attempts=2, provider_calls=2, changed_chapters={2, 3})
    assert out["collateral_chapters"] == []
    assert out["violations_resolved"] == 2
    assert out["delivery_blocked"] is False


def test_a_chapter_beyond_the_end_of_the_book_cannot_be_resolved():
    """🔴 AUDIT REPRO. `tense_drift:999` with `changed_chapters={999}` resolved and shipped a
    three-chapter book. `chapter >= 1` was the only bound, so any number was a chapter. The
    accounting now takes a SERVER-OWNED `chapter_count` and refuses anything outside it."""
    detected = [_v("tense_drift", 999)]
    out = _acct(
        detected=detected, targeted=detected, verdicts={"tense_drift:999": "resolved"},
        repair_attempts=1, provider_calls=1, changed_chapters=set(), chapter_count=3)
    assert out["violations_detected"] == 1
    assert out["violations_resolved"] == 0
    assert out["violations_unresolved"] == 1
    assert out["delivery_blocked"] is True
    assert out["violations_unidentifiable"] == 1, (
        "a finding about a chapter the book does not have is MALFORMED, and saying so is the "
        "observable difference — without it the count is right for the wrong reason")


def test_a_change_set_naming_a_chapter_the_book_does_not_have_is_a_broken_instrument():
    """A real byte comparison cannot report chapter 999 of a three-chapter book."""
    with pytest.raises(f6.F6AccountingError):
        _acct(detected=[], targeted=[], verdicts={},
                                 repair_attempts=1, provider_calls=1,
                                 changed_chapters={999}, chapter_count=3)


@pytest.mark.parametrize("bad_count", [0, -1, True, 1.5, "3", None])
def test_the_chapter_count_itself_must_be_a_positive_integer(bad_count):
    with pytest.raises(f6.F6AccountingError):
        _acct(detected=[], targeted=[], verdicts={},
                                 repair_attempts=0, provider_calls=0,
                                 changed_chapters=set(), chapter_count=bad_count)


@pytest.mark.parametrize("bad", [-7, -1, True, False, 1.5, "3", None])
def test_counters_must_be_non_negative_integers(bad):
    """🔴 AUDIT REPRO. `repair_attempts=-7, provider_calls=-9` were published verbatim. A
    negative count is not a small count; it is a broken instrument, and so is a bool or a
    string that happens to compare."""
    with pytest.raises(f6.F6AccountingError):
        _acct(detected=[], targeted=[], verdicts={},
                                 repair_attempts=bad, provider_calls=1,
                                 changed_chapters=set())
    with pytest.raises(f6.F6AccountingError):
        _acct(detected=[], targeted=[], verdicts={},
                                 repair_attempts=0, provider_calls=bad,
                                 changed_chapters=set())


def test_consistent_counters_are_accepted():
    detected = [_v("tense_drift", 2)]
    out = _acct(
        detected=detected, targeted=detected, verdicts={"tense_drift:2": "resolved"},
        repair_attempts=1, provider_calls=1, changed_chapters={2})
    assert out["violations_resolved"] == 1 and out["delivery_blocked"] is False


# ---------------------------------------------------------------------------
# ...and the changed set is derived from bytes, not asserted
# ---------------------------------------------------------------------------
def test_changed_chapters_comes_from_a_server_owned_byte_comparison():
    """🔴 THE SET MUST BE OBSERVED, NOT CLAIMED. A repair that returns the original bytes has
    changed nothing, whatever the lane reports — that conflation is v9's `revised=1` exactly."""
    before = ["## Chapter 1\n\na\n\n", "## Chapter 2\n\nb\n\n", "## Chapter 3\n\nc"]
    after = ["## Chapter 1\n\na\n\n", "## Chapter 2\n\nB CHANGED\n\n", "## Chapter 3\n\nc"]
    assert f6.changed_chapters_from_blocks(before, after) == {2}
    assert f6.changed_chapters_from_blocks(before, before) == set()


def test_a_preamble_does_not_shift_the_chapter_numbering():
    """🔴 AUDIT REPRO. `split_chapter_blocks` returns a leading preamble as its own block, and
    the comparison numbered every block as a chapter — so editing Chapter 2 of a book with a
    preamble reported `{3}`. Off-by-one here points every repair and every verdict at the wrong
    chapter."""
    import narasi_gate as ng
    book = "> **Gaya:** storytelling\n\n## Chapter 1\n\na\n\n## Chapter 2\n\nb"
    before = ng.split_chapter_blocks(book)
    assert len(before) == 3, "preamble + two chapters"

    after = list(before)
    after[2] = after[2].replace("b", "B")
    assert f6.changed_chapters_from_blocks(before, after) == {2}


def test_a_changed_preamble_is_not_attributed_to_any_chapter():
    """It belongs to no chapter, so it appears in no chapter's change set.

    🔴 AND IT MUST NOT POISON THE COMPARISON EITHER. An earlier version returned None here, on
    the reasoning that an unattributable change means nothing can be vouched for. That rule
    blocked EVERY real job: the gates append a `> **Gaya:** …` metadata header to the assembled
    book AFTER the pre-repair snapshot is taken, so a preamble legitimately exists on one side
    and not the other, and the comparison collapsed on all of them. The preamble is
    server-generated framing, not manuscript; chapters are compared, it is not."""
    import narasi_gate as ng
    # 🔴 BOTH SIDES CARRY A REAL SERVER HEADER. With a bare `Gaya: …` on the left the
    # is-this-our-framing rule refuses first, and this row would then prove nothing about the
    # header having CHANGED — which is the rule it exists for.
    book = "> **Gaya:** storytelling\n\n## Chapter 1\n\na\n\n## Chapter 2\n\nb"
    before = ng.split_chapter_blocks(book)
    after = list(before)
    after[0] = "> **Gaya:** harari\n\n"
    # 🔴 THE RULE TIGHTENED, AND THIS ROW SAYS SO. Reporting `set()` — "nothing changed" — was
    # the audit's finding: a preamble edit was silently ignored, so a repair could prepend
    # arbitrary text and the comparison would not see it. "Not attributed to any chapter" is
    # now expressed as CANNOT BE DETERMINED, which is still not a chapter edit and additionally
    # refuses delivery.
    assert f6.changed_chapters_from_blocks(before, after) is None


def test_a_preamble_appearing_on_only_one_side_does_not_break_the_comparison():
    """Exactly the production shape: no header before the gates, a header after them.

    BOTH directions are checked. Trimming the two sides jointly instead of independently
    happens to work when only the AFTER side has a preamble, so a one-directional fixture
    lets that defect through."""
    import narasi_gate as ng
    header = "> **Gaya:** Storytelling\n\n---\n\n"
    chapters = "## Chapter 1\n\nalpha\n\n## Chapter 2\n\nbeta"
    bare = ng.split_chapter_blocks(chapters)
    headed = ng.split_chapter_blocks(header + chapters)

    assert f6.changed_chapters_from_blocks(bare, headed) == set()
    assert f6.changed_chapters_from_blocks(headed, bare) == set()

    edited = ng.split_chapter_blocks(header + chapters.replace("\n\nbeta", "\n\nBETA"))
    assert f6.changed_chapters_from_blocks(bare, edited) == {2}
    assert f6.changed_chapters_from_blocks(
        headed, ng.split_chapter_blocks(chapters.replace("\n\nbeta", "\n\nBETA"))) == {2}


def test_a_book_without_a_preamble_still_numbers_from_one():
    import narasi_gate as ng
    book = "## Chapter 1\n\nalpha\n\n## Chapter 2\n\nbeta"
    before = ng.split_chapter_blocks(book)
    after = list(before)
    after[0] = after[0].replace("\n\nalpha", "\n\nALPHA")
    assert f6.changed_chapters_from_blocks(before, after) == {1}


def test_a_rewritten_heading_is_a_structural_failure_not_a_chapter_edit():
    """🔴 AUDIT REPRO. `## Chapter 2` → `## Bab 99` compared as an ordinary block difference, so
    the change set said `{2}`, the verifier said resolved, and delivery opened — on a book whose
    chapter heading had been destroyed. Headings are server-owned framing; a repair may rewrite
    prose, never the heading."""
    import narasi_gate as ng
    book = "## Chapter 1\n\na\n\n## Chapter 2\n\nb\n\n## Chapter 3\n\nc"
    before = ng.split_chapter_blocks(book)

    for corrupted in ("## Bab 99", "## Chapter 2 — Revised", "Chapter 2"):
        after = list(before)
        after[1] = after[1].replace("## Chapter 2", corrupted)
        assert f6.changed_chapters_from_blocks(before, after) is None, corrupted


def test_a_lost_heading_is_a_structural_failure():
    import narasi_gate as ng
    before = ng.split_chapter_blocks("## Chapter 1\n\na\n\n## Chapter 2\n\nb")
    after = list(before)
    after[1] = after[1].replace("## Chapter 2\n\n", "")
    assert f6.changed_chapters_from_blocks(before, after) is None


def test_reordered_chapters_are_a_structural_failure():
    import narasi_gate as ng
    before = ng.split_chapter_blocks("## Chapter 1\n\na\n\n## Chapter 2\n\nb")
    assert f6.changed_chapters_from_blocks(before, [before[1], before[0]]) is None


def test_a_prose_only_edit_under_an_intact_heading_is_still_an_ordinary_edit():
    """The positive path must stay reachable: rewriting the prose is exactly what a repair does."""
    import narasi_gate as ng
    before = ng.split_chapter_blocks("## Chapter 1\n\na\n\n## Chapter 2\n\nb")
    after = list(before)
    after[1] = after[1].replace("\n\nb", "\n\nB REWRITTEN")
    assert f6.changed_chapters_from_blocks(before, after) == {2}


def test_a_block_count_change_is_not_silently_reported_as_a_chapter_edit():
    """Losing or gaining a chapter is not "chapter N changed" — it is a structural break, and
    reporting it as an edit would let a deletion look like a repair."""
    before = ["## Chapter 1\n\na\n\n", "## Chapter 2\n\nb"]
    assert f6.changed_chapters_from_blocks(before, before[:1]) is None
    assert f6.changed_chapters_from_blocks(before, before + ["## Chapter 3\n\nc"]) is None


def test_an_undetectable_change_set_blocks_rather_than_resolves():
    """When the comparison cannot be made, nothing may be vouched for."""
    detected = [_v("tense_drift", 2)]
    out = _acct(
        detected=detected, targeted=detected, verdicts={"tense_drift:2": "resolved"},
        repair_attempts=1, provider_calls=1, changed_chapters=None)
    assert out["violations_resolved"] == 0
    assert out["delivery_blocked"] is True


# ---------------------------------------------------------------------------
# The partition
# ---------------------------------------------------------------------------
def test_a_failed_repair_is_unresolved_and_blocks_delivery():
    detected = [_v("tense_drift", 2)]
    out = _acct(
        detected=detected, targeted=detected, verdicts={"tense_drift:2": "unresolved"},
        repair_attempts=1, provider_calls=1, changed_chapters={2})
    assert out["violations_resolved"] == 0
    assert out["violations_unresolved"] == 1
    assert out["delivery_blocked"] is True


def test_a_detected_but_never_targeted_violation_is_unresolved_not_forgotten():
    """🔴 "We didn't get to it" must not be a silent pass."""
    detected = [_v("tense_drift", 2), _tele(2, "hanok|office")]
    out = _acct(
        detected=detected, targeted=[detected[0]],
        verdicts={"tense_drift:2": "resolved"},
        repair_attempts=1, provider_calls=1, changed_chapters={2})
    assert out["violations_targeted"] == 1
    assert out["violations_resolved"] == 1
    assert out["violations_unresolved"] == 1
    assert out["delivery_blocked"] is True


def test_a_missing_verdict_is_unresolved_never_assumed_resolved():
    detected = [_v("tense_drift", 2)]
    out = _acct(detected=detected, targeted=detected, verdicts={},
                                   repair_attempts=1, provider_calls=1, changed_chapters={2})
    assert out["violations_unresolved"] == 1
    assert out["delivery_blocked"] is True


def test_an_unknown_verdict_string_is_unresolved():
    detected = [_v("tense_drift", 2)]
    for bogus in ("ok", "passed", "", None, True):
        out = _acct(
            detected=detected, targeted=detected, verdicts={"tense_drift:2": bogus},
            repair_attempts=1, provider_calls=1, changed_chapters={2})
        assert out["violations_unresolved"] == 1, bogus


def test_nothing_detected_means_nothing_blocked():
    out = _acct(detected=[], targeted=[], verdicts={},
                                   repair_attempts=0, provider_calls=0, changed_chapters=set())
    assert out["violations_detected"] == 0
    assert out["delivery_blocked"] is False


def test_the_partition_invariant_holds_on_every_mix():
    detected = [_v("tense_drift", 2), _tele(3, "a|b"), _v("final_beat", 4, f6_claim="b:4|1")]
    out = _acct(
        detected=detected, targeted=detected[:2],
        verdicts={f6.violation_identity(detected[0]): "resolved",
                  f6.violation_identity(detected[1]): "unresolved"},
        repair_attempts=2, provider_calls=3, changed_chapters={2})
    assert out["violations_resolved"] + out["violations_unresolved"] \
        == out["violations_detected"] == 3


def test_a_duplicate_detection_is_counted_once():
    """Two sightings of ONE violation — same class, same chapter, same claim — are one slot."""
    detected = [_v("tense_drift", 2), _v("tense_drift", 2, evidence="said twice")]
    out = _acct(
        detected=detected, targeted=detected, verdicts={"tense_drift:2": "resolved"},
        repair_attempts=1, provider_calls=1, changed_chapters={2})
    assert out["violations_detected"] == 1
    assert out["violations_resolved"] == 1
    assert out["delivery_blocked"] is False


def test_the_accounting_refuses_to_publish_a_broken_partition():
    """🔴 THE INSTRUMENT MUST FAIL LOUDLY rather than publish numbers that do not add up."""
    with pytest.raises(f6.F6AccountingError):
        _acct(detected=[_v("tense_drift", 2)], targeted=[], verdicts={},
                                 repair_attempts=0, provider_calls=0,
                                 changed_chapters=set(), _force_resolved=99)


# ---------------------------------------------------------------------------
# The deterministic tense verifier
# ---------------------------------------------------------------------------
BEFORE_V9 = ["past", "present", "past"]
_DEFAULT = object()          # so `before=None` can BE a case, not a request for the default


def _ok(after, *, before=_DEFAULT, chapter=2, count=3, changed=(2,)):
    return f6.verify_tense_resolved(BEFORE_V9 if before is _DEFAULT else before, after,
                                    chapter=chapter, chapter_count=count,
                                    changed_chapters=changed)


def test_tense_is_resolved_only_when_the_whole_census_is_consistent():
    assert _ok(["past", "past", "past"]) is True
    assert _ok(["past", "present", "past"]) is False


def test_the_before_census_is_computed_not_claimed():
    """🔴 AUDIT REPRO. The verifier used to take `outliers_before` as a caller's word, so a
    caller could hand it `(2, 3)` and a drift that MOVED to chapter 3 was accepted as
    pre-existing. The set is now derived from the before-census the server itself read."""
    assert _ok(["past", "past", "present"]) is False


def test_relabelling_a_chapter_nobody_touched_is_not_a_resolution():
    """🔴 AUDIT REPRO. `["present","present","present"]` passed while chapters 1 and 3 were
    supposed to be BYTE-IDENTICAL: their tense labels changed, so either the repair edited
    chapters it must not have or the census is unreliable. Either way nothing may be vouched
    for. An unchanged chapter must keep the label it had."""
    assert _ok(["present", "present", "present"]) is False


def test_a_chapter_outside_the_change_set_may_not_drift_either_way():
    for after in (["mixed", "past", "past"], ["past", "past", "mixed"]):
        assert _ok(after) is False, after


def test_moving_the_drift_to_another_chapter_is_not_a_resolution():
    """`["past","past","present"]` said resolved while chapter 3 now drifts.

    Chapter 3 is INSIDE the change set here, so the unchanged-label rule does not apply and the
    new-outlier rule is the only thing that can catch it — with `changed=(2,)` the label rule
    fires first and masks it, which is how the mutant that deleted this rule survived."""
    assert f6.verify_tense_resolved(BEFORE_V9, ["past", "past", "present"],
                                    chapter=2, chapter_count=3,
                                    changed_chapters=(2, 3)) is False


def test_losing_a_chapter_is_not_a_resolution():
    """`["past","past"]` said resolved for a three-chapter book: chapter 3 had been deleted."""
    assert _ok(["past", "past"]) is False


def test_a_pre_existing_outlier_elsewhere_does_not_block_its_own_target():
    """Precision, not blanket strictness: chapter 4 already drifted BEFORE the repair, so it is
    its own unresolved violation — it must not also make chapter 2's genuine repair read as
    failed. Its label is unchanged, which is exactly what makes it pre-existing.

    Five chapters, not four: with two of each there is no strict majority and hence no outlier
    to repair at all — a four-chapter fixture would be testing the no-majority rule instead."""
    assert f6.verify_tense_resolved(
        ["past", "present", "past", "present", "past"],
        ["past", "past", "past", "present", "past"],
        chapter=2, chapter_count=5, changed_chapters=(2,)) is True


def test_a_target_that_was_not_an_outlier_before_is_not_this_violation():
    """If the before-census never flagged the chapter, resolving it is a claim about a defect
    that was not there."""
    assert f6.verify_tense_resolved(["past", "past", "past"], ["past", "past", "past"],
                                    chapter=2, chapter_count=3,
                                    changed_chapters=(2,)) is False


def test_a_book_with_no_dominant_tense_is_not_a_resolution():
    """With no strict majority the census reports NO outliers — and "no outliers" was read as
    "consistent". A book split two-and-two has no dominant tense at all; that IS the drift."""
    assert f6.verify_tense_resolved(
        ["past", "present", "past", "past"], ["past", "past", "present", "present"],
        chapter=2, chapter_count=4, changed_chapters=(2, 3, 4)) is False


def test_a_repair_that_makes_the_book_wholly_consistent_is_a_resolution():
    """Even if the majority tense itself moved — but only when every chapter really changed."""
    assert f6.verify_tense_resolved(
        BEFORE_V9, ["present", "present", "present"],
        chapter=2, chapter_count=3, changed_chapters=(1, 2, 3)) is True


def test_a_chapter_that_changed_but_still_drifts_is_not_resolved():
    """🔴 "THE CHAPTER CHANGED" IS NOT "THE DEFECT IS GONE" — v9's exact claim."""
    assert _ok(["past", "mixed", "past"]) is False


def test_a_census_that_cannot_be_read_after_repair_is_not_a_resolution():
    for unreadable in (None, [], ["past", "future", "past"], "past"):
        assert _ok(unreadable) is False


def test_a_before_census_that_cannot_be_read_vouches_for_nothing():
    for unreadable in (None, [], ["past", "future", "past"], "past"):
        assert _ok(["past", "past", "past"], before=unreadable) is False


def test_the_verifier_refuses_a_census_that_no_longer_covers_the_chapter():
    assert f6.verify_tense_resolved(["past", "present"], ["past", "past"], chapter=3,
                                    chapter_count=2, changed_chapters=(3,)) is False
