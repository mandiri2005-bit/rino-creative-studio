"""F6 — the four classes added after `tense_drift`, at the level of their own arithmetic.

The golden suite proves what the JOB does with these classes. This file pins the pieces that
job assembles: the two new censuses, the server-owned beat claim, the word-count measurement,
and the three new verifiers. Every row here is a rule some earlier version got wrong, or a rule
whose absence a mutant would otherwise be free to exploit.

🔴 THE RULE THAT GOVERNS ALL FOUR VERIFIERS. A verifier answers exactly one question — "is THIS
   violation provably gone from the bytes being delivered?" — and every other outcome is False.
   Not "probably", not "the chapter changed", not "the model says so". `False` here means the
   violation stays unresolved, and unresolved blocks delivery.
"""
from __future__ import annotations

import sys

import pytest


def _live(name):
    import importlib
    return sys.modules.get(name, importlib.import_module(name))


ng = _live("narasi_gate")
nf6 = _live("narasi_f6")


# ---------------------------------------------------------------------------
# chapter_word_counts — the one observation with no model in it
# ---------------------------------------------------------------------------
BOOK3 = ("## Chapter 1: A\n\nsatu dua tiga\n\n"
         "## Chapter 2: B\n\nsatu dua tiga empat lima\n\n"
         "## Chapter 3: C\n\nsatu\n")


def test_word_counts_are_one_entry_per_chapter_in_order():
    assert ng.chapter_word_counts(BOOK3) == [3, 5, 1]


def test_the_heading_is_not_counted_as_prose():
    """🔴 THE GATES LOCALISE `## Chapter 2` TO `## Bab 2`. If the heading counted, a chapter
    would cross its ceiling for a reason that has nothing to do with its prose."""
    localised = BOOK3.replace("## Chapter 2: B", "## Bab 2: Sebuah Judul Yang Jauh Lebih Panjang")
    assert ng.chapter_word_counts(localised) == ng.chapter_word_counts(BOOK3)


def test_a_preamble_is_not_counted_as_a_chapter():
    """The `> **Gaya:** …` header the gates prepend is server framing, not chapter one."""
    assert ng.chapter_word_counts("> **Gaya:** santai\n\n" + BOOK3) == [3, 5, 1]


def test_a_text_with_no_headings_has_no_chapter_counts():
    assert ng.chapter_word_counts("prosa tanpa judul apa pun") == []
    assert ng.chapter_word_counts("") == []


# ---------------------------------------------------------------------------
# Heading grammar — a keyword is not a heading
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("line", [
    "Bab ini dimulai dengan tenang dan tidak ada yang berubah.",
    "Chapter and verse, he said, and closed the book.",
    "## Bab nonsense",
    "## Chapter: The Reveal",
    "Bab",
    "## Bab -1",
])
def test_a_keyword_without_a_valid_ordinal_is_not_a_heading(line):
    """🔴 THE GRAMMAR ASKED FOR A KEYWORD AND THEN ACCEPTED FREE TEXT. `"Bab ini dimulai dengan
    tenang."` is an ordinary Indonesian sentence and it matched — splitting a chapter in half,
    so every chapter count, every census index and every repair target after it pointed at the
    wrong place. A heading carries an ORDINAL; a sentence that happens to open with the word
    does not."""
    assert ng.chapter_heading_line(line) == "", line
    assert ng._CHAPTER_SPLIT_RX.search(line) is None, line


@pytest.mark.parametrize("line", [
    "## Chapter 1: The Return", "## Bab 2", "Chapter 3", "### Hoofdstuk 4",
    "## Capítulo 5: El Regreso", "## Kapitel 6", "## Chapitre 7", "## Bab 08",
])
def test_a_real_heading_is_still_a_heading(line):
    """The positive control: tightening the grammar must not stop recognising what production
    actually emits (`## <Keyword> <N>[: Title]`, `orchestrator/static.py::_chapter_md`)."""
    assert ng.chapter_heading_line(line) == line, line


def test_the_ordinal_sequence_is_read_from_the_headings():
    book = "\n\n".join(f"## Chapter {i}: T\n\nprosa" for i in (1, 2, 3))
    assert ng.chapter_ordinal_sequence(book) == [1, 2, 3]


@pytest.mark.parametrize("numbers,ok", [
    ((1, 2, 3), True), ((1,), True),
    ((1, 3, 2), False),      # reordered
    ((1, 2, 4), False),      # a chapter went missing
    ((2, 3, 4), False),      # the book does not start at one
    ((1, 2, 2), False),      # duplicated
])
def test_headings_must_run_1_to_N_in_order(numbers, ok):
    """🔴 MISSING, EXTRA, DUPLICATED OR REORDERED — all four are the same structural failure.
    A manuscript whose chapters are not 1..N in order cannot be indexed by a census, and every
    per-chapter verdict computed against it is about the wrong chapter."""
    book = "\n\n".join(f"## Chapter {n}: T\n\nprosa" for n in numbers)
    assert ng.chapter_headings_well_formed(book) is ok, numbers


