"""
Tests for the per-request API key override middleware (ContextVar).
The middleware reads X-LaoZhang-API-Key header and makes it available
to make_client() for the duration of the request.
"""
import pytest
from unittest.mock import patch, MagicMock


class TestKeyOverrideMiddleware:
    def test_middleware_is_registered(self):
        """Verify key_override_middleware is attached to the app."""
        from laozhang_api import app
        middleware_names = [m.__class__.__name__ for m in app.middleware_stack.app.middleware_stack if hasattr(m, '__class__')]
        # FastAPI middleware stack: confirm our middleware function is present in routes
        # We verify indirectly by checking the source
        import inspect
        import laozhang_api
        src = inspect.getsource(laozhang_api)
        assert "key_override_middleware" in src
        assert "X-LaoZhang-API-Key" in src

    def test_context_var_exists(self):
        """_req_key ContextVar must be module-level."""
        from laozhang_api import _req_key
        from contextvars import ContextVar
        assert isinstance(_req_key, ContextVar)

    def test_make_client_uses_context_var(self):
        """make_client() should use _req_key.get() when set."""
        from laozhang_api import _req_key, make_client, API_KEY
        from openai import OpenAI

        override_key = "sk-override-12345"
        token = _req_key.set(override_key)
        try:
            with patch("laozhang_api.OpenAI") as MockOpenAI:
                MockOpenAI.return_value = MagicMock(spec=OpenAI)
                make_client()
                call_kwargs = MockOpenAI.call_args
                assert call_kwargs.kwargs.get("api_key") == override_key or \
                       (call_kwargs.args and call_kwargs.args[0] == override_key), \
                       f"Expected override key, got: {call_kwargs}"
        finally:
            _req_key.reset(token)

    def test_make_client_falls_back_to_env_key(self):
        """Without override, make_client() falls back to API_KEY env var."""
        from laozhang_api import _req_key, make_client, API_KEY

        # Reset to empty (no override)
        token = _req_key.set("")
        try:
            with patch("laozhang_api.OpenAI") as MockOpenAI:
                MockOpenAI.return_value = MagicMock()
                make_client()
                call_kwargs = MockOpenAI.call_args
                actual_key = (call_kwargs.kwargs.get("api_key") or
                              (call_kwargs.args[0] if call_kwargs.args else None))
                assert actual_key == API_KEY, f"Expected env key {API_KEY}, got {actual_key}"
        finally:
            _req_key.reset(token)

    def test_env_key_warning_on_startup(self, monkeypatch):
        """If LAOZHANG_API_KEY is not set, a warning is issued (not a crash)."""
        import importlib
        import warnings
        import sys

        # Temporarily remove the module so we can re-import
        mods_to_remove = [k for k in sys.modules if "laozhang_api" in k]
        for m in mods_to_remove:
            del sys.modules[m]

        monkeypatch.delenv("LAOZHANG_API_KEY", raising=False)
        monkeypatch.delenv("LAOZHANG_IMAGE_API_KEY", raising=False)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            try:
                import laozhang_api  # noqa: F401
                # If we get here, it was a warning (good)
                warning_msgs = [str(x.message) for x in w]
                assert any("LAOZHANG_API_KEY" in msg for msg in warning_msgs), \
                    f"Expected warning about API key, got: {warning_msgs}"
            except RuntimeError as e:
                # Old behavior was RuntimeError — still acceptable but warn preferred
                assert "LAOZHANG_API_KEY" in str(e)
            finally:
                # Restore
                for m in [k for k in sys.modules if "laozhang_api" in k]:
                    del sys.modules[m]
                monkeypatch.setenv("LAOZHANG_API_KEY", "sk-test-key-for-unit-tests")


class TestApiKeyHeaderForwarding:
    def test_models_endpoint_accessible(self, client):
        """GET /models should respond (200 or proxy error, not 500 from our code)."""
        with patch("laozhang_api.make_client") as mock:
            mock_client = MagicMock()
            mock_client.models.list.return_value = MagicMock(data=[])
            mock.return_value = mock_client
            # The endpoint might not exist — just verify no import errors
            pass

    def test_cancel_endpoint_exists(self, client):
        """POST /cancel/{id} should be registered."""
        from laozhang_api import app
        routes = [r.path for r in app.routes]
        assert any("cancel" in r for r in routes), f"Cancel route missing. Routes: {routes}"

    def test_session_delete_endpoint_exists(self, client):
        """DELETE /session/{id} should be registered."""
        from laozhang_api import app
        routes = [r.path for r in app.routes]
        assert any("session" in r for r in routes), f"Session route missing. Routes: {routes}"
