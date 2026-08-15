"""F1 — generation-leak containment (BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md, §F1).

Canary `y1b503pf` delivered manuscript v9 with four internal ids — `[anc1]`..`[anc4]` —
copied verbatim into customer-visible prose. `render_canon()` (PROMPT MATERIAL for QC/L3,
unchanged here) serializes `{"anchor_id": "anc3", ...}` into the assist prefix every
chapter worker reads; the model apparently invents its own `[anc3]`-shaped citation while
weaving the anchor's `literal` into a sentence — the source text never actually contains a
bracket, which is why detection/removal must match against the CLOSED SET of ids this
canon instance actually binds, never a bare `\\[anc\\d+\\]`-style guess (a bracket that is
NOT one of this canon's own ids — a legitimate `[Tuesday]`, a footnote `[1]`, an id from a
DIFFERENT canon instance — must never be touched).

Two independent halves, tested separately per the brief's own instruction that a scrub
being mutated to a no-op must still be caught: `scrub_bound_markers` (repair — removes
what it can) and `detect_bound_markers` (verification — the delivery gate's actual
authority; a caller must trust ITS answer, not scrub's side effect, to decide GO/NO-GO).
"""

import canon_lite as cl
import pytest


# ===========================================================================
# Helpers (same pattern as test_canon_lite_l1.py)
# ===========================================================================

def _outline(n=3):
    return [{"id": i + 1, "title": f"Bab {i + 1}", "summary": f"ringkasan {i + 1}"}
            for i in range(n)]


def _cfg(outline=None, **kw):
    outline = _outline() if outline is None else outline
    kw.setdefault("target_language", "id")
    kw.setdefault("narration_style", "kdrama_serial")
    return cl.build_job_config_snapshot(outline_chapters=outline, **kw)


def _canon(outline=None, **kw):
    outline = _outline() if outline is None else outline
    cfg = kw.pop("job_config", None) or _cfg(outline)
    return cl.build_canon_lite_v1(outline_chapters=outline, job_config=cfg, **kw)


def _v9_canon():
    """Four anchors, ids anc1..anc4 — the exact shape of the leaking canon in canary v9."""
    return _canon(anchors=[
        cl.CanonAnchorV1("anc1", "time", "five years of absence"),
        cl.CanonAnchorV1("anc2", "quantity", "30-day co-habitation lease"),
        cl.CanonAnchorV1("anc3", "time", "early September"),
        cl.CanonAnchorV1("anc4", "time", "five years prior"),
    ])


# ===========================================================================
# canon_bound_marker_ids — the closed set, all five component types
# ===========================================================================

def test_bound_marker_ids_covers_all_five_component_types():
    canon = _canon(
        entities=[cl.CanonEntityV1("ent1", "Sari", ("Neng Sari",), "job_input")],
        anchors=[cl.CanonAnchorV1("anc1", "time", "12 Maret 1998")],
        one_time_events=[cl.CanonEventV1("evt_x_ab12cd34ef56", 2, "rumah Sari terbakar")],
        reveals=[cl.CanonRevealV1("rev1", 3)],
        flashback_exceptions=[cl.CanonFlashbackExceptionV1("fx1", 2, "declared_flashback")],
    )
    assert cl.canon_bound_marker_ids(canon) == {"ent1", "anc1", "evt_x_ab12cd34ef56",
                                                 "rev1", "fx1"}


def test_bound_marker_ids_empty_canon_is_empty_set():
    assert cl.canon_bound_marker_ids(_canon()) == frozenset()


def test_bound_marker_ids_rejects_non_canon_input():
    with pytest.raises(cl.CanonSchemaError):
        cl.canon_bound_marker_ids("not a canon")


# ===========================================================================
# scrub_bound_markers — repair half
# ===========================================================================

def test_scrub_removes_all_four_v9_markers():
    canon = _v9_canon()
    text = (
        "The rusted gate groaned, a familiar lament that echoed [anc1] five years of "
        "absence. Kang Tae-jun stepped onto the rooftop, the crisp [anc3] early "
        "September air biting at his cheeks. It was a [anc2] 30-day co-habitation "
        "lease. [anc4] Five years prior, the same rooftop had been a sanctuary."
    )
    scrubbed, count = cl.scrub_bound_markers(text, canon)
    assert count == 4
    for marker in ("[anc1]", "[anc2]", "[anc3]", "[anc4]"):
        assert marker not in scrubbed
    # prose either side of every removed marker survives, without fused words
    assert "echoed five years of absence" in scrubbed
    assert "the crisp early September air" in scrubbed
    assert "It was a 30-day co-habitation lease" in scrubbed
    assert "Five years prior, the same rooftop" in scrubbed
    assert "  " not in scrubbed, "no double-space artifact from a removed marker"


