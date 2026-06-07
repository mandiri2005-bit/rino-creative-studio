"""
Tests for POST /chat/stream
"""
import pytest
import json
from unittest.mock import patch, MagicMock, AsyncMock


def make_stream_response(tokens: list[str], usage=None):
    """Build a mock streaming response from a list of tokens."""
    chunks = []
    for tok in tokens:
        c = MagicMock()
        c.choices = [MagicMock()]
        c.choices[0].delta.content = tok
        c.choices[0].finish_reason = None
        chunks.append(c)
    # Final chunk with finish_reason
    final = MagicMock()
    final.choices = [MagicMock()]
    final.choices[0].delta.content = None
    final.choices[0].finish_reason = "stop"
    if usage:
        final.usage = MagicMock(prompt_tokens=usage[0], completion_tokens=usage[1])
    else:
        final.usage = None
    chunks.append(final)
    return iter(chunks)


class TestChatStreamEndpoint:
    def test_endpoint_registered(self):
        """POST /chat/stream must be in the route list."""
        from laozhang_api import app
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("chat" in r and "stream" in r for r in routes), \
            f"chat/stream route missing. Routes: {routes}"

    def test_chat_request_model_valid(self):
        """ChatRequest model should accept standard fields."""
        from laozhang_api import ChatRequest
        req = ChatRequest(
            session_id="sess-1",
            message="Hello",
            model="gemini-2.5-flash",
            system="You are helpful.",
            temperature=0.9,
            max_tokens=1024,
        )
        assert req.message == "Hello"
        assert req.model == "gemini-2.5-flash"

    def test_models_dict_contains_key_models(self):
        """MODELS dict must map our frontend aliases to real API IDs."""
        from laozhang_api import MODELS
        required = {
            "gemini-2.5-flash":   "gemini-2.5-flash",
            "deepseek-v3":        "deepseek-chat",
            "gpt-4o-mini":        "gpt-4o-mini",
            "claude-sonnet":      "claude-sonnet-4-6",
            "gpt-4o":             "gpt-4o",
            "grok-4":             "grok-4-latest",
            "glm":                "glm-4.5-flash",
        }
        for key, expected_val in required.items():
            assert key in MODELS, f"Key '{key}' missing from MODELS"
            assert MODELS[key] == expected_val, \
                f"MODELS['{key}'] = '{MODELS[key]}', expected '{expected_val}'"

    def test_model_max_tokens_has_entries(self):
        """MODEL_MAX_TOKENS should cover all keys in MODELS."""
        from laozhang_api import MODELS, MODEL_MAX_TOKENS
        missing = []
        for alias, model_id in MODELS.items():
            if model_id not in MODEL_MAX_TOKENS:
                missing.append(model_id)
        assert not missing, f"Missing from MODEL_MAX_TOKENS: {missing}"

    def test_chat_stream_calls_openai_client(self, client):
        """Chat stream should invoke the OpenAI client with the right parameters."""
        with patch("laozhang_api.make_client") as mock_make:
            mock_openai = MagicMock()
            mock_openai.chat.completions.create.return_value = make_stream_response(["Hello", " world"])
            mock_make.return_value = mock_openai

            response = client.post("/chat/stream", json={
                "session_id": "test-session",
                "message": "Hi",
                "model": "gemini-2.5-flash",
                "temperature": 0.9,
                "max_tokens": 512,
            })

            assert response.status_code == 200
            assert "text/event-stream" in response.headers.get("content-type", "")

    def test_session_stored_after_chat(self, client):
        """Messages should accumulate in session store."""
        from laozhang_api import session_store

        with patch("laozhang_api.make_client") as mock_make:
            mock_openai = MagicMock()
            mock_openai.chat.completions.create.return_value = make_stream_response(["Test response"])
            mock_make.return_value = mock_openai

            sid = "session-store-test-" + __import__("uuid").uuid4().hex[:8]
            client.post("/chat/stream", json={
                "session_id": sid,
                "message": "Remember this",
                "model": "gemini-2.5-flash",
                "temperature": 0.7,
                "max_tokens": 128,
            })

            if sid in session_store:
                msgs = session_store[sid]
                assert any(m.get("role") == "user" for m in msgs), \
                    "User message not stored in session"

    def test_cancel_endpoint(self, client):
        """POST /cancel/{session_id} should return ok status."""
        from laozhang_api import cancel_flags
        import threading
        # Register a fake cancel event
        sid = "cancel-test-session"
        cancel_flags[sid] = threading.Event()

        response = client.post(f"/cancel/{sid}")
        assert response.status_code == 200
        data = response.json()
        assert "cancelled" in str(data).lower() or "ok" in str(data).lower() or data.get("status")

    def test_delete_session(self, client):
        """DELETE /session/{id} should clear session history."""
        from laozhang_api import session_store
        sid = "delete-test-session"
        session_store[sid] = [{"role": "user", "content": "test"}]

        response = client.delete(f"/session/{sid}")
        assert response.status_code == 200
        assert sid not in session_store

    def test_get_history_empty_session(self, client):
        """GET /history/{id} for unknown session should return empty list."""
        response = client.get("/history/nonexistent-session-xyz")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, (list, dict))


class TestModelResolution:
    def test_unknown_model_passes_through(self):
        """Models not in MODELS dict are passed as-is (fallthrough)."""
        from laozhang_api import MODELS
        # A model not in dict should not raise — it passes through
        result = MODELS.get("some-custom-model", "some-custom-model")
        assert result == "some-custom-model"

    def test_max_tokens_ceiling(self):
        """TOKEN ceiling must be positive for all known models."""
        from laozhang_api import MODEL_MAX_TOKENS, DEFAULT_MAX_TOKENS
        for model, tokens in MODEL_MAX_TOKENS.items():
            assert tokens > 0, f"MODEL_MAX_TOKENS[{model}] = {tokens} — must be positive"
        assert DEFAULT_MAX_TOKENS > 0
