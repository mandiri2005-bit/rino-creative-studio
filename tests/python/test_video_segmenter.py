# -*- coding: utf-8 -*-
"""
Tests for video_segmenter.py (Step 6 / F-1 — the scene segmenter).

These exercise the contract (the Duration Presets table), the formula, both
narration modes, the scene object, the clip-eligibility gate, and the visual
prompt — all on the dependency-free path (spaCy is NOT required to pass).
"""
import os
import sys

import pytest

# Import the module directly from python/ without importing the heavy backend
# (laozhang_api etc.), so this suite stays fast and self-contained. The module
# only depends on the stdlib, so a plain import is safe and side-effect-free.
_PY_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "python"))
if _PY_DIR not in sys.path:
    sys.path.insert(0, _PY_DIR)
import video_segmenter as vs  # noqa: E402


# ── A sample multi-sentence narration (Indonesian, documentary register) ───────
NARRATION = (
    "Di geladak sempit sebuah perahu bercadik, benih padi dibungkus daun pisang. "
    "Para pelaut Austronesia menatap cakrawala yang belum berperidi. "
    "Mereka membawa babi, anjing, dan ayam melintasi lautan luas. "
    "Pulau demi pulau muncul dari kabut pagi seperti punggung raksasa tidur. "
    "Di Sulawesi, mereka mendirikan rumah panggung pertama dari kayu keras. "
    "Bahasa mereka menyebar lebih jauh dari bahasa mana pun sebelumnya. "
    "Hari ini, jejak pelayaran itu masih hidup dalam ribuan pulau Nusantara. "
    "Dan kisah mereka baru saja dimulai."
)


# ══════════════════════════════════════════════════════════════════════════════
# The contract — the Duration Presets table
# ══════════════════════════════════════════════════════════════════════════════
def test_contract_holds():
    """The formula must reproduce every published preset (words, scenes, credits)."""
    problems = vs.verify_contract()
    assert problems == [], "contract violations:\n" + "\n".join(problems)


@pytest.mark.parametrize("minutes,words,scenes", [
    (0.5, 65, 2),
    (1, 130, 3),
    (2, 260, 6),
    (3, 390, 9),
    (5, 650, 14),
    (10, 1300, 29),
    (15, 1950, 43),
])
def test_preset_rows(minutes, words, scenes):
    p = vs.calculate_video_params(minutes, "hd")
    assert p.target_words == words
    assert p.scene_count == scenes


@pytest.mark.parametrize("minutes,fast,hd,hdp", [
    (0.5, 4, 10, 16),
    (1, 6, 15, 24),     # NOTE: doc renders 18/28 here — a typo; formula is 15/24
    (2, 12, 30, 48),
    (3, 18, 45, 72),
    (5, 28, 70, 112),
    (10, 58, 145, 232),
    (15, 86, 215, 344),
])
def test_preset_credits(minutes, fast, hd, hdp):
    p = vs.calculate_video_params(minutes, "hd")
    assert p.credits_by_tier == {"fast": fast, "hd": hd, "hd_plus": hdp}


# ══════════════════════════════════════════════════════════════════════════════
# calculate_video_params — dispatch, batch plan, progress UI
# ══════════════════════════════════════════════════════════════════════════════
def test_dispatch_and_ui_thresholds():
    assert vs.calculate_video_params(3).dispatch_mode == "full_parallel"   # 9 scenes
    assert vs.calculate_video_params(3).progress_ui == "cards"
    assert vs.calculate_video_params(5).dispatch_mode == "batch"           # 14 scenes
    assert vs.calculate_video_params(5).progress_ui == "bar"


@pytest.mark.parametrize("minutes,plan", [
    (3, [9]),
    (5, [10, 4]),
    (10, [10, 10, 9]),
    (15, [10, 10, 10, 10, 3]),
])
def test_batch_plan(minutes, plan):
    p = vs.calculate_video_params(minutes)
    assert p.batch_plan == plan
    assert sum(p.batch_plan) == p.scene_count


