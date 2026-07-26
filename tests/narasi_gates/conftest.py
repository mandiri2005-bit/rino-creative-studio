"""
pytest fixtures shared across tests/narasi_gates/. Mirrors tests/python/conftest.py's
sys.path and credential-stub setup (a sibling conftest.py is NOT inherited by pytest
across directories, so this file exists independently rather than modifying the
existing one).
"""
import os
import sys

# Make the python package importable, same relative depth as tests/python/conftest.py.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../python"))

# Minimal stubs so laozhang_api.py imports without real credentials (no network/provider
# calls are ever made in this directory's tests -- every model-call boundary is
# monkeypatched with a canned, offline response).
os.environ.setdefault("LAOZHANG_API_KEY", "sk-test-key-for-unit-tests")
os.environ.setdefault("LAOZHANG_IMAGE_API_KEY", "sk-test-key-for-unit-tests")
