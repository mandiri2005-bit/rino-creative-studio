"""The conftest outbound-network guard must actually block, and must not over-block.

A guard nobody tests is a guard that quietly stops working. These cover all four
properties that matter: remote is refused, local still works, the marker exempts, and the
specific code path that leaked in production-adjacent testing is closed.
"""

import pathlib
import shutil
import socket
import subprocess
import sys

import pytest

import conftest
import laozhang_api
from conftest import BLOCKED_ATTEMPTS, OutboundNetworkBlocked


@pytest.mark.outbound_guard_probe
def test_remote_hostname_is_blocked():
    with pytest.raises(OutboundNetworkBlocked) as excinfo:
        socket.getaddrinfo("api.laozhang.ai", 443)

    message = str(excinfo.value)
    assert "api.laozhang.ai" in message
    assert "allow_outbound" in message, "the error must say how to opt in"


@pytest.mark.outbound_guard_probe
def test_remote_ip_is_blocked_even_without_dns():
    """connect() is guarded too, so a hardcoded IP cannot slip past getaddrinfo."""
    sock = socket.socket()
    try:
        with pytest.raises(OutboundNetworkBlocked):
            sock.connect(("1.1.1.1", 443))
    finally:
        sock.close()


def test_localhost_is_not_blocked():
    """Over-blocking would break disposable Postgres / Redis / fixture servers.

    Port 1 is closed, so a refusal proves the guard let the attempt through rather than
    intercepting it — and nothing leaves the machine either way.
    """
    sock = socket.socket()
    sock.settimeout(1)
    try:
        with pytest.raises((ConnectionRefusedError, OSError)) as excinfo:
            sock.connect(("127.0.0.1", 1))
        assert not isinstance(excinfo.value, OutboundNetworkBlocked)
    finally:
        sock.close()


def test_loopback_resolution_is_not_blocked():
    assert socket.getaddrinfo("localhost", 80)


@pytest.mark.allow_outbound
def test_the_marker_exempts_a_test():
    """Proven without dialling anything: the guard simply is not installed here."""
    assert socket.getaddrinfo.__name__ != "guarded_getaddrinfo"
    assert socket.socket.connect.__name__ != "guarded_connect"


def test_the_guard_is_installed_by_default():
    """The counterpart to the marker test — otherwise that one proves nothing."""
    assert socket.getaddrinfo.__name__ == "guarded_getaddrinfo"
    assert socket.socket.connect.__name__ == "guarded_connect"


def test_a_swallowed_dial_out_fails_the_suite(tmp_path):
    """ENFORCEMENT, not detection — run in a subprocess because that is the only place an
    exit code is observable.

    🔴 This is the property the guard originally lacked. Blocking raises inside the socket
       call, but application code swallows it (the OpenAI SDK re-wraps everything into
       APIConnectionError, and _create catches per rung), and a terminal-summary hook cannot
       change the exit code. So the first version of this guard let a test dial out, get
       blocked, and still report green — the guard's own LaoZhang probe was the proof, since
       the suite stayed 8/8 while a real attempt had been intercepted.

       The inner test below swallows the block exactly the way the SDK does. If enforcement
       works, pytest exits non-zero anyway.
    """
    shutil.copy(pathlib.Path(__file__).parent / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_swallower.py").write_text(
        "import socket\n"
        "\n"
        "def test_swallows_the_block():\n"
        "    try:\n"
        "        socket.getaddrinfo('api.laozhang.ai', 443)\n"
        "    except Exception:\n"
        "        pass          # what the OpenAI SDK does to a transport error\n"
        "    assert True       # the test body itself is perfectly happy\n"
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "test_swallower.py"],
        cwd=tmp_path, capture_output=True, text=True)

    assert result.returncode != 0, (
        "a test that dialled out and swallowed the block still passed — the guard detects "
        f"but does not enforce.\nSTDOUT:\n{result.stdout}")
    assert "attempted 1 outbound network call" in result.stdout, (
        f"failed for some other reason than the guard:\n{result.stdout}")
    assert "api.laozhang.ai" in result.stdout