def test_tier_selection_picks_right_credits():
    p = vs.calculate_video_params(2, "fast")
    assert p.credits == p.credits_by_tier["fast"] == 12
    assert vs.calculate_video_params(2, "hd+").credits == 48


def test_min_scenes_floor():
    # a tiny duration still yields at least 2 scenes
    assert vs.calculate_video_params(0.1).scene_count == vs.MIN_SCENES


def test_invalid_minutes_raise():
    with pytest.raises(ValueError):
        vs.calculate_video_params(0)
    with pytest.raises(ValueError):
        vs.calculate_video_params(-3)


# ══════════════════════════════════════════════════════════════════════════════
# Mode B — never truncate; scene count from real length
# ══════════════════════════════════════════════════════════════════════════════
def test_mode_b_preserves_all_words():
    res = vs.segment(NARRATION, mode="B", style="creative_nonfiction")
    assert res.truncated is False
    joined = " ".join(s.text for s in res.scenes)
    # every word of the source survives, in order
    assert joined.split() == NARRATION.split()
    assert res.actual_words == len(NARRATION.split())


def test_mode_b_scene_count_from_words():
    res = vs.segment(NARRATION, mode="B")
    expected = vs.scene_count_for_words(len(NARRATION.split()))
    # may be capped by sentence count, but never exceeds the formula's count
    assert 1 <= len(res.scenes) <= expected or len(res.scenes) == expected


def test_mode_b_minutes_reflect_real_length():
    res = vs.segment(NARRATION, mode="B")
    assert res.actual_minutes == round(len(NARRATION.split()) / 130, 2)
    assert res.params.target_words == res.actual_words


# ══════════════════════════════════════════════════════════════════════════════
# Mode A — exactly scene_count scenes
# ══════════════════════════════════════════════════════════════════════════════
def test_mode_a_requires_minutes():
    with pytest.raises(ValueError):
        vs.segment(NARRATION, mode="A")


def test_mode_a_scene_count_matches_params():
    # build narration with plenty of sentences, then segment to a 2-min plan (6 scenes)
    long_text = " ".join(f"Kalimat nomor {i} bercerita tentang laut dan pulau." for i in range(40))
    res = vs.segment(long_text, mode="A", minutes=2)
    assert res.params.scene_count == 6
    assert len(res.scenes) == 6


# ══════════════════════════════════════════════════════════════════════════════
# Scene object integrity
# ══════════════════════════════════════════════════════════════════════════════
def test_scene_numbering_and_positions():
    res = vs.segment(NARRATION, mode="B")
    scenes = res.scenes
    assert [s.number for s in scenes] == list(range(1, len(scenes) + 1))
    assert scenes[0].position == "opening"
    assert scenes[-1].position == "closing"
    for s in scenes[1:-1]:
        assert s.position == "middle"


def test_scene_empty_asset_slots():
    res = vs.segment(NARRATION, mode="B")
    for s in res.scenes:
        assert s.audio_url is None
        assert s.clip_url is None


def test_est_seconds_formula():
    res = vs.segment(NARRATION, mode="B")
    for s in res.scenes:
        assert s.est_seconds == round(s.word_count / 130 * 60, 2)


def test_visual_prompt_present_and_capped():
    res = vs.segment(NARRATION, mode="B", style="natgeo")
    for s in res.scenes:
        assert s.visual_prompt.strip()
        assert len(s.visual_prompt) <= vs.MAX_VISUAL_PROMPT_CHARS
        assert len(s.visual_prompt.split()) <= vs.MAX_VISUAL_PROMPT_WORDS


def test_visual_prompt_reflects_style_tone():
    opening = vs.segment(NARRATION, mode="B", style="bedtime_story").scenes[0]
    assert "pastel" in opening.visual_prompt or "soft" in opening.visual_prompt


