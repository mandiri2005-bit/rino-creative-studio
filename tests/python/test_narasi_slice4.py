"""
Dalang v2 — Slice 4: fresh-context critic gate + revise + moat_sessions fill.

Pure sampling logic (_narasi_should_critique) and the critic/revise helpers are exercised
with stubbed LLM; the loop wiring + moat fix are locked by source inspection.

Flag: DALANG_CRITIC_ENABLED (sub-flag, requires v2; default OFF).
"""
import asyncio
import inspect
import pytest

import laozhang_api


class _FakeResp:
    def __init__(self, content, finish="stop"):
        self.usage = type("U", (), {"prompt_tokens": 100, "completion_tokens": 50})()
        ch = type("Ch", (), {})()
        ch.message = type("M", (), {"content": content})()
        ch.finish_reason = finish
        self.choices = [ch]


class _FakeClient:
    def __init__(self, content, finish="stop"):
        self.calls = []
        comp = type("Comp", (), {})()
        def create(**kw):
            self.calls.append(kw)
            return _FakeResp(content, finish)
        comp.create = create
        self.chat = type("Chat", (), {"completions": comp})()


def _patch_usage(monkeypatch, cost=4):
    async def fake_usage(*a, **k):
        return cost
    monkeypatch.setattr(laozhang_api, "_log_narasi_usage", fake_usage)


class TestFlags:
    def test_critic_requires_v2(self, monkeypatch):
        monkeypatch.delenv("DALANG_V2_ENABLED", raising=False)
        monkeypatch.setenv("DALANG_CRITIC_ENABLED", "1")
        assert laozhang_api._dalang_critic_enabled() is False
        monkeypatch.setenv("DALANG_V2_ENABLED", "1")
        assert laozhang_api._dalang_critic_enabled() is True


class TestShouldCritique:
    def test_short_book_never(self):
        # total not > MIN (default 5) ⟹ never critique
        assert all(not laozhang_api._narasi_should_critique(i, 5) for i in range(5))

    def test_every_k_for_long_book(self):
        # MIN=5, K=3, total=6 ⟹ critique at i=2 (bab 3) and i=5 (bab 6)
        got = [i for i in range(6) if laozhang_api._narasi_should_critique(i, 6)]
        assert got == [2, 5]


class TestCritic:
    def test_parses_verdict_and_bills(self, monkeypatch):
        _patch_usage(monkeypatch, cost=4)
        monkeypatch.setattr(laozhang_api, "make_client",
                            lambda m: _FakeClient('{"score": 7.5, "coherence_violations": ["kontradiksi umur"], "notes": "ok"}'))
        v, cr = asyncio.run(laozhang_api._narasi_critic(
            "teks bab", "storytelling", "id", tenant_id="t", user_id="u", job_uuid=None))
        assert v["score"] == 7.5
        assert v["coherence_violations"] == ["kontradiksi umur"]
        assert cr == 4

    def test_bad_json_degrades_gracefully(self, monkeypatch):
        _patch_usage(monkeypatch, cost=1)
        monkeypatch.setattr(laozhang_api, "make_client", lambda m: _FakeClient("not json"))
        v, cr = asyncio.run(laozhang_api._narasi_critic(
            "x", "s", "id", tenant_id="t", user_id="u", job_uuid=None))
        assert v["score"] is None and v["coherence_violations"] == []

    def test_json_array_response_never_raises(self, monkeypatch):
        # Review #6 (HIGH): a bare JSON array must not crash .get() and discard the chapter.
        _patch_usage(monkeypatch, cost=1)
        monkeypatch.setattr(laozhang_api, "make_client", lambda m: _FakeClient("[1, 2, 3]"))
        v, cr = asyncio.run(laozhang_api._narasi_critic(
            "x", "s", "id", tenant_id="t", user_id="u", job_uuid=None))
        assert v["score"] is None and v["coherence_violations"] == []

    def test_out_of_range_score_dropped(self, monkeypatch):
        # Review #5: 99 / NaN must not survive into the result payload.
        _patch_usage(monkeypatch, cost=1)
        monkeypatch.setattr(laozhang_api, "make_client", lambda m: _FakeClient('{"score": 99}'))
        v, cr = asyncio.run(laozhang_api._narasi_critic(
            "x", "s", "id", tenant_id="t", user_id="u", job_uuid=None))
        assert v["score"] is None


class TestRevise:
    def test_kept_rewrite_is_billed(self, monkeypatch):
        _patch_usage(monkeypatch, cost=6)
        client = _FakeClient(" ".join(["kata"] * 120))   # long rewrite → kept
        text, cr = asyncio.run(laozhang_api._narasi_revise(
            client, "m", "m", 8000, [{"role": "user", "content": "x"}], "asli pendek",
            {"coherence_violations": ["a"], "notes": "b"},
            tenant_id="t", user_id="u", job_uuid=None))
        assert cr == 6 and text.startswith("kata")

    def test_degenerate_rewrite_kept_original_not_billed(self, monkeypatch):
        _patch_usage(monkeypatch, cost=6)
        client = _FakeClient("terlalu pendek")   # < 50 words → keep original, don't bill
        text, cr = asyncio.run(laozhang_api._narasi_revise(
            client, "m", "m", 8000, [{"role": "user", "content": "x"}], "teks asli",
            {"coherence_violations": ["a"]}, tenant_id="t", user_id="u", job_uuid=None))
        assert cr == 0 and text == "teks asli"

    def test_empty_choices_keeps_original(self, monkeypatch):
        # Review #3: empty choices[] (content-filter) must keep the already-generated chapter,
        # never raise IndexError (which would discard it).
        _patch_usage(monkeypatch, cost=6)
        empty = type("C", (), {})()
        comp = type("Comp", (), {})()
        comp.create = lambda **kw: type("R", (), {"choices": [], "usage": None})()
        empty.chat = type("Chat", (), {"completions": comp})()
        text, cr = asyncio.run(laozhang_api._narasi_revise(
            empty, "m", "m", 8000, [{"role": "user", "content": "x"}], "asli",
            {"coherence_violations": ["a"]}, tenant_id="t", user_id="u", job_uuid=None))
        assert cr == 0 and text == "asli"


class TestMoatFillSource:
    def test_moat_call_no_longer_zeros(self):
        src = inspect.getsource(laozhang_api._narasi_generate_impl)
        # the generate-path moat capture must pass real tokens + cost, not 0,0,0
        assert "model, 0, 0, 0)" not in src
        assert "_calc_cost(model, _mt_ti, _mt_to)" in src


class TestLoopWiringSource:
    def test_critic_wired_and_gated(self):
        src = inspect.getsource(laozhang_api._narasi_generate_impl)
        assert "_dalang_critic_enabled()" in src
        assert "_narasi_should_critique(i, len(chapters))" in src
        assert "_narasi_critic(" in src and "_narasi_revise(" in src
        assert "chapter_scores" in src and "_book_score" in src
