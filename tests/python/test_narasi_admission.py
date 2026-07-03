"""
Dalang v2 — Slice 0 admission net (hermetic).

Covers §7: input clamp (fan-out ceilings), model whitelist (off-list / unpriced →
reject), and the inflight cap. Two layers:
  1. Unit tests of the pure helpers (_narasi_admit / _narasi_model_ok) — fast, no route.
  2. Route tests via the shared TestClient — admission runs BEFORE any auth-DB call or
     credit hold, so an over-limit / unpriced / saturated request short-circuits with
     no LLM, DB, or metering touched (fully hermetic).

Flag semantics: DALANG_ADMISSION_ENABLED is read live per call, so monkeypatch.setenv
toggles it without re-import. Default OFF ⟹ admission is a strict no-op (byte-identical
to pre-Slice-0 behavior) — asserted explicitly.
"""
import uuid
import pytest
from unittest.mock import patch, MagicMock

import laozhang_api
from fastapi import HTTPException


TENANT_A = str(uuid.uuid4())
USER_A = str(uuid.uuid4())


def _make_user(tenant_id=TENANT_A, user_id=USER_A):
    from auth_middleware import CurrentUser
    return CurrentUser(tenant_id=tenant_id, user_id=user_id, plan="free", tier="free")


def _gen_body(n_chapters=2, words=400, model="gemini-2.5-flash"):
    return {
        "model": model,
        "topic": "t",
        "chapters": [{"id": str(i), "title": f"Bab {i}", "words": words}
                     for i in range(n_chapters)],
    }


# ── Unit: the pure admission helper ──────────────────────────────────────────