# ══════════════════════════════════════════════════════════════════════════════
# Clip eligibility gate (forward-looking → Step 6d)
# ══════════════════════════════════════════════════════════════════════════════
def test_clip_fits_veo_vs_kling():
    # 7s narration: too long for Veo (6.8s gate), fits Kling (12.7s gate)
    assert vs.clip_fits(7.0, "veo3") is False
    assert vs.clip_fits(7.0, "kling3") is True
    assert vs.clip_fits(5.0, "veo3") is True


def test_suggested_clip_length_snaps_to_allowed():
    assert vs.suggested_clip_length(5.0, "veo3") == 6      # smallest allowed >= 5
    assert vs.suggested_clip_length(3.0, "veo3") == 4
    assert vs.suggested_clip_length(7.0, "veo3") is None   # doesn't fit → image
    assert vs.suggested_clip_length(11.0, "kling3") == 15


def test_scene_eligibility_consistent_with_est_seconds():
    res = vs.segment(NARRATION, mode="B", clip_model="veo3")
    for s in res.scenes:
        assert s.clip_eligible == vs.clip_fits(s.est_seconds, "veo3")
        if s.clip_eligible:
            assert s.suggested_clip_seconds is not None
        else:
            assert s.suggested_clip_seconds is None


# ══════════════════════════════════════════════════════════════════════════════
# Edge cases & robustness
# ══════════════════════════════════════════════════════════════════════════════
def test_empty_text():
    res = vs.segment("", mode="B")
    assert res.scenes == []
    assert res.actual_words == 0
    assert res.truncated is False


def test_unpunctuated_text_still_chunks():
    # no sentence terminators, but enough words → word-chunk fallback to scene_count
    blob = " ".join(["kata"] * 260)   # ~2 min worth, but one "sentence"
    res = vs.segment(blob, mode="A", minutes=2)
    assert len(res.scenes) == 6
    assert " ".join(s.text for s in res.scenes).split() == blob.split()


def test_fewer_sentences_than_scenes_keeps_params_honest():
    res = vs.segment("Satu kalimat saja.", mode="A", minutes=5)
    # only one sentence → can't make 14 scenes; params must reflect reality
    assert len(res.scenes) == res.params.scene_count
    assert res.params.scene_count >= 1


# ══════════════════════════════════════════════════════════════════════════════
# CLI smoke
# ══════════════════════════════════════════════════════════════════════════════
def test_cli_self_test_passes():
    assert vs.main(["--self-test"]) == 0


def test_cli_all_durations_runs(capsys):
    assert vs.main(["--all-durations"]) == 0
    out = capsys.readouterr().out
    assert "30 sec" in out and "15 min" in out


def test_serialization_roundtrip():
    res = vs.segment(NARRATION, mode="B")
    d = res.to_dict()
    assert d["mode"] == "B"
    assert isinstance(d["scenes"], list) and d["scenes"]
    assert set(d["params"]).issuperset({"scene_count", "credits", "batch_plan"})


# ══════════════════════════════════════════════════════════════════════════════
# Step 6d — decide_visual_modes (full-clips / full-images / hybrid + fit gate)
# ══════════════════════════════════════════════════════════════════════════════
# 4 scenes; on veo3 (gate 6.8s) scenes 0,2,3 fit, scene 1 (9.2s) does not.
def _decide_scenes():
    return [
        {"number": 1, "word_count": 10, "est_seconds": 4.6, "position": "opening", "visual_prompt": "a"},
        {"number": 2, "word_count": 20, "est_seconds": 9.2, "position": "middle", "visual_prompt": "b"},
        {"number": 3, "word_count": 8,  "est_seconds": 3.7, "position": "middle", "visual_prompt": "c"},
        {"number": 4, "word_count": 12, "est_seconds": 5.5, "position": "closing", "visual_prompt": "d"},
    ]


def test_decide_full_images():
    out = vs.decide_visual_modes(_decide_scenes(), "full_images", "veo3")
    assert [s["kind"] for s in out] == ["image"] * 4
    assert all(s["suggested_clip_seconds"] is None for s in out)


