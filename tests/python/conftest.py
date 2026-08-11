"""
pytest fixtures shared across all Python test modules.
"""
import socket
import sys
import os
import io
import pytest

# Make the python package importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../python"))

# ── Minimal stubs so laozhang_api.py imports without real creds ──────────────
# ⚠️ THESE DUMMY KEYS ARE WHY THE NETWORK GUARD BELOW EXISTS. A placeholder key is
#    truthy, and the code's rung-skip guard is `if not key: continue` — so a rung with a
#    fake key reads as USABLE and gets attempted for real. Do not remove the guard on the
#    grounds that "the keys are fake"; fake keys are precisely the hazard.
os.environ.setdefault("LAOZHANG_API_KEY", "sk-test-key-for-unit-tests")
os.environ.setdefault("LAOZHANG_IMAGE_API_KEY", "sk-test-key-for-unit-tests")


# ── Outbound-network guard ────────────────────────────────────────────────────
#
# 🔴 WHY THIS IS NOT OPTIONAL. Observed live 2026-08-11: a unit test let the real
#    _NarasiFailoverClient._create walk a chain containing the laozhang rung, and the suite
#    issued SIX genuine HTTPS requests to api.laozhang.ai — each answered 401 with a real
#    upstream request id. Nothing was billed only because the placeholder token above is
#    rejected. A valid key in the environment (a developer's shell, a CI secret) turns the
#    same code path into real spend, and a test suite is the last place that should be
#    deciding whether an LLM call gets paid for.
#
#    Blocking at the SOCKET is deliberate. Patching client constructors would mean chasing
#    every library the tree uses — this repo reaches the network through requests (~70 call
#    sites), OpenAI (19), httpx.AsyncClient (15), urllib.request (6), google genai (6) and
#    boto3 — and would still miss the next one added. One socket-level block covers all of
#    them, including transports that do not exist yet.
#
#    getaddrinfo is guarded as well as connect() so the error can name the HOST. By the time
#    connect() runs, a hostname has already become an IP address, and "connection to
#    104.21.x.x blocked" tells nobody which provider was called.
#
#    Local sockets stay open: TestClient/ASGI needs none, but a disposable Postgres, Redis,
#    or a fixture's throwaway server on 127.0.0.1 is legitimate and unaffected.
#
#    A test that genuinely must reach the network opts in explicitly:
#        @pytest.mark.allow_outbound
#    Use it sparingly, and say in the test why a live call is required.
#
#    For DB-integration runs against a DISPOSABLE database that is not on loopback, name its
#    host instead of disabling the guard:
#        PYTEST_ALLOW_OUTBOUND_HOSTS=my-scratch-db.example.com pytest ...
#    (The documented T72_DSN already points at 127.0.0.1, so that path needs nothing.)
#
#    KNOWN LIMIT, stated rather than hidden: this is a function-scoped fixture, so it covers
#    test bodies, fixtures and setup — but NOT module-level code that runs during collection.
#    A test module that dials out at import time would slip past. None do today.

_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "", "0", None}

# Extra hosts an operator has explicitly vouched for, e.g. a throwaway remote Postgres.
_EXTRA_ALLOWED_HOSTS = {
    h.strip().lower()
    for h in (os.environ.get("PYTEST_ALLOW_OUTBOUND_HOSTS") or "").split(",")
    if h.strip()
}

# Every blocked attempt, as {"test": nodeid, "host": ..., "port": ...}.
#
# 🔴 RAISING IS NOT ENOUGH TO FIND THE OFFENDERS, which is the whole reason this list
#    exists. Callers here swallow broad exceptions: the OpenAI SDK re-wraps anything a
#    transport raises into APIConnectionError('Connection error.') — losing the original
#    message entirely — and _NarasiFailoverClient._create then catches per-rung and advances.
#    So a test that dials out can BLOCK CLEANLY AND STILL PASS, looking for all the world
#    like it never touched the network. The record is what makes those visible; see the
#    terminal summary hook below.
BLOCKED_ATTEMPTS: list = []


class OutboundNetworkBlocked(AssertionError):
    """A test tried to reach a host outside this machine."""


def _is_local_host(host) -> bool:
    if host in _LOCAL_HOSTNAMES:
        return True
    text = str(host).strip().lower()
    if text in _LOCAL_HOSTNAMES or text in _EXTRA_ALLOWED_HOSTS:
        return True
    # 127.0.0.0/8 and *.localhost are loopback by definition.
    return text.startswith("127.") or text.endswith(".localhost")