def test_an_unreadable_heading_is_simply_not_a_heading():
    """🔴 AND THAT IS THE WHOLE CONSEQUENCE, DELIBERATELY. `## Chapter nonsense` does not become
    a malformed chapter — it stops being a chapter boundary at all, so the book has ONE chapter
    where the request asked for two. The structural failure surfaces as a chapter-count
    mismatch, which is the expected-count rule's job; inventing a second refusal here would be
    two mechanisms for one rule."""
    book = "## Chapter 1: T\n\nprosa\n\n## Chapter nonsense\n\nprosa"
    assert ng.chapter_ordinal_sequence(book) == [1]
    assert len(ng.chapter_word_counts(book)) == 1
    assert "## Chapter nonsense" in ng.split_chapter_blocks(book)[0]


def test_a_book_with_no_headings_at_all_is_not_well_formed():
    assert ng.chapter_headings_well_formed("prosa tanpa judul") is False
    assert ng.chapter_headings_well_formed("") is False


# ---------------------------------------------------------------------------
# teleport_census — a count the server can do arithmetic on
# ---------------------------------------------------------------------------
def test_a_clean_teleport_census_names_no_offender():
    out = ng.teleport_census([0, 0, 0], chapter_count=3)
    assert out["valid"] is True and out["offenders"] == []


def test_offenders_are_1_based_chapters():
    out = ng.teleport_census([0, 2, 0, 1], chapter_count=4)
    assert out["offenders"] == [2, 4]


@pytest.mark.parametrize("bad,reason", [
    ([0, "1", 0], "invalid_value"),
    ([0, -1, 0], "invalid_value"),
    ([0, None, 0], "invalid_value"),
    ([0, 1.5, 0], "invalid_value"),
    ([0, 999, 0], "implausible_count"),
])
def test_a_malformed_teleport_census_is_refused_not_repaired(bad, reason):
    out = ng.teleport_census(bad, chapter_count=3)
    assert out["valid"] is False and out["reason"] == reason
    assert out["offenders"] == [], "an invalid census must target nothing"


def test_a_bool_is_not_a_count():
    """🔴 `True == 1` IN PYTHON. A bool that survived into the count would make "one teleport"
    and "yes there is a problem" the same value, and the arithmetic could not tell a repaired
    chapter from an unmeasured one."""
    assert ng.teleport_census([False, True, False], chapter_count=3)["valid"] is False


def test_a_teleport_census_must_cover_every_chapter():
    out = ng.teleport_census([0, 0], chapter_count=3)
    assert out["valid"] is False and out["reason"] == "chapter_count_mismatch"


def test_the_teleport_census_is_bounded():
    assert ng.teleport_census([0] * 500, chapter_count=500)["reason"] == "too_many_chapters"


def test_an_absent_teleport_census_is_not_a_clean_book():
    for absent in (None, [], "none", {}):
        assert ng.teleport_census(absent, chapter_count=3)["valid"] is False, absent


# ---------------------------------------------------------------------------
# beat_census — three-valued, keyed by the outline THIS process rendered
# ---------------------------------------------------------------------------
SIZES = {1: 2, 2: 1}
FULL = [{"chapter": 1, "beat": 1, "state": "executed"},
        {"chapter": 1, "beat": 2, "state": "promised"},
        {"chapter": 2, "beat": 1, "state": "absent"}]


def test_a_complete_beat_census_is_read_as_a_map():
    out = ng.beat_census(FULL, outline_sizes=SIZES)
    assert out["valid"] is True
    assert out["states"] == {(1, 1): "executed", (1, 2): "promised", (2, 1): "absent"}


def test_a_beat_the_outline_does_not_have_invalidates_the_census():
    """🔴 A MODEL INVENTING KEYS IS A MODEL THAT WAS NOT READING THE OUTLINE IT WAS HANDED.
    Skipping the entry instead would let an invented beat sit beside real ones as if the answer
    were sound."""
    out = ng.beat_census(FULL + [{"chapter": 9, "beat": 1, "state": "executed"}],
                         outline_sizes=SIZES)
    assert out["valid"] is False and out["reason"] == "unknown_beat"


