"""The F6 configuration announcement — emitted at BOTH entry points, before work is accepted.

🔴 WHY THIS EXISTS. The activation runbook's step after opening the gate is "confirm the service
   actually resolved `observe`". Before this line the only evidence was the very environment
   variables that step exists to corroborate independently — circular. This is the independent
   witness: what the PROCESS resolved.

🔴 AND ORDER IS THE WHOLE POINT. A configuration line printed after a service began taking work
   cannot be trusted to describe the configuration the first job ran under. So these tests do not
   assert that the call exists in the source; they drive the real entry points and assert the
   announcement happened BEFORE the moment each one becomes able to accept work — `Worker(...)`
   for narration-worker, the lifespan `yield` for the python service.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import types

import pytest


def _live(name):
    return sys.modules.get(name) or importlib.import_module(name)


# ---------------------------------------------------------------------------
# what the record says
# ---------------------------------------------------------------------------
_FIELDS = {"service", "raw_enabled", "raw_mode", "resolved_mode",
           "effective_sample_rate", "deployment_revision"}


@pytest.fixture
def announce(monkeypatch):
    na = _live("narration_api")

    def run(*, enabled=None, mode=None, rate=None, revision="deadbeefcafe", service="python"):
        for key, value in (("NARASI_F6_ENABLED", enabled), ("NARASI_F6_MODE", mode),
                           ("NARASI_F6_OBSERVE_SAMPLE_RATE", rate),
                           ("RAILWAY_GIT_COMMIT_SHA", revision)):
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        for key in ("RAILWAY_DEPLOYMENT_ID", "GIT_COMMIT_SHA", "SOURCE_COMMIT"):
            monkeypatch.delenv(key, raising=False)
        return na.log_f6_config(service)

    return run


def test_the_record_carries_exactly_the_six_declared_fields(announce):
    rec = announce(enabled="0", mode="observe", rate="0.10")
    assert set(rec) == _FIELDS, f"unexpected fields: {sorted(set(rec) ^ _FIELDS)}"
    assert rec["service"] == "python"
    assert rec["raw_enabled"] == "0"
    assert rec["raw_mode"] == "observe"
    assert rec["resolved_mode"] == "off", "the brake outranks the mode"
    assert rec["effective_sample_rate"] == 0.10
    assert rec["deployment_revision"] == "deadbeefcafe"


def test_it_reports_the_raw_values_alongside_the_resolved_one(announce):
    """🔴 RAW AND RESOLVED BOTH, BECAUSE THE GAP BETWEEN THEM IS THE BUG. `ENABLED=1` with
    `MODE` absent resolves to `enforce`; a record that showed only the resolved value would
    leave an operator unable to see WHY."""
    rec = announce(enabled="1", mode=None)
    assert (rec["raw_enabled"], rec["raw_mode"]) == ("1", None)
    assert rec["resolved_mode"] == "enforce"


def test_no_other_environment_variable_is_disclosed(announce, monkeypatch):
    monkeypatch.setenv("LAOZHANG_API_KEY", "sk-secret-value")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@host/db")
    rec = announce(enabled="0", mode="observe")
    blob = repr(rec)
    assert "sk-secret-value" not in blob and "postgres://" not in blob
    assert set(rec) == _FIELDS


def test_an_absent_revision_is_reported_as_none_not_invented(announce):
    rec = announce(enabled="0", mode="observe", revision=None)
    assert rec["deployment_revision"] is None


# ---------------------------------------------------------------------------
# the level carries the warning
# ---------------------------------------------------------------------------
def test_gate_on_with_no_mode_is_a_warning_because_it_means_enforce(announce, caplog):
    """🔴 THE TRAP THIS LINE EXISTS TO CATCH. A deployment meaning to observe, whose mode
    variable never landed, refuses customer deliveries and looks configured."""
    import logging
    caplog.set_level(logging.INFO)
    rec = announce(enabled="1", mode=None)
    assert rec["resolved_mode"] == "enforce"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"
                and "F6 config" in r.getMessage()]
    assert warnings, "the enforce fallback was not announced at warning level"
    assert "ENFORCE" in warnings[0].getMessage()


def test_an_unset_gate_with_no_mode_is_also_a_warning(announce, caplog):
    """Absent `NARASI_F6_ENABLED` means ON — so "I set nothing at all" is the same trap."""
    import logging
    caplog.set_level(logging.INFO)
    rec = announce(enabled=None, mode=None)
    assert rec["resolved_mode"] == "enforce"
    assert [r for r in caplog.records if r.levelname == "WARNING" and "F6 config" in r.getMessage()]


def test_an_invalid_mode_is_an_error_and_resolves_off(announce, caplog):
    import logging
    caplog.set_level(logging.INFO)
    rec = announce(enabled="1", mode="obsereve")
    assert rec["resolved_mode"] == "off", "a typo must never start blocking deliveries"
    errors = [r for r in caplog.records if r.levelname == "ERROR" and "F6 config" in r.getMessage()]
    assert errors


@pytest.mark.parametrize("enabled,mode", [("0", "observe"), ("1", "observe"), ("1", "enforce")])
def test_a_well_formed_configuration_is_merely_informational(announce, caplog, enabled, mode):
    import logging
    caplog.set_level(logging.INFO)
    announce(enabled=enabled, mode=mode)
    assert not [r for r in caplog.records
                if r.levelname in ("WARNING", "ERROR") and "F6 config" in r.getMessage()]


def test_the_announcement_never_raises(announce, monkeypatch):
    na = _live("narration_api")
    monkeypatch.setattr(na, "_f6_mode", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert na.log_f6_config("python") == {}, "a broken announcement must not stop a boot"


# ---------------------------------------------------------------------------
# ORDER — announced before either entry point can accept work
# ---------------------------------------------------------------------------
class _Stop(Exception):
    """Ends the drive once the entry point reaches the moment work becomes acceptable."""


def test_the_worker_announces_before_it_can_accept_a_job(monkeypatch):
    """🔴 `Worker(...)` IS THE MOMENT. Construction hands this process to BullMQ; anything
    printed afterwards describes a configuration the first job may already have run under."""
    na, nw = _live("narration_api"), _live("narration_worker")
    order = []

    real = na.log_f6_config
    monkeypatch.setattr(na, "log_f6_config",
                        lambda service: (order.append(("config", service)), real(service))[1])

    def _worker(*_a, **_k):
        order.append(("accepting_jobs", None))
        raise _Stop()

    monkeypatch.setitem(sys.modules, "bullmq", types.SimpleNamespace(Worker=_worker))

    async def _anoop(*_a, **_k):
        return None

    monkeypatch.setattr(_live("database"), "init_db", _anoop)
    monkeypatch.setattr(_live("redis_client"), "init_redis", _anoop)
    monkeypatch.setattr(_live("laozhang_api"), "_dalang_crashsafe_enabled", lambda: False)
    meter = _live("canon_lite_qc_meter")
    meter.reset_host_role_for_tests()
    monkeypatch.setenv("NARASI_EXECUTOR_THREADS", "0")

    with pytest.raises(_Stop):
        asyncio.run(nw.main())

    assert ("config", "narration-worker") in order, "the worker never announced its config"
    assert order.index(("config", "narration-worker")) < order.index(("accepting_jobs", None)), \
        "the worker became able to accept jobs before announcing its configuration"


def test_the_python_service_announces_before_it_serves(monkeypatch):
    """🔴 THE `yield` IN lifespan IS THE MOMENT. Everything before it runs while the app is
    still not serving; everything after it runs with requests already arriving."""
    na, lz = _live("narration_api"), _live("laozhang_api")
    order = []

    real = na.log_f6_config
    monkeypatch.setattr(na, "log_f6_config",
                        lambda service: (order.append(("config", service)), real(service))[1])

    async def _anoop(*_a, **_k):
        return None

    monkeypatch.setattr(lz, "_require_prod_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(_live("database"), "init_db", _anoop)
    monkeypatch.setattr(_live("redis_client"), "init_redis", _anoop)
    monkeypatch.setattr(lz, "_image_jobs_sweep_loop", _anoop)
    monkeypatch.setenv("NARASI_EXECUTOR_THREADS", "0")

    async def drive():
        # `lifespan` is an @asynccontextmanager: `__aenter__` runs everything up to the yield,
        # which is exactly the window in which the app is not yet serving.
        cm = lz.lifespan(None)
        await cm.__aenter__()
        order.append(("serving", None))
        try:
            await cm.__aexit__(None, None, None)
        except Exception:               # noqa: BLE001 - shutdown noise is not what is under test
            pass

    asyncio.run(drive())

    assert ("config", "python") in order, "the python service never announced its config"
    assert order.index(("config", "python")) < order.index(("serving", None)), \
        "the app began serving before announcing its configuration"
