"""
Avatar VI-mode segmenter (Slice 1) — /video/segment/avatar (Dalang-owned).

Covers the spec DoD (~/docs/wimba-avatar-slice1-spec.md §Tests):
  role→kind mapping · scene count ≤ SPK_MAX_SEGMENTS · every est_seconds ≤ SPK_MAX_SECONDS
  (over-cap split) · planner failure → single presenter scene · gate-only (no metering/debit).

The pure transform (_avatar_scenes_from_segments) is exercised directly (deterministic, no LLM);
the route is smoke-tested with the Spokesperson planner stubbed.
"""
import asyncio
import inspect

import laozhang_api


SEG = laozhang_api._avatar_scenes_from_segments


class TestRoleToKind:
    def test_presenter_avatar_broll_clip(self):
        scenes = SEG([
            {"role": "presenter", "text": "hook line here"},
            {"role": "broll", "text": "cutaway line", "broll_brief": "a city skyline"},
        ])
        assert [s["kind"] for s in scenes] == ["avatar", "clip"]

    def test_broll_visual_prompt_from_brief_presenter_empty(self):
        scenes = SEG([
            {"role": "presenter", "text": "talk", "broll_brief": "ignored for presenter"},
            {"role": "broll", "text": "cut", "broll_brief": "a forest at dawn"},
        ])
        assert scenes[0]["visual_prompt"] == ""                 # presenter carries no visual prompt
        assert scenes[1]["visual_prompt"] == "a forest at dawn"

    def test_unknown_role_defaults_to_presenter(self):
        assert SEG([{"role": "weird", "text": "x"}])[0]["kind"] == "avatar"


class TestCaps:
    def test_count_capped_at_max_segments(self):
        segs = [{"role": "presenter", "text": f"beat {i}"} for i in range(20)]
        assert len(SEG(segs, max_segments=8, max_seconds=45)) == 8

    def test_over_duration_segment_split_each_within_cap(self):
        long_text = " ".join(f"w{i}" for i in range(250))   # 250/2.2 ≈ 113s > 45s → must split
        scenes = SEG([{"role": "presenter", "text": long_text}], max_segments=8, max_seconds=45)
        assert len(scenes) >= 2
        assert all(s["est_seconds"] <= 45 for s in scenes)
        assert all(s["kind"] == "avatar" for s in scenes)     # split keeps the source role

    def test_split_still_bounded_by_count_cap(self):
        huge = " ".join(f"w{i}" for i in range(5000))
        assert len(SEG([{"role": "presenter", "text": huge}], max_segments=8, max_seconds=45)) == 8

    def test_est_seconds_is_words_over_2_2(self):
        scenes = SEG([{"role": "presenter", "text": " ".join(["w"] * 11)}])
        assert scenes[0]["est_seconds"] == 5.0                 # 11 / 2.2 = 5.0

    def test_every_scene_within_duration_cap_default(self):
        long_text = " ".join(f"w{i}" for i in range(400))
        assert all(s["est_seconds"] <= laozhang_api._avatar_scenes_from_segments.__defaults__[2]
                   for s in SEG([{"role": "broll", "text": long_text, "broll_brief": "b"}]))


class TestFallback:
    def test_empty_segments_single_presenter_scene(self):
        scenes = SEG([], fallback_text="a topic sentence")
        assert len(scenes) == 1 and scenes[0]["kind"] == "avatar"
        assert scenes[0]["text"] == "a topic sentence"

    def test_all_blank_text_falls_back(self):
        scenes = SEG([{"role": "presenter", "text": "   "}, {"role": "broll", "text": ""}],
                     fallback_text="fallback")
        assert len(scenes) == 1 and scenes[0]["text"] == "fallback"

    def test_numbering_sequential_1_based(self):
        scenes = SEG([{"role": "presenter", "text": "a"},
                      {"role": "broll", "text": "b", "broll_brief": "x"}])
        assert [s["number"] for s in scenes] == [1, 2]

    def test_non_dict_segments_skipped(self):
        scenes = SEG(["garbage", None, {"role": "presenter", "text": "ok"}], fallback_text="fb")
        assert [s["text"] for s in scenes] == ["ok"]


class TestSceneFieldContract:
    def test_scene_has_exactly_the_fields_store_reads(self):
        # store.mjs createJob HSET reads: number, text, visual_prompt, kind, est_seconds
        s = SEG([{"role": "broll", "text": "hi", "broll_brief": "b"}])[0]
        assert set(s.keys()) == {"number", "text", "visual_prompt", "kind", "est_seconds"}


class TestRoute:
    def test_returns_role_tagged_scenes_from_stubbed_plan(self, monkeypatch):
        async def fake_plan(inp):
            assert inp["broll"]["on"] is True                  # avatar forces broll-mode plan
            return {"script": "full script", "segments": [
                {"role": "presenter", "text": "hook"},
                {"role": "broll", "text": "cut", "broll_brief": "skyline"},
            ]}
        import recipe_spokesperson
        monkeypatch.setattr(recipe_spokesperson, "plan", fake_plan)
        req = laozhang_api.VideoAvatarSegmentReq(topic="t", broll=True)
        out = asyncio.run(laozhang_api.video_segment_avatar(req, request=None, user=None))
        assert [s["kind"] for s in out["scenes"]] == ["avatar", "clip"]
        assert out["script"] == "full script"

    def test_plan_exception_degrades_to_single_presenter(self, monkeypatch):
        async def boom(inp):
            raise RuntimeError("llm down")
        import recipe_spokesperson
        monkeypatch.setattr(recipe_spokesperson, "plan", boom)
        req = laozhang_api.VideoAvatarSegmentReq(topic="just a topic", broll=True)
        out = asyncio.run(laozhang_api.video_segment_avatar(req, request=None, user=None))
        assert len(out["scenes"]) == 1 and out["scenes"][0]["kind"] == "avatar"
        assert "just a topic" in out["scenes"][0]["text"]

    def test_broll_off_plan_returns_script_only(self, monkeypatch):
        async def fake_plan(inp):
            return {"script": "narration only"}                # no segments key (b-roll off shape)
        import recipe_spokesperson
        monkeypatch.setattr(recipe_spokesperson, "plan", fake_plan)
        req = laozhang_api.VideoAvatarSegmentReq(topic="t", broll=False)
        out = asyncio.run(laozhang_api.video_segment_avatar(req, request=None, user=None))
        assert len(out["scenes"]) == 1 and out["scenes"][0]["kind"] == "avatar"
        assert out["scenes"][0]["text"] == "narration only"


class TestGateOnly:
    def test_handler_has_no_metering_or_debit(self):
        # Scan for actual metering/charge CALL sites (not bare words — the docstring legitimately
        # says "no metering/debit"). None may appear in a gate-only segment route.
        src = inspect.getsource(laozhang_api.video_segment_avatar)
        for banned in ("begin_charge", "credits_lib", "metering.", ".debit(", ".settle(",
                       "grant_entitlement", "_hold("):
            assert banned not in src, f"avatar segmenter must be gate-only; found '{banned}'"

    def test_route_registered(self):
        paths = {getattr(r, "path", None) for r in laozhang_api.app.routes}
        assert "/video/segment/avatar" in paths
        assert "/video/segment" in paths          # sibling untouched / still present