class TestNarasiAdmitUnit:
    def test_noop_when_flag_off(self, monkeypatch):
        """Flag OFF (default) ⟹ even an egregiously over-limit request is a no-op."""
        monkeypatch.delenv("DALANG_ADMISSION_ENABLED", raising=False)
        # 999 chapters would be rejected if enabled — must NOT raise when off.
        laozhang_api._narasi_admit(_gen_body(n_chapters=999), kind="generate")

    def test_over_limit_chapters_rejected(self, monkeypatch):
        monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")
        with pytest.raises(HTTPException) as ei:
            laozhang_api._narasi_admit(_gen_body(n_chapters=999), kind="generate")
        assert ei.value.status_code == 400

    def test_over_limit_words_per_chapter_rejected(self, monkeypatch):
        monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")
        big = laozhang_api.DALANG_MAX_WORDS_PER_CHAPTER + 1
        with pytest.raises(HTTPException) as ei:
            laozhang_api._narasi_admit(_gen_body(n_chapters=1, words=big), kind="generate")
        assert ei.value.status_code == 400

    def test_total_words_rejected(self, monkeypatch):
        monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")
        # Each chapter under the per-chapter cap, but the sum blows the total ceiling.
        per = laozhang_api.DALANG_MAX_WORDS_PER_CHAPTER
        n = (laozhang_api.DALANG_MAX_TOTAL_WORDS // per) + 2
        n = min(n, laozhang_api.DALANG_MAX_CHAPTERS)  # stay within the chapter-count cap
        # Force the total over the ceiling using max per-chapter words.
        body = _gen_body(n_chapters=n, words=per)
        if sum(c["words"] for c in body["chapters"]) <= laozhang_api.DALANG_MAX_TOTAL_WORDS:
            pytest.skip("configured ceilings don't allow a total-only breach within chapter cap")
        with pytest.raises(HTTPException) as ei:
            laozhang_api._narasi_admit(body, kind="generate")
        assert ei.value.status_code == 400

    def test_unpriced_model_rejected(self, monkeypatch):
        monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")
        with pytest.raises(HTTPException) as ei:
            laozhang_api._narasi_admit(_gen_body(model="totally-fake-model-xyz"), kind="generate")
        assert ei.value.status_code == 400

    def test_valid_request_passes(self, monkeypatch):
        monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")
        # Must NOT raise — a legitimate small job.
        laozhang_api._narasi_admit(_gen_body(n_chapters=3, words=400), kind="generate")

    def test_outline_chap_count_rejected(self, monkeypatch):
        monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")
        with pytest.raises(HTTPException) as ei:
            laozhang_api._narasi_admit(
                {"model": "gemini-2.5-flash",
                 "chap_count": laozhang_api.DALANG_MAX_CHAPTERS + 5,
                 "word_max": 4500}, kind="outline")
        assert ei.value.status_code == 400

    def test_outline_valid_passes(self, monkeypatch):
        monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")
        laozhang_api._narasi_admit(
            {"model": "gemini-2.5-flash", "chap_count": 5, "word_max": 4500}, kind="outline")

    def test_outline_word_max_is_total_not_per_chapter(self, monkeypatch):
        """Regression guard: outline word_max is the TOTAL book budget, so a large total
        under DALANG_MAX_TOTAL_WORDS must be allowed (not rejected at the per-chapter cap)."""
        monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")
        big_total = laozhang_api.DALANG_MAX_WORDS_PER_CHAPTER + 20000  # > per-chapter, < total
        assert big_total < laozhang_api.DALANG_MAX_TOTAL_WORDS
        laozhang_api._narasi_admit(
            {"model": "gemini-2.5-flash", "chap_count": 12, "word_max": big_total}, kind="outline")

    def test_outline_total_over_ceiling_rejected(self, monkeypatch):
        monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")
        with pytest.raises(HTTPException) as ei:
            laozhang_api._narasi_admit(
                {"model": "gemini-2.5-flash", "chap_count": 5,
                 "word_max": laozhang_api.DALANG_MAX_TOTAL_WORDS + 1}, kind="outline")
        assert ei.value.status_code == 400

    def test_non_dict_chapter_rejected_cleanly(self, monkeypatch):
        """A chapters list containing non-dict items must 400 (HTTPException), never 500."""
        monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")
        with pytest.raises(HTTPException) as ei:
            laozhang_api._narasi_admit(
                {"model": "gemini-2.5-flash", "chapters": ["not-a-dict", 123]}, kind="generate")
        assert ei.value.status_code == 400


class TestNarasiModelOk:
    def test_known_priced_model_ok(self):
        assert laozhang_api._narasi_model_ok("gemini-2.5-flash") is True

    def test_off_list_model_rejected(self):
        assert laozhang_api._narasi_model_ok("gpt-9-imaginary") is False

    def test_empty_model_rejected(self):
        assert laozhang_api._narasi_model_ok("") is False


# ── Route: admission short-circuits before any hold/DB (hermetic) ─────────────

class TestNarasiGenerateRouteAdmission:
    def _override(self, app, user):
        from auth_middleware import get_current_user
        async def _dep():
            return user
        app.dependency_overrides[get_current_user] = _dep

    def test_over_limit_returns_400(self, client, app, monkeypatch):
        monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")
        self._override(app, _make_user())
        try:
            # make_client patched so a mis-fire can't reach the real relay.
            with patch("laozhang_api.make_client", return_value=MagicMock()):
                resp = client.post("/narasi/generate", json=_gen_body(n_chapters=999),
                                   headers={"Authorization": "Bearer test"})
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 400, resp.text

    def test_unpriced_model_returns_400(self, client, app, monkeypatch):
        monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")
        self._override(app, _make_user())
        try:
            with patch("laozhang_api.make_client", return_value=MagicMock()):
                resp = client.post("/narasi/generate",
                                   json=_gen_body(model="totally-fake-model-xyz"),
                                   headers={"Authorization": "Bearer test"})
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 400, resp.text

    def test_inflight_cap_returns_429(self, client, app, monkeypatch):
        monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")
        # Saturate the counter — the early check raises 429 before any await.
        monkeypatch.setattr(laozhang_api, "_narasi_inflight",
                            laozhang_api.DALANG_MAX_INFLIGHT + 10)
        self._override(app, _make_user())
        try:
            with patch("laozhang_api.make_client", return_value=MagicMock()):
                resp = client.post("/narasi/generate", json=_gen_body(n_chapters=1),
                                   headers={"Authorization": "Bearer test"})
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 429, resp.text
