"""F6 tense drift — the server owns the census; the critic only reports per chapter.

🔴 WHY A CENSUS AND NOT A FINDING. Asking the critic "is there tense drift?" hands it both the
   observation and the judgement, and it answers with prose the server cannot target. The v9
   manuscript is past / present / past: the DEFECT is chapter 2, but nothing in a prose finding
   says so in a form a repair call can be pointed at.

   So the critic is asked only for what it can see per chapter — the dominant tense of each —
   and the server does the arithmetic: majority, outlier, target. That arithmetic is
   deterministic, so it is testable without a provider, and it cannot drift with a prompt.

🔴 NO SECOND PROVIDER CALL. The census rides on the critic call that already happens. Adding a
   census-specific call would double the cost of every job to compute something the critic is
   already reading the whole book for.

🔴 CONSERVATIVE WHERE IT CANNOT TELL. No strict majority means no outlier and no target: a
   two-chapter book split past/present has no minority to repair, and guessing one would send a
   correct chapter to a rewrite.
"""
from __future__ import annotations

import narasi_gate as ng


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------
def test_the_v9_shape_targets_chapter_two():
    """🔴 THE CASE F6 EXISTS FOR. past / present / past → the majority is past and the single
    present chapter is the outlier. Chapter numbers are 1-based, as every other finding is."""
    out = ng.tense_census(["past", "present", "past"])
    assert out["valid"] is True
    assert out["majority"] == "past"
    assert out["outliers"] == [2]


def test_the_inverse_book_is_the_same_violation():
    """A present-tense book with one past chapter is not a different rule."""
    out = ng.tense_census(["present", "past", "present"])
    assert out["majority"] == "present"
    assert out["outliers"] == [2]


def test_a_consistent_book_has_no_outlier():
    out = ng.tense_census(["past", "past", "past"])
    assert out["majority"] == "past"
    assert out["outliers"] == []


def test_several_outliers_are_all_reported():
    out = ng.tense_census(["past", "present", "past", "present", "past"])
    assert out["majority"] == "past"
    assert out["outliers"] == [2, 4]


def test_no_strict_majority_targets_nothing():
    """🔴 THE CONSERVATIVE DIRECTION. Two chapters, one each: there is no minority to repair,
    and picking one would rewrite a chapter that is not wrong."""
    out = ng.tense_census(["past", "present"])
    assert out["valid"] is True
    assert out["majority"] is None
    assert out["outliers"] == []
    assert out["reason"] == "no_majority"


def test_an_exact_half_is_not_a_majority():
    out = ng.tense_census(["past", "past", "present", "present"])
    assert out["majority"] is None
    assert out["outliers"] == []


def test_a_mixed_chapter_is_an_outlier_against_a_clear_majority():
    out = ng.tense_census(["past", "mixed", "past"])
    assert out["majority"] == "past"
    assert out["outliers"] == [2]


# ---------------------------------------------------------------------------
# Bounded and validated: the input comes from a model
# ---------------------------------------------------------------------------
def test_a_value_outside_the_closed_vocabulary_invalidates_the_census():
    """🔴 THE CENSUS IS MODEL INPUT. An unrecognised label is not evidence of anything, and
    reading it as "not the majority" would target a chapter on a typo."""
    out = ng.tense_census(["past", "future", "past"])
    assert out["valid"] is False
    assert out["outliers"] == []
    assert out["reason"] == "invalid_value"


def test_a_census_that_does_not_cover_every_chapter_is_refused():
    """Targeting by index only means anything if the list IS the chapters, in order."""
    out = ng.tense_census(["past", "present"], chapter_count=3)
    assert out["valid"] is False
    assert out["outliers"] == []
    assert out["reason"] == "chapter_count_mismatch"

    ok = ng.tense_census(["past", "present", "past"], chapter_count=3)
    assert ok["valid"] is True and ok["outliers"] == [2]


def test_a_missing_or_malformed_census_is_simply_absent():
    for bad in (None, "past", {}, 3, [], ["past", None, "past"], [["past"]]):
        out = ng.tense_census(bad)
        assert out["valid"] is False, bad
        assert out["outliers"] == [], bad


def test_case_and_whitespace_from_a_model_are_normalised_not_rejected():
    """A model writing "Past " is answering correctly; refusing it would be a false negative."""
    out = ng.tense_census([" Past", "PRESENT", "past "])
    assert out["valid"] is True
    assert out["majority"] == "past"
    assert out["outliers"] == [2]


def test_the_census_is_bounded_so_a_runaway_list_cannot_be_used():
    """A model that returns thousands of entries is malformed, not informative."""
    out = ng.tense_census(["past"] * (ng._TENSE_CENSUS_MAX_CHAPTERS + 1))
    assert out["valid"] is False
    assert out["reason"] == "too_many_chapters"