def test_an_incomplete_beat_census_is_refused():
    """🔴 THE CLASS IS ABOUT SOMETHING MISSING. An uncovered beat would read as "not reported"
    and quietly leave the books — which is the defect, not the report of it."""
    out = ng.beat_census(FULL[:2], outline_sizes=SIZES)
    assert out["valid"] is False and out["reason"] == "incomplete_coverage"


@pytest.mark.parametrize("state", ["done", "", "EXECUTE", None, 1, True])
def test_a_state_outside_the_closed_vocabulary_is_refused(state):
    out = ng.beat_census([{"chapter": 1, "beat": 1, "state": state}] + FULL[1:],
                         outline_sizes=SIZES)
    assert out["valid"] is False


def test_two_contradictory_answers_for_one_beat_are_no_answer():
    out = ng.beat_census(FULL + [{"chapter": 2, "beat": 1, "state": "executed"}],
                         outline_sizes=SIZES)
    assert out["valid"] is False and out["reason"] == "contradictory_entry"


def test_a_repeated_but_consistent_entry_is_tolerated():
    out = ng.beat_census(FULL + [FULL[0]], outline_sizes=SIZES)
    assert out["valid"] is True


def test_no_outline_means_no_beat_census_rather_than_a_clean_one():
    """A book with no accepted outline has no beats to execute. That is not the same as a book
    whose beats were all executed, and it is reported as its own reason."""
    out = ng.beat_census(FULL, outline_sizes={})
    assert out["valid"] is False and out["reason"] == "no_outline"


def test_an_invalid_outline_keyspace_is_refused():
    assert ng.beat_census(FULL, outline_sizes={1: 0})["reason"] == "invalid_outline"
    assert ng.beat_census(FULL, outline_sizes={"1": 2})["reason"] == "invalid_outline"


# ---------------------------------------------------------------------------
# outline_beat_claim — the server-owned discriminator
# ---------------------------------------------------------------------------
def test_a_bounded_reference_becomes_a_claim():
    assert nf6.outline_beat_claim({"outline_chapter": 1, "outline_beat": 2}, SIZES) == \
        "outline_beat:1|2"


@pytest.mark.parametrize("source", [
    {"outline_chapter": 1, "outline_beat": 3},      # past the end of that chapter's beats
    {"outline_chapter": 9, "outline_beat": 1},      # a chapter with no packet
    {"outline_chapter": 1},                          # no ordinal at all
    {"outline_chapter": "1", "outline_beat": 1},     # a string is not a reference
    {"outline_chapter": True, "outline_beat": 1},    # nor is a bool
    {"outline_chapter": 0, "outline_beat": 1},
    {"outline_chapter": 1, "outline_beat": 0},
    "not a dict",
])
def test_an_unbounded_reference_yields_no_claim(source):
    """🔴 NO CLAIM MEANS NO IDENTITY MEANS UNRESOLVED. Inventing an identity for a reference
    nobody could check against the rendered packet is how a violation leaves the books."""
    assert nf6.outline_beat_claim(source, SIZES) == ""


def test_a_claimless_multi_instance_violation_has_no_identity():
    for vclass in ("teleport", "final_beat", "beat_execution"):
        assert nf6.violation_identity({"f6_class": vclass, "chapter": 2}) is None, vclass


def test_the_claim_makes_two_findings_in_one_chapter_two_violations():
    """🔴 AUDIT FINDING #2. `class + chapter` collapsed two different teleports in chapter 2
    into one, `detected` fell from two to one, and resolving the first opened delivery."""
    first = nf6.violation_identity({"f6_class": "teleport", "chapter": 2,
                                    "f6_claim": "teleport_instance:2|1"})
    second = nf6.violation_identity({"f6_class": "teleport", "chapter": 2,
                                     "f6_claim": "teleport_instance:2|2"})
    assert first and second and first != second


# ---------------------------------------------------------------------------
# verify_teleport_resolved
# ---------------------------------------------------------------------------
def _tele(before, after, *, chapter=2, changed=(2,), count=3):
    return nf6.verify_teleport_resolved(before, after, chapter=chapter, chapter_count=count,
                                        changed_chapters=changed)


def test_a_teleport_count_that_reached_zero_is_resolved():
    assert _tele([0, 1, 0], [0, 0, 0]) is True


def test_a_teleport_count_that_merely_fell_is_not_resolved():
    """Two moves becoming one leaves a teleport in the delivered book."""
    assert _tele([0, 2, 0], [0, 1, 0]) is False


def test_a_teleport_pushed_into_another_chapter_is_not_resolved():
    assert _tele([0, 1, 0], [0, 0, 1]) is False