def test_scrub_rescan_confirms_clean():
    canon = _v9_canon()
    text = "the crisp [anc3] early September air"
    scrubbed, _ = cl.scrub_bound_markers(text, canon)
    assert cl.detect_bound_markers(scrubbed, canon) == ()


def test_scrub_leaves_legitimate_brackets_byte_identical():
    canon = _v9_canon()
    text = ("Meet me [Tuesday] at noon. See footnote [1] for the source. "
            "The file was marked [REDACTED] before release.")
    scrubbed, count = cl.scrub_bound_markers(text, canon)
    assert count == 0
    assert scrubbed == text, "no canon-bound id present — text must be untouched"


def test_scrub_does_not_touch_an_id_valid_in_a_DIFFERENT_canon_instance():
    """The closed set is per-instance. An id that would be bound in some OTHER run's
    canon is not an internal marker for THIS run — the closed set must come from the
    canon actually passed in, never a global/prefix guess."""
    canon_without_anc1 = _canon(anchors=[cl.CanonAnchorV1("anc7", "time", "unrelated")])
    text = "a familiar lament that echoed [anc1] five years of absence"
    scrubbed, count = cl.scrub_bound_markers(text, canon_without_anc1)
    assert count == 0
    assert scrubbed == text


def test_scrub_none_canon_is_a_safe_noop():
    text = "some prose with [anc1] in it"
    scrubbed, count = cl.scrub_bound_markers(text, None)
    assert (scrubbed, count) == (text, 0)


def test_scrub_empty_text_is_a_safe_noop():
    assert cl.scrub_bound_markers("", _v9_canon()) == ("", 0)


def test_scrub_collapses_back_to_back_markers_without_fusing_words():
    """Two markers with no separating whitespace between them is the shape most likely
    to break a naive "eat the adjacent space" replacer — each marker's own regex match
    can only see ONE of its two neighbouring spaces, and a wrong rule silently fuses
    the words on the other side of the pair."""
    canon = _v9_canon()
    text = "text [anc1][anc2] more"
    scrubbed, count = cl.scrub_bound_markers(text, canon)
    assert count == 2
    assert scrubbed == "text more"


def test_scrub_marker_at_string_start_and_end():
    canon = _v9_canon()
    assert cl.scrub_bound_markers("[anc1] Text starts here", canon)[0].strip() == "Text starts here"
    assert cl.scrub_bound_markers("Text ends here [anc1]", canon)[0].strip() == "Text ends here"


# ===========================================================================
# Adversarial-audit findings, F1 (2026-08-14 night): case sensitivity + the
# whitespace-collapse blast radius. Both against the real functions, end to end.
# ===========================================================================

def test_scrub_and_detect_are_case_insensitive_to_a_capitalized_echo():
    """A model auto-capitalizing the first token of a sentence (ordinary LLM
    behaviour) produces `[Anc1]`, not `[anc1]` -- this defeated BOTH the scrub and
    the detect half identically (they share one regex), which is the exact
    customer-visible leak class canary v9 shipped, just case-varied."""
    canon = _v9_canon()
    text = "After a long silence. [Anc1] settled over the rooftop that night."
    scrubbed, count = cl.scrub_bound_markers(text, canon)
    assert count == 1
    assert "[Anc1]" not in scrubbed and "[anc1]" not in scrubbed
    assert "After a long silence." in scrubbed and "settled over the rooftop" in scrubbed
    assert cl.detect_bound_markers(text, canon) == ("anc1",), \
        "detect must catch a case-varied echo too -- it is the ONLY function a delivery gate may trust"
    assert cl.detect_bound_markers(scrubbed, canon) == ()


def test_detect_reports_the_canonical_lowercase_id_regardless_of_echoed_case():
    canon = _v9_canon()
    assert cl.detect_bound_markers("[ANC3] and [Anc1]", canon) == ("anc3", "anc1")


def test_scrub_whitespace_collapse_is_localized_to_the_removed_marker_not_global():
    """The post-removal doubled-space collapse must never touch whitespace elsewhere
    in the string -- only whitespace directly adjacent to a marker that was actually
    removed. Verse indentation, a deliberate double space, and a markdown hard-break
    trailing double-space, ALL unrelated to the one removed marker, must survive
    byte-for-byte (audit-caught: the prior global collapse mangled all three)."""
    canon = _v9_canon()
    text = (
        "Intro line.\n\n"
        "    A verse line, indented.\n"
        "    Another verse line, indented.\n\n"
        "This  sentence  has  a  deliberate  double  space.\n\n"
        "A line with a hard break.  \n"
        "The next line.\n\n"
        "the crisp [anc3] early September air.\n"
    )
    scrubbed, count = cl.scrub_bound_markers(text, canon)
    assert count == 1
    assert "[anc3]" not in scrubbed
    assert "the crisp early September air." in scrubbed
    assert "    A verse line, indented.\n    Another verse line, indented." in scrubbed, \
        "unrelated verse indentation must survive untouched"
    assert "This  sentence  has  a  deliberate  double  space." in scrubbed, \
        "a pre-existing deliberate double space elsewhere must survive untouched"
    assert "A line with a hard break.  \n" in scrubbed, \
        "a markdown hard-break's trailing double space elsewhere must survive untouched"


