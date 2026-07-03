"""
Dalang v2 — Slice 5: repetition guard (deterministic n-gram gate + fail-open Qdrant index).

The n-gram gate is pure/deterministic; the Qdrant paths are tested for fail-open (no
QDRANT_URL in the test env ⟹ index/search skip cleanly, never raise).

Flag: DALANG_EMBED_DEDUP_ENABLED (sub-flag, requires v2; default OFF).
"""
import inspect
import pytest

import laozhang_api
import dalang_dedup


class TestFlag:
    def test_dedup_requires_v2(self, monkeypatch):
        monkeypatch.delenv("DALANG_V2_ENABLED", raising=False)
        monkeypatch.setenv("DALANG_EMBED_DEDUP_ENABLED", "1")
        assert laozhang_api._dalang_dedup_enabled() is False
        monkeypatch.setenv("DALANG_V2_ENABLED", "1")
        assert laozhang_api._dalang_dedup_enabled() is True


class TestNgram:
    def test_identical(self):
        assert dalang_dedup.ngram_jaccard("a b c d e f", "a b c d e f") == 1.0

    def test_disjoint(self):
        assert dalang_dedup.ngram_jaccard("a b c d e f", "u v w x y z") == 0.0

    def test_partial_overlap_between_0_and_1(self):
        s = dalang_dedup.ngram_jaccard("a b c d e f g", "a b c d e x y")
        assert 0.0 < s < 1.0


class TestFindRepetition:
    def test_flags_near_duplicate(self):
        prior = ["the quick brown fox jumps over the lazy dog again and again",
                 "unrelated content about space travel and rockets"]
        r = dalang_dedup.find_repetition(
            "the quick brown fox jumps over the lazy dog again and again", prior, 0.18)
        assert r["duplicate"] is True and r["of_index"] == 0 and r["score"] >= 0.18

    def test_no_dup_for_fresh_text(self):
        prior = ["completely different earlier chapter about the ocean deep"]
        r = dalang_dedup.find_repetition(
            "a brand new topic on mountain climbing techniques and gear", prior, 0.18)
        assert r["duplicate"] is False

    def test_empty_prior_never_dup(self):
        assert dalang_dedup.find_repetition("anything", [], 0.18)["duplicate"] is False


class TestPointId:
    def test_stable_and_distinct(self):
        a = dalang_dedup._point_id("series-1", 0)
        b = dalang_dedup._point_id("series-1", 0)
        c = dalang_dedup._point_id("series-1", 1)
        assert a == b and a != c and isinstance(a, int) and a > 0


class TestFailOpen:
    def test_index_chapter_no_qdrant_returns_false(self, monkeypatch):
        monkeypatch.setattr(dalang_dedup, "_QDRANT_URL", "")   # no Qdrant configured
        assert dalang_dedup.index_chapter("text", "s", "t", 0) is False

    def test_semantic_no_qdrant_returns_none(self, monkeypatch):
        monkeypatch.setattr(dalang_dedup, "_QDRANT_URL", "")
        assert dalang_dedup.semantic_repetition("text", "s", "t", 0) is None

    def test_index_no_gemini_key_returns_false(self, monkeypatch):
        # Qdrant URL present but no embed key ⟹ embed returns None ⟹ index fails open
        monkeypatch.setattr(dalang_dedup, "_QDRANT_URL", "http://localhost:6333")
        monkeypatch.setattr(dalang_dedup, "_GEMINI_API_KEY", "")
        assert dalang_dedup.index_chapter("text", "s", "t", 0) is False


class TestEmbedTaskAndReuse:
    def test_embed_defaults_to_semantic_similarity(self):
        # Symmetric chapter-vs-chapter dedup ⟹ same task both sides (comparable vectors).
        import inspect as _i
        src = _i.getsource(dalang_dedup._embed)
        assert 'task: str = "SEMANTIC_SIMILARITY"' in src

    def test_index_accepts_precomputed_vec(self):
        import inspect as _i
        sig = _i.signature(dalang_dedup.index_chapter)
        assert "vec" in sig.parameters   # reuse the search vector → no double embed


class TestLoopWiringSource:
    def test_dedup_check_index_and_rewrite_wired(self):
        src = inspect.getsource(laozhang_api._narasi_generate_impl)
        assert "_dalang_dedup_enabled()" in src
        assert "dalang_dedup.find_repetition" in src
        assert "dalang_dedup.index_chapter" in src
        assert "dalang_dedup.search_vec" in src and "dalang_dedup.embed" in src  # embed once, reuse