def test_a_new_teleport_inside_the_change_set_is_not_a_resolution():
    """🔴 THE CASE THE UNTOUCHED-CHAPTER RULE CANNOT SEE. When the new move lands in a chapter
    NOBODY changed, the byte-identity rule above already refuses it — so that row leaves the
    "no new teleport anywhere" check unreached and unproven. Here chapters 2 and 3 were BOTH
    targeted and both legitimately changed, and the repair traded chapter 2's single move for
    an extra one in chapter 3. Only the final `outliers_after - outliers_before` equivalent
    catches that."""
    assert _tele([0, 1, 1], [0, 0, 2], chapter=2, changed=(2, 3)) is False
    # ...and the same repair without the extra move IS a resolution, so the check above is
    # refusing the defect rather than refusing everything.
    assert _tele([0, 1, 1], [0, 0, 1], chapter=2, changed=(2, 3)) is True


def test_a_chapter_that_was_never_an_offender_cannot_be_resolved():
    assert _tele([0, 0, 0], [0, 0, 0]) is False


def test_a_count_moving_in_an_untouched_chapter_vouches_for_nothing():
    """Chapter 3 was supposed to come back byte-identical; a count that moved there means the
    repair reached further than it was allowed to, or the observation is unreliable."""
    assert _tele([0, 1, 1], [0, 0, 0], changed=(2,)) is False
    assert _tele([0, 1, 1], [0, 0, 0], changed=(2, 3)) is True


@pytest.mark.parametrize("before,after", [
    (None, [0, 0, 0]),
    ([0, 1, 0], None),
    ([0, 1, 0], [0, 0]),
    ([0, "x", 0], [0, 0, 0]),
])
def test_an_unreadable_teleport_census_on_either_side_is_not_a_resolution(before, after):
    assert _tele(before, after) is False


@pytest.mark.parametrize("chapter,count", [(True, 3), ("2", 3), (2, 0), (2, True)])
def test_teleport_verification_refuses_a_malformed_target(chapter, count):
    assert _tele([0, 1, 0], [0, 0, 0], chapter=chapter, count=count) is False


# ---------------------------------------------------------------------------
# verify_beat_resolved
# ---------------------------------------------------------------------------
CHAIN = {1: 3}


def _beats(states):
    return [{"chapter": 1, "beat": i, "state": s} for i, s in enumerate(states, 1)]


def _beat(before, after, *, beat=3, order=False):
    return nf6.verify_beat_resolved(_beats(before), _beats(after), chapter=1, beat=beat,
                                    outline_sizes=CHAIN, require_order=order)


def test_an_absent_beat_that_became_executed_is_resolved():
    assert _beat(["executed", "executed", "absent"],
                 ["executed", "executed", "executed"]) is True


def test_a_promise_is_not_an_execution():
    """🔴 THE WHOLE POINT OF THE CLASS. "I will give a deposition" reads like a resolution to
    any check that only asks whether the deposition is mentioned."""
    assert _beat(["executed", "executed", "absent"],
                 ["executed", "executed", "promised"]) is False


def test_a_beat_that_was_already_executed_is_not_this_violation():
    assert _beat(["executed", "executed", "executed"],
                 ["executed", "executed", "executed"]) is False


def test_a_repair_that_dropped_another_beat_resolves_nothing():
    """Trading one defect for another is not a repair."""
    assert _beat(["executed", "executed", "absent"],
                 ["executed", "absent", "executed"]) is False


def test_the_ordering_chain_is_enforced_only_for_beat_execution():
    """🔴 `surrender → recordings inadmissible → deposition executed`. A deposition that happens
    while the beat it depends on is still absent is a different scene wearing the outline's
    name — which `beat_execution` refuses and `final_beat` does not ask about."""
    before = ["absent", "absent", "absent"]
    after = ["absent", "absent", "executed"]
    assert _beat(before, after, order=False) is True
    assert _beat(before, after, order=True) is False
    assert _beat(before, ["executed", "executed", "executed"], order=True) is True


@pytest.mark.parametrize("before,after", [
    (None, ["executed"] * 3),
    (["executed", "executed", "absent"], None),
    (["executed", "executed", "absent"], ["executed", "executed"]),
])
def test_an_unreadable_beat_census_on_either_side_is_not_a_resolution(before, after):
    assert nf6.verify_beat_resolved(
        _beats(before) if before else before,
        _beats(after) if after else after,
        chapter=1, beat=3, outline_sizes=CHAIN) is False


def test_a_beat_the_outline_does_not_have_cannot_be_resolved():
    assert _beat(["executed", "executed", "absent"],
                 ["executed", "executed", "executed"], beat=9) is False