# ===========================================================================
# detect_bound_markers — verification half, the delivery gate's actual authority
# ===========================================================================

def test_detect_reports_every_distinct_bound_id_present():
    canon = _v9_canon()
    text = "[anc1] ... [anc3] ... [anc1] again"
    assert cl.detect_bound_markers(text, canon) == ("anc1", "anc3")


def test_detect_is_the_authority_a_broken_scrub_cannot_fool():
    """This is the unit-level half of the brief's DoD item: 'ketika scrub sengaja
    dimutasi agar gagal, publication seam harus menolak delivery.' A caller that wires
    the delivery gate to scrub's own side effect instead of an independent detect call
    would ship a leak the moment the scrubber has any bug at all. Simulate exactly
    that bug — a scrub that does nothing — and prove detect still sees the leak."""
    canon = _v9_canon()
    text = "the crisp [anc3] early September air"

    def _broken_scrub_that_does_nothing(t, _c):
        return t, 0

    scrubbed, _ = _broken_scrub_that_does_nothing(text, canon)
    assert cl.detect_bound_markers(scrubbed, canon) == ("anc3",), \
        "detect must independently see the leak scrub failed to remove"


def test_detect_ignores_ids_not_bound_in_this_canon():
    canon = _canon(anchors=[cl.CanonAnchorV1("anc7", "time", "unrelated")])
    assert cl.detect_bound_markers("[anc1] prose", canon) == ()


def test_detect_none_canon_is_empty():
    assert cl.detect_bound_markers("[anc1] prose", None) == ()


# ===========================================================================
# render_canon_for_generation — prevention half; render_canon() contract untouched
# ===========================================================================

def test_generation_projection_omits_every_bound_id():
    canon = _canon(
        entities=[cl.CanonEntityV1("ent1", "Sari", ("Neng Sari",), "job_input")],
        anchors=[cl.CanonAnchorV1("anc1", "time", "12 Maret 1998")],
        one_time_events=[cl.CanonEventV1("evt_x_ab12cd34ef56", 2, "rumah Sari terbakar")],
        reveals=[cl.CanonRevealV1("rev1", 3)],
        flashback_exceptions=[cl.CanonFlashbackExceptionV1("fx1", 2, "declared_flashback")],
    )
    generation_text = cl.render_canon_for_generation(canon)
    for bound_id in cl.canon_bound_marker_ids(canon):
        assert bound_id not in generation_text, f"{bound_id} leaked into generation projection"
    # the FACTS survive — only the ids are gone
    assert "Sari" in generation_text
    assert "Neng Sari" in generation_text
    assert "12 Maret 1998" in generation_text
    assert "rumah Sari terbakar" in generation_text


def test_render_canon_still_carries_ids_contract_unchanged():
    """render_canon() is QC/L3's contract — F1 must not touch it. Same canon, both
    renderers: the id-bearing legacy render still has the ids; the new generation
    projection does not."""
    canon = _canon(anchors=[cl.CanonAnchorV1("anc1", "time", "12 Maret 1998")])
    assert "anc1" in cl.render_canon(canon)
    assert "anc1" not in cl.render_canon_for_generation(canon)


def test_generation_projection_is_deterministic_and_byte_stable():
    outline = _outline()
    a = _canon(outline, anchors=[cl.CanonAnchorV1("anc1", "time", "sama")])
    b = _canon(outline, anchors=[cl.CanonAnchorV1("anc1", "time", "sama")])
    assert cl.render_canon_for_generation(a) == cl.render_canon_for_generation(b)


def test_generation_projection_carries_its_own_version_marker():
    canon = _canon()
    assert cl.GENERATION_PROJECTION_VERSION in cl.render_canon_for_generation(canon)
    assert cl.GENERATION_PROJECTION_VERSION != cl.SCHEMA_VERSION, \
        "must not be conflated with the canon schema version render_canon() carries"


