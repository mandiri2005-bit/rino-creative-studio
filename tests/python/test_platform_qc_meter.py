"""Offline acceptance for the L2B physical-attempt meter.

Every provider is a local async fake. No test imports or constructs a real provider client.
"""
import asyncio
import ast
import logging
import random
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

import canon_lite_qc_meter as meter


TEST_BOUND = 8
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fresh_process_latch(monkeypatch):
    monkeypatch.setattr(meter, "_process_killed", False)
    monkeypatch.setattr(meter, "_cancelled_attempts", 0)


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.raise_get = False
        self.raise_counter = False

    async def get(self, key):
        self.calls.append(("get", key))
        if self.raise_get:
            raise RuntimeError("redis-secret")
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        self.calls.append(("set", key, value, dict(kwargs)))
        self.values[key] = str(value)
        return True

    async def incr(self, key):
        self.calls.append(("incr", key))
        if self.raise_counter:
            raise RuntimeError("redis-secret")
        self.values[key] = str(int(self.values.get(key, "0")) + 1)
        return int(self.values[key])

    async def decr(self, key):
        self.calls.append(("decr", key))
        if self.raise_counter:
            raise RuntimeError("redis-secret")
        self.values[key] = str(int(self.values.get(key, "0")) - 1)
        return int(self.values[key])


class FakeSink:
    def __init__(self):
        self.calls = []
        self.armed = False
        self.begin_outcome = "inserted"
        self.raise_on = set()
        self.resolve_outcome = "applied"

    async def begin(self, context, *, unit_index, attempt_ordinal):
        self.calls.append(("begin", context.run_id, unit_index, attempt_ordinal))
        if "begin" in self.raise_on:
            raise RuntimeError("db-secret-begin")
        return {
            "attempt_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "outcome": self.begin_outcome,
        }

    async def finish(self, attempt_id, state):
        self.calls.append(("finish", attempt_id, state))
        if "finish" in self.raise_on:
            raise RuntimeError("db-secret-finish")
        return {"lifecycle_applied": True, "current_state": state}

    async def resolve(self, attempt_id, usage):
        self.calls.append((
            "resolve", attempt_id, usage.tokens_in, usage.tokens_out,
            usage.provider_reported_cost_usd))
        if "resolve" in self.raise_on:
            raise RuntimeError("db-secret-resolve")
        return {"outcome": self.resolve_outcome, "cost_usd": Decimal("0.000225")}

    async def arm(self, reason_code):
        self.calls.append(("arm", reason_code))
        if "arm" in self.raise_on:
            raise RuntimeError("db-secret-arm")
        was = self.armed
        self.armed = True
        return {"armed": True, "was_already_armed": was}

    async def is_armed(self):
        self.calls.append(("is_armed",))
        if "is_armed" in self.raise_on:
            raise RuntimeError("db-secret-read")
        return self.armed


def _context(run_id="run-1"):
    return meter.AttemptContext(
        run_id=run_id,
        job_uuid=UUID("11111111-1111-4111-8111-111111111111"),
        job_external_id="job-ext",
        phase="canon_lite_l2_extract",
        provider="fake",
        model_upstream="fake-model",
        pricing_version="test-v1",
        rate_in_usd_per_m=Decimal("0.075"),
        rate_out_usd_per_m=Decimal("0.150"),
        attempt_timeout_s=Decimal("30"),
    )


def _request(index=0, attempt=1):
    return SimpleNamespace(
        chapter_index=index,
        attempt=attempt,
        chapter_bytes=b"TOP SECRET CHAPTER BYTES",
    )


def _usage(_raw):
    return meter.ProviderUsage(
        tokens_in=1000,
        tokens_out=500,
        provider_reported_cost_usd=None,
    )


def _provider_wrapper(provider, sink, redis, *, run_id="run-1", usage_reader=_usage):
    return meter.MeteredProvider(
        provider,
        sink=sink,
        context=_context(run_id),
        usage_reader=usage_reader,
        max_inflight=TEST_BOUND,
        redis_getter=lambda: redis,
    )