# ---------------------------------------------------------------------------
# chapter_word_bounds + verify_ceiling_resolved
# ---------------------------------------------------------------------------
def test_the_ceiling_is_the_generators_own_contract():
    """`word_target × 1.1` is `word_max` from the chapter prompt; `× 0.9` is `word_min`. A
    ceiling F6 invented for itself would judge the chapter against a bound nobody gave it."""
    assert nf6.chapter_word_bounds({"chapters": [{"word_target": 400}]}, 1) == {1: (360, 440)}


def test_the_legacy_words_key_is_honoured():
    assert nf6.chapter_word_bounds({"chapters": [{"words": 100}]}, 1) == {1: (90, 110)}


@pytest.mark.parametrize("body", [
    {}, {"chapters": None}, {"chapters": []}, {"chapters": ["x"]},
    {"chapters": [{"word_target": 0}]}, {"chapters": [{"word_target": -5}]},
    {"chapters": [{"word_target": True}]}, {"chapters": [{"word_target": "400"}]},
])
def test_a_chapter_with_no_declared_target_has_no_ceiling(body):
    """There is no contract to exceed, and inventing one would block a job over a bound it was
    never given."""
    assert nf6.chapter_word_bounds(body, 1) == {}


def test_the_request_copy_may_be_longer_than_the_book():
    assert set(nf6.chapter_word_bounds({"chapters": [{"word_target": 100}] * 5}, 2)) == {1, 2}


BOUNDS = {1: (90, 110), 2: (90, 110)}


def _ceiling(before, after, *, chapter=1, changed=(1,), bounds=BOUNDS, count=2):
    return nf6.verify_ceiling_resolved(before, after, chapter=chapter, bounds=bounds,
                                       chapter_count=count, changed_chapters=changed)


def test_a_chapter_brought_inside_its_ceiling_is_resolved():
    assert _ceiling([200, 100], [100, 100]) is True


def test_shorter_but_still_over_is_not_resolved():
    assert _ceiling([200, 100], [130, 100]) is False


def test_an_emptied_chapter_is_not_resolved():
    """🔴 A CHAPTER REDUCED TO NOTHING IS COMFORTABLY UNDER ITS CEILING. The floor is the other
    half of the same contract."""
    assert _ceiling([200, 100], [0, 100]) is False
    assert _ceiling([200, 100], [80, 100]) is False


def test_exactly_at_the_ceiling_is_inside_it():
    assert _ceiling([200, 100], [110, 100]) is True


def test_a_chapter_that_was_never_over_its_ceiling_cannot_be_resolved():
    assert _ceiling([100, 100], [95, 100]) is False


def test_a_reduction_that_pushed_another_chapter_over_resolves_nothing():
    assert _ceiling([200, 100], [100, 150], changed=(1, 2)) is False


def test_a_count_moving_in_an_untouched_chapter_vouches_for_nothing_either():
    assert _ceiling([200, 100], [100, 105], changed=(1,)) is False


def test_a_chapter_with_no_bound_cannot_be_resolved():
    assert _ceiling([200, 100], [100, 100], bounds={2: (90, 110)}) is False


@pytest.mark.parametrize("before,after", [
    (None, [100, 100]),
    ([200, 100], None),
    ([200, 100], [100]),
    ([200, 100], [100, "x"]),
    ([200, 100], [100, -1]),
    ([200, True], [100, 100]),
])
def test_an_unreadable_word_count_on_either_side_is_not_a_resolution(before, after):
    assert _ceiling(before, after) is False


@pytest.mark.parametrize("bounds", [None, "nope", {1: (90,)}, {1: 110}])
def test_a_malformed_bounds_contract_vouches_for_nothing(bounds):
    assert _ceiling([200, 100], [100, 100], bounds=bounds) is False


# ---------------------------------------------------------------------------
# The five classes are one closed set
# ---------------------------------------------------------------------------
def test_every_f6_class_is_either_single_instance_or_needs_a_claim():
    """🔴 A CLASS THAT IS NEITHER WOULD BE UNIDENTIFIABLE FOREVER. Adding one to `F6_CLASSES`
    without deciding which side it is on makes every instance of it an orphan."""
    for vclass in nf6.F6_CLASSES:
        with_claim = nf6.violation_identity(
            {"f6_class": vclass, "chapter": 1, "f6_claim": "c"})
        without = nf6.violation_identity({"f6_class": vclass, "chapter": 1})
        assert with_claim is not None, vclass
        assert (without is not None) == (vclass in ("tense_drift", "chapter_ceiling")), vclass


def test_a_class_outside_the_set_gets_no_identity():
    assert nf6.violation_identity({"f6_class": "vibes", "chapter": 1}) is None
