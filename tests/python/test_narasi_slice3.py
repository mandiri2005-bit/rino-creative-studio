"""
Dalang v2 — Slice 3: Series State + word-enforcement gate + bounded re-call + partial.

Pure gates (word verdict, job-partial, json parse) and the continuation/cheap-call helpers
are exercised behaviorally with stubbed LLM; the migration/DB/loop wiring is locked with
source + migration-file inspection (needs a real DB to run live).

Flags: DALANG_V2_ENABLED (default OFF ⟹ loop byte-identical), DALANG_FACT_LEDGER_ENABLED
(sub-flag, requires v2).
"""
import asyncio
import inspect
import os
import pytest

import laozhang_api
import database

MIG = os.path.join(os.path.dirname(__file__), "../../database/migrations/0055_series_state.sql")


# ── LLM stubs ────────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, content, finish="stop"):
        self.usage = None
        choice = type("Ch", (), {})()
        choice.message = type("M", (), {"content": content})()
        choice.finish_reason = finish
        self.choices = [choice]


class _FakeClient:
    def __init__(self, content, finish="stop"):
        self.calls = []
        comp = type("Comp", (), {})()
        def create(**kw):
            self.calls.append(kw)
            return _FakeResp(content, finish)
        comp.create = create
        self.chat = type("Chat", (), {"completions": comp})()


# ── Flags ────────────────────────────────────────────────────────────────────
class TestFlags:
    def test_v2_default_off(self, monkeypatch):
        monkeypatch.delenv("DALANG_V2_ENABLED", raising=False)
        assert laozhang_api._dalang_v2_enabled() is False

    def test_fact_ledger_requires_v2(self, monkeypatch):
        monkeypatch.delenv("DALANG_V2_ENABLED", raising=False)
        monkeypatch.setenv("DALANG_FACT_LEDGER_ENABLED", "1")
        assert laozhang_api._dalang_fact_ledger_enabled() is False   # v2 off ⟹ still off
        monkeypatch.setenv("DALANG_V2_ENABLED", "1")
        assert laozhang_api._dalang_fact_ledger_enabled() is True


# ── Deterministic word gate ──────────────────────────────────────────────────
class TestWordVerdict:
    def test_undershoot(self):
        v = laozhang_api._narasi_word_verdict("one two three", 10, 20, "stop")
        assert v["words"] == 3 and v["undershoot"] is True and v["shortfall"] == 7
        assert v["overshoot"] is False and v["truncated"] is False

    def test_in_range(self):
        v = laozhang_api._narasi_word_verdict(" ".join(["w"] * 15), 10, 20, "stop")
        assert v["undershoot"] is False and v["overshoot"] is False and v["shortfall"] == 0

    def test_overshoot(self):
        v = laozhang_api._narasi_word_verdict(" ".join(["w"] * 30), 10, 20, "stop")
        assert v["overshoot"] is True

    def test_truncated(self):
        v = laozhang_api._narasi_word_verdict(" ".join(["w"] * 15), 10, 20, "length")
        assert v["truncated"] is True


class TestJobPartial:
    def test_partial_when_below_floor(self):
        # targets 1000,1000 → floor 1800; delivered 400+400=800 < 1800 → partial
        r = laozhang_api._narasi_job_partial([1000, 1000], [400, 400])
        assert r["partial"] is True and r["floor_words"] == 1800
        assert len(r["short_chapters"]) == 2

    def test_not_partial_when_meets_floor(self):
        r = laozhang_api._narasi_job_partial([1000, 1000], [950, 980])
        assert r["partial"] is False and r["short_chapters"] == []

    def test_empty_never_partial(self):
        assert laozhang_api._narasi_job_partial([1000], [])["partial"] is False


class TestParseJson:
    def test_fenced(self):
        assert laozhang_api._narasi_parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_bare(self):
        assert laozhang_api._narasi_parse_json('{"a": 1}') == {"a": 1}

    def test_prose_wrapped(self):
        assert laozhang_api._narasi_parse_json('sure! {"a": 1} done') == {"a": 1}

    def test_array(self):
        assert laozhang_api._narasi_parse_json("[1, 2, 3]") == [1, 2, 3]

    def test_garbage_none(self):
        assert laozhang_api._narasi_parse_json("not json at all") is None