@pytest.mark.parametrize(
    ("raw", "unreadable", "expected"),
    [
        ("0", False, (True, "inflight_zero")),
        (b"0", False, (True, "inflight_zero")),
        (None, False, (False, "inflight_absent")),
        ("x", True, (False, "inflight_unreadable")),
        ("1", False, (False, "inflight_positive")),
        ("9", False, (False, "inflight_out_of_policy_range")),
        ("-1", False, (False, "inflight_negative")),
        ("-0", False, (False, "inflight_malformed")),
        ("٠", False, (False, "inflight_malformed")),
        ("१२", False, (False, "inflight_malformed")),
        ("²", False, (False, "inflight_malformed")),
        ("9" * 5000, False, (False, "inflight_malformed")),
        ("00", False, (False, "inflight_malformed")),
        ("+0", False, (False, "inflight_malformed")),
        (" 0 ", False, (False, "inflight_malformed")),
        ("1e3", False, (False, "inflight_malformed")),
    ],
)
def test_inflight_classifier_closed_ascii_domain(raw, unreadable, expected):
    got = meter.classify_inflight(
        raw, unreadable=unreadable, max_inflight=TEST_BOUND)
    assert (got.passes, got.reason_code) == expected


@pytest.mark.parametrize("policy", [None, "8", -1, 0, 1.5, True, [], b"8", 10 ** 16])
@pytest.mark.parametrize("raw", ["0", "1"])
def test_policy_is_validated_before_every_counter_branch(policy, raw):
    got = meter.classify_inflight(raw, max_inflight=policy)
    assert (got.passes, got.reason_code) == (False, "inflight_policy_invalid")


def test_inflight_classifier_signature_wide_fuzz_supports_totality():
    """Deterministic whole-signature sampling; structural partitioning carries the claim."""
    rng = random.Random(0x1_2B_37)
    policies = [None, "8", -1, 0, 1.5, True, [], b"8", 1, 8, 10 ** 15, 10 ** 16]
    fixed_raw = [
        None, "0", b"0", "", b"", "-0", "-1", "00", "+0", " 0 ", "1e3",
        "٠", "१२", "²", "9" * 5000, b"\xff", object(),
    ]

    for _ in range(40_000):
        selector = rng.randrange(5)
        if selector == 0:
            raw = rng.choice(fixed_raw)
        elif selector == 1:
            raw = "".join(chr(rng.randrange(0x3000)) for _ in range(rng.randrange(40)))
        elif selector == 2:
            raw = bytes(rng.randrange(256) for _ in range(rng.randrange(40)))
        elif selector == 3:
            raw = str(rng.randrange(10 ** 18))
        else:
            raw = "9" * rng.randrange(1025, 4097)
        policy = rng.choice(policies)
        unreadable = bool(rng.randrange(2))

        verdict = meter.classify_inflight(
            raw, unreadable=unreadable, max_inflight=policy)
        assert verdict.reason_code in meter.DECLARED_INFLIGHT_CODES
        if verdict.passes:
            assert type(policy) is int and 1 <= policy <= 10 ** 15
            assert unreadable is False
            assert raw in ("0", b"0")


def test_required_max_inflight_has_no_default():
    with pytest.raises(meter.MeterConfigurationError, match="inflight_policy_missing"):
        meter.load_max_inflight({})
    assert meter.load_max_inflight({"L2B_MAX_INFLIGHT": "8"}) == 8
    assert "attempt cap is excluded" in meter.MAX_INFLIGHT_DERIVATION


def test_module_is_dark_and_imports_no_provider_client():
    source = (REPO / "python" / "canon_lite_qc_meter.py").read_text("utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {
        "anthropic", "google", "laozhang_api", "openai", "requests", "httpx",
    }

    # DG-4 narrows this invariant rather than dropping it. The meter was previously
    # referenced by NOTHING, which proved L2B was dark. The ratified DG-4 path
    # deliberately wires ONE thing into production: the host sentinel, established by
    # narration_worker's own boot code so the metered-host role can never be claimed by a
    # job payload. Everything else must stay unreferenced — importing the sentinel must
    # not become a doorway for the metering machinery.
    # Exactly two permitted referrers, each with its OWN allowed name set. Anything else
    # in python/ must still not mention the meter at all.
    PERMITTED = {
        # The host sentinel, established by the worker's own boot code so the metered-host
        # role can never be claimed by a job payload.
        "narration_worker.py": {"declare_host_role", "metered_host_ok"},
        # The adapter, which raises the ratified configuration error and returns the
        # ratified usage type. No cycle: the meter does not import the provider.
        "canon_lite_qc_provider.py": {"MeterConfigurationError", "ProviderUsage"},
        # The runner — the ONLY sanctioned caller. It assembles the metered chain, so it
        # legitimately touches the machinery the worker must not. Its own gate order is
        # asserted separately in the DG-4 suite.
        "canon_lite_qc_runner.py": {
            "metered_host_ok", "AttemptContext", "MeteredProvider", "QcUsageSink",
            "load_extractor_concurrency", "load_max_inflight"},
    }
    for path in (REPO / "python").rglob("*.py"):
        if path.name == "canon_lite_qc_meter.py":
            continue
        text = path.read_text("utf-8")
        allowed = PERMITTED.get(path.name)
        if allowed is None:
            assert "canon_lite_qc_meter" not in text, path.name
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module == "canon_lite_qc_meter":
                assert {a.name for a in node.names} <= allowed, path.name
            elif isinstance(node, ast.Import):
                assert not any(a.name.startswith("canon_lite_qc_meter")
                               for a in node.names), path.name
    # The metering machinery itself stays out of the worker: importing the sentinel must
    # not become a doorway for the meter's execution path.
    worker = (REPO / "python" / "narration_worker.py").read_text("utf-8")
    for forbidden in ("MeteredProvider", "QcUsageSink", "AttemptContext",
                      "load_max_inflight"):
        assert forbidden not in worker, f"narration_worker reaches past the sentinel"
        assert "run_reaper_once" not in text


