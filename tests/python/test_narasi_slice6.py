"""
Dalang v2 — Slice 6: strip the dead Gutenberg narration RAG (§8).

Confirms the phantom module import is gone, /rag/context is a disabled stub (still routed
so server.js doesn't 404), the generate loop no longer references moat.gutenberg, and the
LIVE visual corpus (nusantara_visual_v1) + the RAG_ENABLED kill switch are retained.
"""
import inspect
import pytest

import laozhang_api


class TestDeadRagRemoved:
    def test_rag_available_false(self):
        assert laozhang_api.RAG_AVAILABLE is False

    def test_kill_switch_retained(self):
        # RAG_ENABLED (the kill switch) is kept per §8, even though there's no live path now.
        assert hasattr(laozhang_api, "RAG_ENABLED")

    def test_no_live_phantom_import_in_generate_loop(self):
        src = inspect.getsource(laozhang_api._narasi_generate_impl)
        assert "moat.gutenberg" not in src
        assert "get_narration_context" not in src

    def test_rag_context_endpoint_is_disabled_stub(self, client):
        resp = client.post("/rag/context", json={"topic": "x", "style": "epic"})
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is False
        assert body.get("context_text") == "" and body.get("passages") == 0

    def test_rag_context_source_has_no_phantom_import(self):
        src = inspect.getsource(laozhang_api.rag_context)
        assert "moat.gutenberg" not in src


class TestVisualCorpusUntouched:
    def test_nusantara_visual_collection_intact(self):
        # The visual prompt-enhancement corpus is a DIFFERENT system — must be untouched.
        import nusantara_corpus
        assert nusantara_corpus._COLLECTION == "nusantara_visual_v1"