# ── Bounded continuation re-call (H2) ────────────────────────────────────────
class TestContinuation:
    def _patch_usage(self, monkeypatch, cost=3):
        async def fake_usage(*a, **k):
            return cost
        monkeypatch.setattr(laozhang_api, "_log_narasi_usage", fake_usage)

    def test_loops_and_caps_and_bills(self, monkeypatch):
        self._patch_usage(monkeypatch, cost=3)
        client = _FakeClient("tambahan kata pendek", finish="stop")   # keeps it under word_min
        text0 = "awalnya pendek"
        verdict = laozhang_api._narasi_word_verdict(text0, 1000, 2000, "stop")  # undershoot
        text, extra = asyncio.run(laozhang_api._narasi_continuation(
            client, "gemini-2.5-flash", "gemini-2.5-flash", 8000,
            [{"role": "user", "content": "x"}], text0, verdict,
            tenant_id="t", user_id="u", job_uuid=None, word_min=1000, word_max=2000))
        # capped at DALANG_MAX_CHAPTER_RETRIES (default 2), each billed, each appended
        assert len(client.calls) == laozhang_api.DALANG_MAX_CHAPTER_RETRIES
        assert extra == 3 * laozhang_api.DALANG_MAX_CHAPTER_RETRIES
        assert text.count("tambahan kata pendek") == laozhang_api.DALANG_MAX_CHAPTER_RETRIES
        assert text.startswith(text0)

    def test_empty_addition_logged_but_not_billed(self, monkeypatch):
        # Review fix: an attempt returning tokens-but-empty-text is logged (COGS) yet NOT
        # folded into the chapter cost (nothing kept), matching the primary-retry rule.
        self._patch_usage(monkeypatch, cost=5)
        client = _FakeClient("", finish="stop")   # empty content
        verdict = laozhang_api._narasi_word_verdict("short", 1000, 2000, "stop")
        text, extra = asyncio.run(laozhang_api._narasi_continuation(
            client, "m", "m", 8000, [{"role": "user", "content": "x"}], "short", verdict,
            tenant_id="t", user_id="u", job_uuid=None, word_min=1000, word_max=2000))
        assert extra == 0 and text == "short" and len(client.calls) == 1

    def test_noop_when_satisfied(self, monkeypatch):
        self._patch_usage(monkeypatch)
        client = _FakeClient("should not be called")
        verdict = {"undershoot": False, "truncated": False, "words": 500, "shortfall": 0}
        text, extra = asyncio.run(laozhang_api._narasi_continuation(
            client, "m", "m", 8000, [{"role": "user", "content": "x"}], "fine text", verdict,
            tenant_id="t", user_id="u", job_uuid=None, word_min=10, word_max=999))
        assert client.calls == [] and extra == 0 and text == "fine text"


class TestCheapCall:
    def test_returns_text_and_cost(self, monkeypatch):
        async def fake_usage(*a, **k):
            return 2
        monkeypatch.setattr(laozhang_api, "_log_narasi_usage", fake_usage)
        monkeypatch.setattr(laozhang_api, "make_client", lambda m: _FakeClient('{"facts": []}'))
        text, cr = asyncio.run(laozhang_api._narasi_cheap_call(
            "sys", "usr", tenant_id="t", user_id="u", json_mode=True))
        assert cr == 2 and text == '{"facts": []}'


# ── Migration 0055 ───────────────────────────────────────────────────────────
class TestMigration0055:
    def _sql(self):
        return open(MIG).read()

    def test_three_tables(self):
        s = self._sql()
        for t in ("series", "series_facts", "series_summary"):
            assert f"CREATE TABLE IF NOT EXISTS {t} " in s

    def test_kind_check_and_fact_unique(self):
        s = self._sql()
        assert "kind IN ('book','channel')" in s
        assert "UNIQUE (series_id, entity_key)" in s   # enables the fact upsert

    def test_rls_enabled_forced_isolated(self):
        s = self._sql()
        assert "ENABLE ROW LEVEL SECURITY" in s and "FORCE  ROW LEVEL SECURITY" in s
        assert "CREATE POLICY tenant_isolation" in s and "WITH CHECK" in s
        assert "GRANT SELECT, INSERT, UPDATE, DELETE ON series, series_facts, series_summary TO app_user" in s

    def test_updated_at_triggers(self):
        s = self._sql()
        assert "trg_series_facts_updated_at" in s and "trg_series_summary_updated_at" in s


class TestDbSource:
    def test_facts_upsert_keyed(self):
        assert "ON CONFLICT (series_id, entity_key)" in inspect.getsource(database.upsert_series_facts)

    def test_summary_upsert_keyed(self):
        assert "ON CONFLICT (series_id)" in inspect.getsource(database.upsert_series_summary)


class TestLoopWiringSource:
    def test_v2_gates_and_recall_wired(self):
        src = inspect.getsource(laozhang_api._narasi_generate_impl)
        assert "_narasi_word_verdict" in src and "_narasi_continuation" in src
        assert "if _dalang_v2_enabled():" in src
        assert "_narasi_job_partial" in src and '"partial"' in src

    def test_fact_ledger_gated(self):
        src = inspect.getsource(laozhang_api._narasi_generate_impl)
        assert "_dalang_fact_ledger_enabled()" in src
        assert "_narasi_context_block" in src and "_narasi_update_series_state" in src
        assert "create_series" in src