def test_reaper_entrypoint_is_standalone_and_matches_backup_job_pattern():
    source = (REPO / "python" / "canon_lite_qc_meter.py").read_text("utf-8")
    entrypoint = source[source.index("async def run_reaper_once"):]
    assert entrypoint.index("platform_qc_reap()") < entrypoint.index("_read_cache_state")
    assert entrypoint.index("_read_cache_state") < entrypoint.index("platform_qc_kill_sync")
    assert 'role != "platform_qc_reaper"' in entrypoint

    railway = (REPO / "backup" / "railway.json").read_text("utf-8")
    assert '"startCommand"' in railway
    assert '"restartPolicyType": "NEVER"' in railway


def test_phase_a_requires_flag_counter_and_open_rows():
    assert meter.phase_a_ready(
        l2b_enabled=False, inflight_raw="0", inflight_unreadable=False,
        attempted_rows=0, max_inflight=TEST_BOUND).passes
    assert meter.phase_a_ready(
        l2b_enabled=True, inflight_raw="0", inflight_unreadable=False,
        attempted_rows=0, max_inflight=TEST_BOUND).reason_code == "l2b_flag_not_off"
    assert meter.phase_a_ready(
        l2b_enabled=False, inflight_raw="1", inflight_unreadable=False,
        attempted_rows=0, max_inflight=TEST_BOUND).reason_code == "inflight_positive"
    assert meter.phase_a_ready(
        l2b_enabled=False, inflight_raw="0", inflight_unreadable=False,
        attempted_rows=1, max_inflight=TEST_BOUND).reason_code == "attempted_rows_open"


def test_phase_a_reads_product_resolver_verdict_not_set_unset_probe():
    mistyped_but_set = {"NARASI_CANON_LITE_MODE": "shdaow"}
    assert "NARASI_CANON_LITE_MODE" in mistyped_but_set
    assert meter.phase_a_from_product_resolver(
        inflight_raw="0",
        inflight_unreadable=False,
        attempted_rows=0,
        max_inflight=TEST_BOUND,
        environ=mistyped_but_set,
    ).passes
    assert meter.phase_a_from_product_resolver(
        inflight_raw="0",
        inflight_unreadable=False,
        attempted_rows=0,
        max_inflight=TEST_BOUND,
        environ={"NARASI_CANON_LITE_MODE": "shadow"},
    ).reason_code == "l2b_flag_not_off"


def test_reset_attestation_requires_all_four_points():
    note = {name: True for name in meter.RESET_ATTESTATION_FIELDS}
    assert meter.validate_reset_attestation(note) == (True, ())
    assert meter.RESET_ATTESTATION_NOTE_TEMPLATE[:2] == (
        "recorded_at_utc", "operator")
    assert meter.RESET_ATTESTATION_NOTE_TEMPLATE[2:] == meter.RESET_ATTESTATION_FIELDS
    note.pop("provider_accounting_has_no_open_request")
    ok, missing = meter.validate_reset_attestation(note)
    assert not ok and missing == ("provider_accounting_has_no_open_request",)


