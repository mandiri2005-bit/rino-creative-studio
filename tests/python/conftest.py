"""
pytest fixtures shared across all Python test modules.
"""
import sys
import os
import io
import pytest

# Make the python package importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../python"))

# ── Minimal stubs so laozhang_api.py imports without real creds ──────────────
os.environ.setdefault("LAOZHANG_API_KEY", "sk-test-key-for-unit-tests")
os.environ.setdefault("LAOZHANG_IMAGE_API_KEY", "sk-test-key-for-unit-tests")

from fastapi.testclient import TestClient

@pytest.fixture(scope="session")
def app():
    """Import and return the FastAPI app instance."""
    from laozhang_api import app as fastapi_app
    return fastapi_app

@pytest.fixture(scope="session")
def client(app):
    """Synchronous TestClient for the FastAPI app."""
    return TestClient(app, raise_server_exceptions=True)

@pytest.fixture
def lz_headers():
    """Headers with a per-request API key override."""
    return {"X-LaoZhang-API-Key": "sk-override-test-key"}

@pytest.fixture
def minimal_txt():
    """Tiny UTF-8 text file bytes."""
    return b"Hello, world! This is a test file."

@pytest.fixture
def minimal_csv():
    return b"name,age,city\nAlice,30,Paris\nBob,25,London\n"

@pytest.fixture
def minimal_json():
    return b'{"key": "value", "number": 42}'
