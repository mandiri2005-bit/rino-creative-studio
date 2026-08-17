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
        # config errors are reported once per process; each row is its own process, logically
        na._f6_reset_config_errors_for_tests()
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
# ---------------------------------------------------------------------------
# the gate arms ONLY on the literal "1" — truth table
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("enabled,mode,expected", [
    # ── the brake always wins, whatever the mode says ──────────────────────
    ("0",    None,        "off"),
    ("0",    "observe",   "off"),
    ("0",    "enforce",   "off"),
    ("0",    "OBSERVE",   "off"),     # still off — but reported, see the row below
    # ── ENABLED arms on the literal "1" ALONE: no trimming, no case folding ─
    (None,   None,        "off"),     # absent — a typo in the NAME lands here
    ("",     "observe",   "off"),
    (" ",    "observe",   "off"),
    (" 1 ",  "observe",   "off"),     # whitespace is not the literal
    ("1 ",   "observe",   "off"),
    (" 1",   "observe",   "off"),
    (" 0 ",  None,        "off"),
    ("true", "observe",   "off"),
    ("TRUE", "observe",   "off"),
    ("yes",  None,        "off"),
    ("ON",   "observe",   "off"),
    ("2",    "observe",   "off"),
    ("01",   "observe",   "off"),
    # ── mode: same rule, and the dangerous half is "ENFORCE", not "observe " ─
    ("1",    None,        "enforce"),  # absent → legacy compatibility
    ("1",    "",          "off"),      # deliberately empty is a VALUE, and invalid
    ("1",    " ",         "off"),
    ("1",    "  ",        "off"),
    ("1",    "OBSERVE",   "off"),
    ("1",    "Observe",   "off"),
    ("1",    "observe ",  "off"),
    ("1",    " observe",  "off"),
    ("1",    "ENFORCE",   "off"),      # the one that would otherwise arm blocking
    ("1",    " enforce",  "off"),
    ("1",    "Enforce",   "off"),
    ("1",    "OFF",       "off"),
    ("1",    "obsereve",  "off"),
    # ── the only three that resolve to themselves ──────────────────────────
    ("1",    "observe",   "observe"),
    ("1",    "enforce",   "enforce"),
    ("1",    "off",       "off"),
])
def test_the_gate_truth_table(announce, enabled, mode, expected):
    assert announce(enabled=enabled, mode=mode)["resolved_mode"] == expected


def test_an_absent_gate_value_is_a_config_error_not_a_quiet_off(announce, caplog):
    """🔴 THE PRICE OF INVERTING THE DEFAULT, PAID LOUDLY. A variable that goes missing now
    DISABLES the gate instead of arming it — which is the original F6 finding ("enforcement flag
    default OFF") reachable by accident. It is acceptable only because it is not silent."""
    import logging
    caplog.set_level(logging.INFO)
    rec = announce(enabled=None, mode="observe")
    assert rec["resolved_mode"] == "off"
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "an absent gate variable disabled F6 silently"
    assert any("gating NOTHING" in r.getMessage() or "only the literal '1'" in r.getMessage().lower()
               for r in errors)


def test_the_deliberate_brake_is_not_an_error(announce, caplog):
    """`0` is an operator holding the brake on purpose — expected, and quiet."""
    import logging
    caplog.set_level(logging.INFO)
    for mode in (None, "observe", "enforce", "off"):
        caplog.clear()
        announce(enabled="0", mode=mode)
        assert not [r for r in caplog.records if r.levelname == "ERROR"], mode


@pytest.mark.parametrize("bad_mode", ["", " ", "OBSERVE", "observe ", "ENFORCE", "obsereve"])
def test_a_mode_typo_is_reported_while_the_brake_is_still_on(announce, caplog, bad_mode):
    """🔴 THE RUNBOOK SETS THE MODE WHILE THE BRAKE IS ON (steps 2-3, gate opens at step 4). A
    malformed mode that stayed silent until the gate opened would surface at the single worst
    moment. The brake still wins — resolved is `off` either way — but the typo speaks now."""
    import logging
    caplog.set_level(logging.INFO)
    rec = announce(enabled="0", mode=bad_mode)
    assert rec["resolved_mode"] == "off", "the brake must win regardless"
    assert [r for r in caplog.records if r.levelname == "ERROR"], \
        f"a staged mode typo ({bad_mode!r}) was hidden behind the brake"


@pytest.mark.parametrize("bad", ["true", "yes", "ON", "2"])
def test_an_unvalidated_gate_value_is_a_config_error(announce, caplog, bad):
    import logging
    caplog.set_level(logging.INFO)
    assert announce(enabled=bad, mode="observe")["resolved_mode"] == "off"
    assert [r for r in caplog.records if r.levelname == "ERROR"]


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


def test_an_unset_gate_is_off_and_an_error_not_enforce(announce, caplog):
    """The inverse of the old rule: setting nothing at all no longer lands in `enforce`."""
    import logging
    caplog.set_level(logging.INFO)
    rec = announce(enabled=None, mode=None)
    assert rec["resolved_mode"] == "off"
    assert [r for r in caplog.records if r.levelname == "ERROR"]


def test_an_invalid_mode_is_an_error_and_resolves_off(announce, caplog):
    import logging
    caplog.set_level(logging.INFO)
    rec = announce(enabled="1", mode="obsereve")
    assert rec["resolved_mode"] == "off", "a typo must never start blocking deliveries"
    errors = [r for r in caplog.records if r.levelname == "ERROR" and "F6 config" in r.getMessage()]
    assert errors


@pytest.mark.parametrize("enabled,mode", [("0", "observe"), ("1", "observe"), ("1", "enforce")])  # noqa: E501
def test_a_well_formed_configuration_is_merely_informational(announce, caplog, enabled, mode):
    import logging
    caplog.set_level(logging.INFO)
    announce(enabled=enabled, mode=mode)
    assert not [r for r in caplog.records
                if r.levelname in ("WARNING", "ERROR") and "F6 config" in r.getMessage()]


def test_the_announcement_never_raises(announce, monkeypatch):
    """Broken at the seam the announcement ACTUALLY uses. It reads `_f6_resolve` now, not
    `_f6_mode`; a test still patching the latter would patch a function off the path and prove
    nothing."""
    na = _live("narration_api")
    monkeypatch.setattr(na, "_f6_resolve", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert na.log_f6_config("python") == {}, "a broken announcement must not stop a boot"


def test_the_announcement_and_the_job_path_read_the_same_resolver(monkeypatch):
    """🔴 ONE RESOLVER, TWO CONSUMERS — asserted, not assumed. While these parsed the
    environment separately, a mutant that disabled the resolver's own config error stayed alive
    because the announcement's private copy still reported one."""
    na = _live("narration_api")
    calls = []
    real = na._f6_resolve
    monkeypatch.setattr(na, "_f6_resolve",
                        lambda: (calls.append(1), real())[1])
    monkeypatch.setenv("NARASI_F6_ENABLED", "1")
    monkeypatch.setenv("NARASI_F6_MODE", "observe")
    na._f6_reset_config_errors_for_tests()
    assert na._f6_mode() == "observe"
    assert na.log_f6_config("python")["resolved_mode"] == "observe"
    assert len(calls) == 2, "one of the two consumers is not using the shared resolver"


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