def test_decide_full_clips_forces_all_clips():
    out = vs.decide_visual_modes(_decide_scenes(), "full_clips", "veo3")
    # full_clips is an EXPLICIT user choice → every scene is a clip, even one that
    # overshoots the fit gate (it gets the longest allowed length; the stitcher
    # trims/pads to the measured audio). Only hybrid auto-respects the gate.
    assert [s["kind"] for s in out] == ["clip", "clip", "clip", "clip"]
    assert all(s["suggested_clip_seconds"] for s in out)


def test_decide_hybrid_ratio_one_clips_all_eligible():
    out = vs.decide_visual_modes(_decide_scenes(), "hybrid", "veo3", clip_ratio=1.0,
                                 merit_scores=[1, 1, 1, 1])
    assert [s["kind"] for s in out] == ["clip", "image", "clip", "clip"]


def test_decide_hybrid_ratio_zero_no_clips():
    out = vs.decide_visual_modes(_decide_scenes(), "hybrid", "veo3", clip_ratio=0.0)
    assert all(s["kind"] == "image" for s in out)


def test_decide_hybrid_never_picks_ineligible_even_if_top_merit():
    # scene 1 has the highest merit but doesn't fit → must stay an image
    out = vs.decide_visual_modes(_decide_scenes(), "hybrid", "veo3", clip_ratio=0.5,
                                 merit_scores=[10, 99, 50, 20])
    assert out[1]["kind"] == "image"
    # k = round(4*0.5)=2 → the two highest-merit ELIGIBLE scenes (2 then 3)
    assert [s["kind"] for s in out] == ["image", "image", "clip", "clip"]


def test_decide_hybrid_default_ratio_is_about_30pct():
    out = vs.decide_visual_modes(_decide_scenes(), "hybrid", "veo3")  # ratio 0.3 → 1 clip
    assert sum(1 for s in out if s["kind"] == "clip") == 1


def test_decide_kling_higher_ceiling_keeps_more_eligible():
    # the 9.2s scene fits kling3 (gate 12.7s) but not veo3
    out = vs.decide_visual_modes(_decide_scenes(), "full_clips", "kling3")
    assert out[1]["kind"] == "clip"


def test_decide_coerces_string_est_seconds_without_crashing():
    # est_seconds can arrive as a string across the Python→JSON→Python boundary;
    # a bad value must fall back to the word-count estimate, never raise (was a 500).
    out = vs.decide_visual_modes(
        [{"word_count": 10, "est_seconds": "4.6"},   # → 4.6s, fits veo3
         {"word_count": 20, "est_seconds": "bad"},   # → recompute 9.2s, doesn't fit the gate
         {"word_count": 8}],                          # → 3.7s, fits
        "full_clips", "veo3")
    # the point is no crash on the bad string; full_clips forces all to clips
    assert [s["kind"] for s in out] == ["clip", "clip", "clip"]


def test_clip_modes_size_scenes_to_fit_clips():
    # The bug: image-sized scenes (~15s for 30s/2-scene) never fit a Veo clip, so
    # 'Semua klip' silently produced stills. Clip modes must cut scenes down so
    # they're clip-eligible; full_images keeps the long, cheap scenes.
    imgs = vs.calculate_video_params(0.5, "fast", "full_images", "veo3")
    clips = vs.calculate_video_params(0.5, "fast", "full_clips", "veo3")
    assert clips.scene_count > imgs.scene_count          # more, shorter scenes for clips
    assert vs.clip_fits(clips.seconds_per_scene, "veo3")  # and they actually fit a clip
    assert not vs.clip_fits(imgs.seconds_per_scene, "veo3")
    # kling3's higher ceiling allows longer scenes than veo3
    assert vs.words_per_scene_for("full_clips", "kling3") > vs.words_per_scene_for("full_clips", "veo3")
    assert vs.words_per_scene_for("full_images", "veo3") == vs.WORDS_PER_SCENE