def test_decimal_usage_contract_rejects_float():
    meter.ProviderUsage(1, 2, Decimal("0.01"))
    with pytest.raises(ValueError, match="usage_payload_invalid"):
        meter.ProviderUsage(1, 2, 0.01)
    with pytest.raises(meter.MeterConfigurationError):
        replace(_context(), rate_in_usd_per_m=0.075)
    with pytest.raises(meter.MeterConfigurationError, match="phase_not_in_catalog"):
        replace(_context(), phase="not-in-the-versioned-catalog")


@pytest.mark.asyncio
async def test_success_records_lifecycle_and_cost_without_request_content(caplog):
    redis = FakeRedis()
    sink = FakeSink()
    provider_calls = []

    async def provider(request):
        provider_calls.append((request.chapter_index, request.attempt))
        return {"coverage": [], "claims": []}

    wrapped = _provider_wrapper(provider, sink, redis)
    with caplog.at_level(logging.ERROR):
        raw = await wrapped(_request())

    assert raw == {"coverage": [], "claims": []}
    assert provider_calls == [(0, 1)]
    assert [call[0] for call in sink.calls] == [
        "is_armed", "begin", "finish", "resolve"]
    assert redis.values[meter.INFLIGHT_KEY] == "0"
    assert wrapped.emitted_attempts == 1
    wrapped.assert_reconciled(1)
    assert "TOP SECRET" not in caplog.text
    assert "TOP SECRET" not in repr(sink.calls)


@pytest.mark.asyncio
async def test_begin_failure_arms_cache_before_authority_and_suppresses_provider(caplog):
    redis = FakeRedis()
    sink = FakeSink()
    sink.raise_on.add("begin")
    provider_called = False

    async def provider(_request):
        nonlocal provider_called
        provider_called = True
        return {}

    wrapped = _provider_wrapper(provider, sink, redis)
    with caplog.at_level(logging.ERROR), pytest.raises(
            meter.MeteringBlocked, match="begin_write_failed"):
        await wrapped(_request())

    assert not provider_called
    assert redis.values[meter.KILL_KEY] == "1"
    assert redis.values[meter.WRITE_FAIL_KEY] == "1"
    assert ("arm", "begin_write_failed") in sink.calls
    assert "db-secret" not in caplog.text
    cache_set = next(i for i, call in enumerate(redis.calls) if call[0] == "set"
                     and call[1] == meter.KILL_KEY)
    assert redis.calls[cache_set][3] == {}  # no TTL


@pytest.mark.asyncio
async def test_sink_failure_safety_order_is_ineligible_cache_authority_counter(monkeypatch):
    events = []

    class OrderedRedis(FakeRedis):
        async def set(self, key, value, **kwargs):
            if key == meter.KILL_KEY:
                events.append("cache")
            return await super().set(key, value, **kwargs)

        async def incr(self, key):
            events.append("counter")
            return await super().incr(key)

    class OrderedSink(FakeSink):
        async def arm(self, reason_code):
            events.append("authority")
            return await super().arm(reason_code)

    monkeypatch.setattr(meter, "_mark_ineligible", lambda: events.append("ineligible"))
    wrapped = _provider_wrapper(
        lambda _request: asyncio.sleep(0), OrderedSink(), OrderedRedis())
    await wrapped._arm("finish_write_failed")
    assert events == ["ineligible", "cache", "authority", "counter"]


@pytest.mark.asyncio
async def test_post_provider_sink_failure_returns_raw_and_marks_measurement_ineligible():
    redis = FakeRedis()
    sink = FakeSink()
    sink.raise_on.add("finish")

    async def provider(_request):
        return {"coverage": [], "claims": []}

    wrapped = _provider_wrapper(provider, sink, redis)
    raw = await wrapped(_request())
    assert raw == {"coverage": [], "claims": []}
    assert redis.values[meter.KILL_KEY] == "1"
    assert meter._process_killed is True


@pytest.mark.asyncio
async def test_provider_error_text_is_never_logged_or_persisted(caplog):
    redis = FakeRedis()
    sink = FakeSink()

    async def provider(_request):
        raise RuntimeError("PROVIDER PRIVATE ERROR TEXT")

    wrapped = _provider_wrapper(provider, sink, redis)
    with caplog.at_level(logging.DEBUG), pytest.raises(
            RuntimeError, match="PROVIDER PRIVATE ERROR TEXT"):
        await wrapped(_request())
    assert ("finish", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "failed") in sink.calls
    assert "PROVIDER PRIVATE ERROR TEXT" not in caplog.text
    assert "PROVIDER PRIVATE ERROR TEXT" not in repr(sink.calls)