def _blocked_message(host, port) -> str:
    return (
        f"outbound network call to {host!r}:{port} was blocked by the test suite.\n"
        f"A unit test must not reach a real provider — the placeholder API keys in "
        f"conftest.py read as valid to `if not key: continue`, so a rung with a fake key is "
        f"attempted for real, and a valid key in the environment would BILL.\n"
        f"Fix by stubbing the rung or the client factory for this test (see "
        f"tests/python/test_narasi_outline_production_config.py for the pattern).\n"
        f"If a live call is genuinely required, mark the test @pytest.mark.allow_outbound "
        f"and document why."
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_outbound: test genuinely needs a real outbound network call "
        "(exempt from the conftest network guard)")
    config.addinivalue_line(
        "markers",
        "outbound_guard_probe: test deliberately triggers the network guard in order to "
        "verify it; exempt from the teardown failure, still blocked from the network")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Cross-test overview of blocked attempts.

    ⚠️ THIS IS A REPORT, NOT THE ENFORCEMENT. A terminal-summary hook cannot change the exit
       code, so for a while this was the ONLY thing surfacing a swallowed dial-out — meaning
       a test could attempt a real provider call, get blocked, swallow the exception and
       still report green. Enforcement lives in the fixture's teardown; this stays only
       because a single grouped list is easier to act on than N separate failures.
    """
    by_test: dict = {}
    for record in BLOCKED_ATTEMPTS:
        # Probes trigger the guard on purpose. Keyed off the MARKER, never off a filename:
        # a filename check silently hands a free pass to every test in that file, and
        # follows the file if it is renamed or copied.
        if record["probe"]:
            continue
        by_test.setdefault(record["test"], set()).add(
            f"{record['host']}:{record['port']}")
    if not by_test:
        return
    total = sum(1 for r in BLOCKED_ATTEMPTS if not r["probe"])
    terminalreporter.write_sep("=", "outbound network calls blocked", yellow=True)
    terminalreporter.write_line(
        f"{total} attempt(s) from {len(by_test)} test(s). These were stopped, not billed — "
        f"but they should be stubbed, not left to the guard:")
    for nodeid in sorted(by_test):
        terminalreporter.write_line(f"  {nodeid}")
        for target in sorted(by_test[nodeid]):
            terminalreporter.write_line(f"      -> {target}")


@pytest.fixture(autouse=True)
def _no_outbound_network(request, monkeypatch):
    """Block connections off this machine, and FAIL the test that attempted one.

    🔴 Blocking alone is not enforcement, and that distinction is the whole point of the
       teardown half of this fixture. Application code here swallows transport errors: the
       OpenAI SDK re-wraps anything into APIConnectionError('Connection error.') and
       _NarasiFailoverClient._create then catches per rung and advances to the next one. So
       raising inside the socket call is invisible to the test result — the suite goes green
       while a test is still reaching for a paid provider on every run. The teardown check
       is what turns "we stopped it" into "you have to fix it".
    """
    if request.node.get_closest_marker("allow_outbound"):
        yield
        return

    is_probe = request.node.get_closest_marker("outbound_guard_probe") is not None
    nodeid = request.node.nodeid
    first_record = len(BLOCKED_ATTEMPTS)

    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _refuse(host, port):
        BLOCKED_ATTEMPTS.append(
            {"test": nodeid, "host": host, "port": port, "probe": is_probe})
        raise OutboundNetworkBlocked(_blocked_message(host, port))

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        if not _is_local_host(host):
            _refuse(host, port)
        return real_getaddrinfo(host, port, *args, **kwargs)

    def _check_address(address):
        # AF_UNIX addresses are plain strings/bytes — never remote.
        if not isinstance(address, tuple) or not address:
            return
        host = address[0]
        if not _is_local_host(host):
            _refuse(host, address[1] if len(address) > 1 else "?")

    def guarded_connect(self, address, *args, **kwargs):
        _check_address(address)
        return real_connect(self, address, *args, **kwargs)

    def guarded_connect_ex(self, address, *args, **kwargs):
        _check_address(address)
        return real_connect_ex(self, address, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

    yield

    attempts = BLOCKED_ATTEMPTS[first_record:]
    if attempts and not is_probe:
        targets = sorted({f"{r['host']}:{r['port']}" for r in attempts})
        pytest.fail(
            f"this test attempted {len(attempts)} outbound network call(s) to "
            f"{', '.join(targets)}. They were blocked, so nothing was billed — but the test "
            f"still reaches for a real provider on every run, and the application swallowed "
            f"the error rather than surfacing it, so it would otherwise have passed.\n"
            f"{_blocked_message(targets[0].rsplit(':', 1)[0], targets[0].rsplit(':', 1)[1])}",
            pytrace=False)


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