def test_a_clean_test_is_not_failed_by_the_guard(tmp_path):
    """The other half: no attempt, no failure. Otherwise the check above proves nothing."""
    shutil.copy(pathlib.Path(__file__).parent / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_clean.py").write_text(
        "def test_touches_no_network():\n"
        "    assert 2 + 2 == 4\n"
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_clean.py"],
        cwd=tmp_path, capture_output=True, text=True)

    assert result.returncode == 0, (
        f"the guard failed a test that never touched the network:\n{result.stdout}")


def test_probe_marker_exempts_from_the_teardown_failure(tmp_path):
    """A probe may trigger the guard on purpose — keyed on the MARKER, not a filename.

    A filename-based exemption hands a silent free pass to every test in that file and
    follows the file when it is renamed or copied, which is how a real offender hides.
    """
    shutil.copy(pathlib.Path(__file__).parent / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_probe.py").write_text(
        "import socket\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.outbound_guard_probe\n"
        "def test_deliberate_probe():\n"
        "    with pytest.raises(Exception):\n"
        "        socket.getaddrinfo('api.laozhang.ai', 443)\n"
        "\n"
        "def test_same_file_without_the_marker_is_still_caught():\n"
        "    try:\n"
        "        socket.getaddrinfo('api.laozhang.ai', 443)\n"
        "    except Exception:\n"
        "        pass\n"
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_probe.py"],
        cwd=tmp_path, capture_output=True, text=True)

    # The marked probe is exempt; its unmarked neighbour in the SAME FILE is not.
    assert result.returncode != 0, (
        f"the unmarked test in the probe file escaped the guard:\n{result.stdout}")
    assert "1 error" in result.stdout, (
        f"expected exactly one test to be caught:\n{result.stdout}")
    # Enforcement lands in teardown, so the caught test's BODY still passes and the counts
    # read "2 passed ... 1 error". What matters is WHICH test carries the error.
    errors = [line for line in result.stdout.splitlines() if line.startswith("ERROR ")]
    assert any("test_same_file_without_the_marker_is_still_caught" in line
               for line in errors), f"the offender was not flagged:\n{result.stdout}"
    assert not any("test_deliberate_probe" in line for line in errors), (
        f"the marked probe was flagged despite its marker:\n{result.stdout}")


def test_operator_host_allowlist_is_honoured(monkeypatch):
    """A DB-integration run against a throwaway remote host must not need the guard off.

    Patched on the parsed set rather than the env var, because the set is built at import.
    """
    monkeypatch.setattr(
        conftest, "_EXTRA_ALLOWED_HOSTS", {"scratch-db.example.invalid"})

    assert conftest._is_local_host("scratch-db.example.invalid") is True
    assert conftest._is_local_host("api.laozhang.ai") is False


@pytest.mark.outbound_guard_probe
def test_the_laozhang_rung_cannot_reach_the_real_provider():
    """The exact leak this guard was written for.

    conftest sets LAOZHANG_API_KEY to a placeholder. That value is truthy, so _create's
    `if not key: continue` treats the laozhang rung as usable, constructs a real
    OpenAI(base_url="https://api.laozhang.ai/v1") and calls it. On 2026-08-11 that sent six
    live requests to LaoZhang from a unit test.

    The guard's exception does NOT survive to the caller, and that is exactly why the
    BLOCKED_ATTEMPTS record exists. The OpenAI SDK re-wraps whatever the transport raises
    into APIConnectionError('Connection error.'), discarding the message, and
    _create then catches per rung and advances. So asserting on the error TEXT does not
    work, and asserting only pytest.raises(RuntimeError) would pass even if the request had
    really gone out and come back 401. The record is the only sound evidence.
    """
    before = len(BLOCKED_ATTEMPTS)

    client = laozhang_api._NarasiFailoverClient("claude-opus-4-6", role="", phase="")

    with pytest.raises(RuntimeError):
        client.chat.completions.create(
            model="claude-opus-4-6",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=16, stream=False)

    new_attempts = BLOCKED_ATTEMPTS[before:]
    assert new_attempts, (
        "the laozhang rung was NOT intercepted — the request may have really gone out")
    assert any(record["host"] == "api.laozhang.ai" for record in new_attempts), (
        f"blocked something, but not LaoZhang: {new_attempts}")