@pytest.mark.asyncio
async def test_cancellation_writes_no_terminal_db_state_and_drains_counter():
    redis = FakeRedis()
    sink = FakeSink()
    started = asyncio.Event()

    async def provider(_request):
        started.set()
        await asyncio.Future()

    wrapped = _provider_wrapper(provider, sink, redis)
    task = asyncio.create_task(wrapped(_request()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [call[0] for call in sink.calls] == ["is_armed", "begin"]
    assert redis.values[meter.INFLIGHT_KEY] == "0"
    assert meter._cancelled_attempts == 1


@pytest.mark.asyncio
async def test_absent_cache_consults_postgres_and_both_stores_fail_closed():
    redis = FakeRedis()
    sink = FakeSink()

    async def provider(_request):
        return {}

    wrapped = _provider_wrapper(provider, sink, redis)
    assert await wrapped._killed() is False
    assert ("is_armed",) in sink.calls

    sink.armed = True
    assert await wrapped._killed() is True
    assert redis.values[meter.KILL_KEY] == "1"

    meter._process_killed = False
    redis.raise_get = True
    assert await wrapped._killed() is True

    meter._process_killed = False
    redis.raise_get = False
    redis.values.clear()
    sink.raise_on.add("is_armed")
    assert await wrapped._killed() is True


@pytest.mark.asyncio
async def test_redis_client_factory_failure_is_fail_closed_and_never_leaks_after_provider():
    def broken_getter():
        raise RuntimeError("redis-factory-secret")

    blocked = _provider_wrapper(
        lambda _request: asyncio.sleep(0), FakeSink(), FakeRedis())
    blocked._redis_getter = broken_getter
    assert await blocked._killed() is True

    redis = FakeRedis()
    sink = FakeSink()
    sink.raise_on.add("finish")
    calls = 0

    def fails_only_when_arming():
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise RuntimeError("redis-factory-secret")
        return redis

    async def provider(_request):
        return {"coverage": [], "claims": []}

    wrapped = _provider_wrapper(provider, sink, redis)
    wrapped._redis_getter = fails_only_when_arming
    assert await wrapped(_request()) == {"coverage": [], "claims": []}
    assert meter._process_killed is True


def test_run_id_distinguishes_queue_retry_and_reconciliation_mismatch_fails():
    assert _context("queue-run-1").run_id != _context("queue-run-2").run_id

    async def provider(_request):
        return {}

    wrapped = _provider_wrapper(provider, FakeSink(), FakeRedis())
    with pytest.raises(meter.ReconciliationMismatch, match="attempt_count_mismatch"):
        wrapped.assert_reconciled(1)


class FakeReaperConnection:
    def __init__(self, events, role="platform_qc_reaper"):
        self.events = events
        self.role = role

    async def fetchval(self, sql):
        if "current_user" in sql:
            self.events.append("role")
            return self.role
        self.events.append("reap")
        return 3

    async def fetchrow(self, sql, cache_state):
        self.events.append(("kill_sync", cache_state))
        return {
            "authority_repaired": False,
            "repair_skipped_reason": "cache_absent",
            "effective_armed": False,
        }

    async def close(self):
        self.events.append("close")


@pytest.mark.asyncio
async def test_reaper_order_is_sweep_then_redis_then_sync():
    events = []

    class OrderedRedis:
        async def get(self, key):
            events.append(("redis", key))
            return None

    conn = FakeReaperConnection(events)

    async def connector(_url):
        return conn

    result = await meter.run_reaper_once(
        database_url="postgresql://test.invalid/qc",
        redis_client=OrderedRedis(),
        connector=connector)
    assert result.reaped == 3
    assert events == [
        "role",
        "reap",
        ("redis", meter.KILL_KEY),
        ("kill_sync", "absent"),
        "close",
    ]


@pytest.mark.asyncio
async def test_reaper_refuses_owner_connection_before_sweep():
    events = []
    conn = FakeReaperConnection(events, role="neondb_owner")

    async def connector(_url):
        return conn

    with pytest.raises(meter.MeterConfigurationError, match="reaper_role_invalid"):
        await meter.run_reaper_once(
            database_url="postgresql://test.invalid/qc",
            redis_client=FakeRedis(),
            connector=connector)
    assert events == ["role", "close"]