def test_generation_projection_changes_when_a_bound_literal_changes():
    """Not vacuous: the projection must actually reflect content, not just omit ids."""
    outline = _outline()
    a = _canon(outline, anchors=[cl.CanonAnchorV1("anc1", "time", "12 Maret 1998")])
    b = _canon(outline, anchors=[cl.CanonAnchorV1("anc1", "time", "13 Maret 1998")])
    assert cl.render_canon_for_generation(a) != cl.render_canon_for_generation(b)


# ===========================================================================
# 2026-08-15 re-audit — REJECT findings closed here:
# (1) render_canon_for_generation() still called CanonChapterV1.to_canonical_obj() for
#     [CHAPTERS], which carries chapter_id — every OTHER component type was already
#     re-projected id-free (see test_generation_projection_omits_every_bound_id above),
#     chapters alone were missed.
# (2) canon_bound_marker_ids() deliberately excludes chapter_id BY DESIGN (module
#     docstring above canon_bound_marker_ids, unchanged): a chapter id that is simply the
#     chapter's own order number ("1" for chapter 1) is human-facing, indistinguishable
#     from a legitimate footnote "[1]", and must stay legal. That reasoning does not
#     extend to an OPAQUE outline-supplied chapter id ("private-chapter-alpha", "ch1", a
#     UUID) — those are never a legitimate footnote and were reaching generation with no
#     closed-set coverage to catch them if they ever leaked.
# ===========================================================================

def _opaque_chapter_outline(opaque_id="private-chapter-alpha", n=3):
    """Chapter 1 carries a custom, non-numeric outline id; the rest default to their
    order number, the common/expected shape."""
    rows = _outline(n)
    rows[0] = {**rows[0], "id": opaque_id}
    return rows


def test_opaque_chapter_id_does_not_appear_in_generation_projection():
    outline = _opaque_chapter_outline()
    canon = _canon(outline)
    assert canon.chapters[0].chapter_id == "private-chapter-alpha"  # sanity: outline landed
    generation_text = cl.render_canon_for_generation(canon)
    assert "private-chapter-alpha" not in generation_text
    # the writer-facing fields the brief asks for ARE still there
    assert canon.chapters[0].expected_title in generation_text or cl.UNKNOWN in generation_text


def test_opaque_chapter_id_is_in_the_closed_marker_set_and_gets_scrubbed():
    outline = _opaque_chapter_outline()
    canon = _canon(outline)
    assert "private-chapter-alpha" in cl.canon_bound_marker_ids(canon)
    text = "Dia membuka [private-chapter-alpha] dan mulai membaca."
    scrubbed, n = cl.scrub_bound_markers(text, canon)
    assert n == 1
    assert "[private-chapter-alpha]" not in scrubbed
    assert cl.detect_bound_markers(scrubbed, canon) == ()


def test_sequential_numeric_chapter_id_stays_legal_as_a_footnote():
    """The ORIGINAL reasoning this module's docstring gives for excluding chapter_id
    must still hold for the common case: chapter 1's id, left at its default ("1"),
    must not become an unremovable/flagged bracket — `[1]` is a legitimate footnote."""
    outline = _outline()  # every chapter id defaults to its own order number
    canon = _canon(outline)
    assert canon.chapters[0].chapter_id == "1"
    assert "1" not in cl.canon_bound_marker_ids(canon)
    text = "Lihat catatan kaki [1] untuk detail."
    scrubbed, n = cl.scrub_bound_markers(text, canon)
    assert n == 0
    assert scrubbed == text
    assert cl.detect_bound_markers(text, canon) == ()


def test_prefix_guessing_is_not_used_ch1_style_ids_are_opaque_too():
    """`_canonical_outline_chapters` falls back to a generated "ch{n}" id when the
    outline's own id fails `_ID_RE` — that generated id is exactly as opaque as a
    custom one and must be covered the same way, not specially exempted because it
    looks like a auto-generated label."""
    outline = _outline()
    outline[0] = {**outline[0], "id": "!!! invalid !!!"}  # fails _ID_RE -> falls back
    canon = _canon(outline)
    assert canon.chapters[0].chapter_id == "ch1"
    assert "ch1" in cl.canon_bound_marker_ids(canon)
    assert "ch1" not in cl.render_canon_for_generation(canon)


def test_render_canon_chapters_contract_is_byte_identical_after_the_fix():
    """render_canon() (QC/L3's contract) must not change at all — same golden shape as
    test_render_canon_still_carries_ids_contract_unchanged above, chapter-specific."""
    outline = _opaque_chapter_outline()
    canon = _canon(outline)
    assert "private-chapter-alpha" in cl.render_canon(canon)
    assert '"chapter_id":"private-chapter-alpha"' in cl.render_canon(canon)  # compact JSON, no space
