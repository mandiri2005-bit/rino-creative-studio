"""P0A acceptance suite — privacy-safe aggregate narration observability.

The point of this suite is not that the module runs; it is that the module CANNOT emit an
identifier, a raw provider/model/task string, an exact word count, or a fabricated zero,
and that with the flag off it performs no Redis work at all.

No real network, provider, production Redis, production DB, or tenant fixture is used. The
fake Redis below records every call so a test can assert on what was NOT done.
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "python"))

import narasi_observability as obs  # noqa: E402

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Deterministic fake Redis
# ---------------------------------------------------------------------------
class FakePipe:
    def __init__(self, store, log):
        self._store, self._log, self._ops = store, log, []

    def hincrby(self, key, field, amount):
        self._ops.append(("hincrby", key, field, amount))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    async def execute(self):
        # Atomic by construction: the whole op list applies or none of it does.
        for op in self._ops:
            self._log.append(op)
            if op[0] == "hincrby":
                self._store.setdefault(op[1], {})
                self._store[op[1]][op[2]] = self._store[op[1]].get(op[2], 0) + op[3]
            elif op[0] == "expire":
                self._store.setdefault("__ttl__", {})[op[1]] = op[2]
        self._ops = []
        return True


class FakeRedis:
    def __init__(self, *, fail=False):
        self.store, self.log, self.fail = {}, [], fail

    def pipeline(self, transaction=True):
        if self.fail:
            raise RuntimeError("redis down")
        return FakePipe(self.store, self.log)

    async def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise RuntimeError("redis down")
        self.log.append(("set", key, ex))
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def hgetall(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        self.log.append(("hgetall", key))
        return dict(self.store.get(key, {}))

    # Deliberately absent: keys/scan. A reader that reaches for them fails loudly.


@pytest.fixture
def fake(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(obs, "_client", lambda: r)
    monkeypatch.setenv(obs.FLAG, "1")
    return r


# ---------------------------------------------------------------------------
# §10.1 flag parser
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
def test_flag_enables_only_on_explicit_truthy(monkeypatch, value):
    monkeypatch.setenv(obs.FLAG, value)
    assert obs.enabled() is True


@pytest.mark.parametrize("value", ["", " ", "0", "false", "no", "off", "2", "enabled", "y"])
def test_flag_disabled_for_everything_else(monkeypatch, value):
    monkeypatch.setenv(obs.FLAG, value)
    assert obs.enabled() is False


def test_flag_unset_is_disabled(monkeypatch):
    monkeypatch.delenv(obs.FLAG, raising=False)
    assert obs.enabled() is False


# ---------------------------------------------------------------------------
# §10.1 enums — an unknown input never becomes a label
# ---------------------------------------------------------------------------
def test_enum_normalisation_never_passes_a_raw_value():
    hostile = "tenant-9f3c-secret"
    assert obs.normalize_route(hostile) == "unknown"
    assert obs.normalize_executor(hostile) == "unknown"
    assert obs.normalize_terminal(hostile) == "unknown"
    assert obs.normalize_provider(hostile) == "other"
    assert obs.normalize_phase(hostile) == "other"


class HostileStr(str):
    def __eq__(self, other):  # pragma: no cover - must never be invoked
        raise AssertionError("__eq__ ran on an unvalidated value")

    def __hash__(self):  # pragma: no cover
        raise AssertionError("__hash__ ran on an unvalidated value")


def test_str_subclass_is_rejected_before_any_hook_runs():
    # Exact-type gate first: the subclass's __eq__/__hash__ must never be reached.
    assert obs.normalize_route(HostileStr("api_direct")) == "unknown"
    assert obs.normalize_provider(HostileStr("kie")) == "other"


def test_non_string_inputs_do_not_crash_or_leak():
    for value in (None, 5, 5.0, True, [], {}, object()):
        assert obs.normalize_route(value) == "unknown"
        assert obs.normalize_provider(value) == "other"


def test_empty_provider_maps_to_other_not_a_fabricated_family():
    # The plain client leaves CallTelemetry.provider empty; that is "not observable",
    # which is `other` — never a guess at a family.
    assert obs.normalize_provider("") == "other"


# Every `task_id` shape below is a literal `task_id=` site in the pinned source, not an
# invented example. The earlier version of this test asserted on shapes production never
# emits (`chapter_7`, `story_bible`, `critique_pass2`), which passed while classifying
# almost nothing correctly — the mapping matched a fiction, and so did the test.
@pytest.mark.parametrize("task_id,expected", [
    # orchestrator/static.py:488,490,492,538,541,543,1118
    ("ch1", "chapter"), ("ch2", "chapter"), ("ch12", "chapter"),
    ("ch1:cont1", "chapter"), ("ch7:cont3", "chapter"),      # static.py:388
    ("single", "chapter"),                                    # router.py:406
    ("planner:bible", "bible"),                               # dynamic.py:2099
    ("polish:novel", "polish"),                               # static.py:1567,1636
    ("polish:novel:chunk2/5", "polish"),                      # static.py:1598,1607
    # Known shapes that are NOT one of the seven closed phases: `other`, never a guess.
    ("planner:outline", "other"),                             # dynamic.py:998
    ("planner:outline-titlefix", "other"),                    # dynamic.py:1029
    ("planner:subtasks", "other"),                            # dynamic.py:252
    ("diet2", "other"),                                       # narration_api.py:1543
    ("dyn:worker", "other"), ("dyn:synthesize", "other"),     # router.py:500,527
    ("cowork:alpha", "other"), ("cowork:synthesize", "other"),  # static.py:208,249
    ("narrator-worker-a", "other"),         # core.py:497/537 `task_id or worker.name`
    # Near-misses that must not be swept into `chapter`
    ("chapter_7", "other"), ("champion", "other"), ("ch", "other"),
    ("chx1", "other"), ("ch1:cont", "other"), ("ch1:contx", "other"),
    ("ch1:other", "other"),
])
def test_phase_mapping_matches_the_production_task_id_census(task_id, expected):
    assert obs.normalize_phase(task_id) == expected


def test_critic_and_revise_are_unreachable_from_provider_task_ids():
    """Those calls go through laozhang_api._narasi_cheap_call, which never reaches
    _UsageSink, so no CallTelemetry can carry them. Only the explicit phase argument used
    by record_phase_timing may produce them."""
    for shape in ("critique_pass2", "revise_ch3", "consistency", "rewrite", "critic_1"):
        assert obs.normalize_phase(shape) == "other"
    assert obs.normalize_phase("critic") == "critic"      # the explicit phase argument
    assert obs.normalize_phase("revise") == "revise"


def test_roles_are_not_phases():
    # The censused roles are worker/manager/synthesize/model/broll/"" — none is a phase.
    for role in ("worker", "manager", "synthesize", "model", "broll", ""):
        assert obs.normalize_phase(None, role) == "other"


def test_hostile_task_ids_cannot_reach_a_phase_or_burn_time():
    assert obs.normalize_phase("ch" + "9" * 400) == "other"     # over the length bound
    assert obs.normalize_phase("ch²") == "other"                # unicode digit, not ASCII
    assert obs.normalize_phase("ch1" + ":" * 5000) == "other"
    assert obs.normalize_phase("CH1") == "chapter"              # case-folded, still bounded


# ---------------------------------------------------------------------------
# §10.1 buckets — boundary ownership is exhaustively pinned
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("count,expected", [
    (1, "1"), (2, "2_3"), (3, "2_3"), (4, "4_5"), (5, "4_5"), (6, "6_10"),
    (10, "6_10"), (11, "11_20"), (20, "11_20"), (21, "gt_20"), (999, "gt_20"),
    (0, "unknown"), (-1, "unknown"), (None, "unknown"), (True, "unknown"),
    (3.0, "unknown"), ("3", "unknown"),
])
def test_chapter_bucket_boundaries(count, expected):
    assert obs.chapter_bucket(count) == expected


@pytest.mark.parametrize("words,expected", [
    # 0 is `unknown`, NOT the smallest real band: no narration request asks for nothing,
    # so a resolved 0 means the target could not be read, and bucketing it as le_5k would
    # manufacture a measurement out of a missing one.
    (0, "unknown"), (1, "le_5k_words"), (5000, "le_5k_words"), (5001, "5k_10k_words"),
    (10000, "5k_10k_words"), (10001, "10k_20k_words"), (20000, "10k_20k_words"),
    (20001, "20k_40k_words"), (40000, "20k_40k_words"), (40001, "gt_40k_words"),
    (-1, "unknown"), (None, "unknown"), (True, "unknown"), (40000.0, "unknown"),
])
def test_size_bucket_boundaries(words, expected):
    assert obs.size_bucket(words) == expected


# ---------------------------------------------------------------------------
# §10.1 histograms
# ---------------------------------------------------------------------------
def test_histogram_buckets_are_disjoint_and_cover_everything():
    bounds = obs.JOB_DURATION_BOUNDS_MS
    labels = obs._hist_labels(bounds)
    assert len(set(labels)) == len(labels)
    for bound in bounds:
        assert obs.bucket_label(bounds, bound) == "le_%d" % bound
        assert obs.bucket_label(bounds, bound + 1) != "le_%d" % bound
    assert obs.bucket_label(bounds, bounds[-1] + 1) == "overflow"
    assert obs.bucket_label(bounds, 0) == "le_%d" % bounds[0]


def test_quantiles_are_bucket_upper_bounds_and_absent_on_no_sample():
    bounds = obs.JOB_DURATION_BOUNDS_MS
    assert obs.quantile_from_histogram(bounds, {}, 0.5) is None
    counts = {"le_30000": 5, "le_60000": 5}
    assert obs.quantile_from_histogram(bounds, counts, 0.5) == 30000
    assert obs.quantile_from_histogram(bounds, counts, 0.95) == 60000
    # An overflow-dominated sample has no honest upper bound to report.
    assert obs.quantile_from_histogram(bounds, {"overflow": 3}, 0.5) is None


# ---------------------------------------------------------------------------
# §10.1 keys, TTL, window bounds
# ---------------------------------------------------------------------------
def test_hour_key_is_utc_and_rejects_naive_datetimes():
    assert obs.hour_key(datetime(2026, 7, 31, 5, 42, tzinfo=UTC)) == "2026073105"
    with pytest.raises(ValueError):
        obs.hour_key(datetime(2026, 7, 31, 5, 42))


def test_dedupe_key_is_one_way_and_never_contains_the_job_id():
    key = obs.dedupe_key("job-7f3c-abcd", "terminal")
    assert "job-7f3c-abcd" not in key
    assert key.startswith(obs.NAMESPACE + ":dedupe:")
    assert key != obs.dedupe_key("job-7f3c-abcd", "start")


def _start(job_id="j1", route="api_direct", chapters=1, words=1000):
    """The DISPATCH event."""
    return obs.record_job_start(route=route, chapter_count=chapters,
                                total_words=words, job_id=job_id)


def _exec(job_id="j1", executor="python_api"):
    """The EXECUTION event — a separate counter with a separate denominator."""
    return obs.record_execution(executor=executor, job_id=job_id)


def test_ttl_is_35_days_and_reader_window_is_capped_at_31(fake):
    asyncio.run(_start())
    ttls = [op[2] for op in fake.log if op[0] == "expire"]
    assert ttls and all(t == 35 * 24 * 3600 for t in ttls)
    start = datetime(2026, 6, 1, tzinfo=UTC)
    with pytest.raises(ValueError) as exc:
        obs.validate_window(start, start + timedelta(days=32), now=start + timedelta(days=60))
    assert "TOO_LONG" in str(exc.value)


@pytest.mark.parametrize("start,end,code", [
    (datetime(2026, 6, 2, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC), "INVERTED"),
    (datetime(2026, 6, 1, 0, 30, tzinfo=UTC), datetime(2026, 6, 2, tzinfo=UTC), "PARTIAL_HOUR"),
])
def test_window_validation_rejects_bad_shapes(start, end, code):
    with pytest.raises(ValueError) as exc:
        obs.validate_window(start, end, now=datetime(2026, 7, 1, tzinfo=UTC))
    assert code in str(exc.value)


def test_future_window_is_rejected():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    with pytest.raises(ValueError) as exc:
        obs.validate_window(now, now + timedelta(hours=2), now=now)
    assert "FUTURE" in str(exc.value)


def test_reader_enumerates_hour_keys_and_never_scans(fake):
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = start + timedelta(hours=3)
    snap = asyncio.run(obs.read_snapshot(start, end,
                                         metadata={"commit": "deadbeef"}))
    reads = [op for op in fake.log if op[0] == "hgetall"]
    assert len(reads) == 3
    assert not hasattr(fake, "keys") and not hasattr(fake, "scan")
    assert snap["coverage"] == "INSUFFICIENT_SAMPLE"


# ---------------------------------------------------------------------------
# §10.2 behavioural
# ---------------------------------------------------------------------------
def test_exactly_once_job_events_do_not_inflate_denominators(fake):
    for _ in range(3):
        asyncio.run(_start(job_id="job-1", route="bullmq_worker", chapters=10,
                           words=40000))
        asyncio.run(_exec(job_id="job-1", executor="narration_worker"))
        asyncio.run(obs.record_terminal(terminal="done", duration_ms=1000, job_id="job-1"))
    hour = obs.agg_key(obs.hour_key(datetime.now(UTC)))
    bucket = fake.store[hour]
    assert bucket["starts:total"] == 1
    assert bucket["executions:total"] == 1
    assert bucket["terminals:total"] == 1
    assert bucket["route:bullmq_worker"] == 1
    assert bucket["executor:narration_worker"] == 1


def test_dispatch_and_execution_keep_separate_denominators(fake, monkeypatch):
    """The gap between them IS the signal: jobs enqueued that never ran. Collapsing them
    into one counter makes that population unobservable by construction."""
    past = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    monkeypatch.setattr(obs, "_now", lambda: past)
    for index in range(3):
        asyncio.run(_start(job_id="d-%d" % index, route="bullmq_worker"))
    asyncio.run(_exec(job_id="d-0", executor="narration_worker"))   # only one ran
    monkeypatch.setattr(obs, "_now", lambda: datetime.now(UTC))
    snap = asyncio.run(obs.read_snapshot(past, past + timedelta(hours=1)))
    assert snap["denominators"]["starts_total"] == 3
    assert snap["denominators"]["executions_total"] == 1
    assert snap["route"] == {"bullmq_worker": 3}
    assert snap["executor"] == {"narration_worker": 1}


def test_provider_retries_each_count_as_a_real_call(fake):
    for _ in range(3):
        asyncio.run(obs.record_provider_call(provider="kie", phase="chapter", ok=True,
                                             tokens_in=10, tokens_out=20, latency_ms=500))
    bucket = fake.store[obs.agg_key(obs.hour_key(datetime.now(UTC)))]
    assert bucket["calls:kie:chapter"] == 3
    assert bucket["tokens_in:kie:chapter"] == 30
    assert bucket["tokens_out:kie:chapter"] == 60


def test_raw_telemetry_strings_never_reach_keys_values_or_snapshot(fake):
    secret_model = "claude-opus-4-6-SECRET"
    task = "chapter_7_tenant_9f3c_job_abcd"
    asyncio.run(obs.record_provider_call(provider=secret_model, phase=task, ok=True,
                                         tokens_in=1, tokens_out=1, latency_ms=1))
    blob = json.dumps(fake.store, default=str)
    assert secret_model not in blob
    assert task not in blob
    assert "9f3c" not in blob
    # and it still counted, under bounded labels. The phase is `other`, not `chapter`:
    # `chapter_7...` is not a production task-id shape, and guessing `chapter` from a
    # substring is how an identifier-shaped string earns a real-looking label.
    bucket = fake.store[obs.agg_key(obs.hour_key(datetime.now(UTC)))]
    assert bucket["calls:other:other"] == 1


def test_exact_word_count_is_never_stored(fake):
    asyncio.run(_start(job_id="job-w", chapters=10, words=40321))
    blob = json.dumps(fake.store, default=str)
    assert "40321" not in blob
    assert "size:gt_40k_words" in fake.store[obs.agg_key(obs.hour_key(datetime.now(UTC)))]


def test_redis_failure_is_swallowed_and_never_raises(monkeypatch):
    monkeypatch.setenv(obs.FLAG, "1")
    monkeypatch.setattr(obs, "_client", lambda: FakeRedis(fail=True))
    assert asyncio.run(_start(job_id="j")) is False
    assert asyncio.run(obs.record_provider_call(provider="kie", phase="chapter", ok=True,
                                                tokens_in=1, tokens_out=1,
                                                latency_ms=1)) is False
    assert asyncio.run(obs.record_terminal(terminal="done", duration_ms=1,
                                           job_id="j")) is False


def test_every_terminal_status_is_recorded_under_a_bounded_label(fake):
    for index, status in enumerate(("done", "failed", "cancelled", "exploded")):
        asyncio.run(obs.record_terminal(terminal=status, duration_ms=1000,
                                        job_id="job-%d" % index))
    bucket = fake.store[obs.agg_key(obs.hour_key(datetime.now(UTC)))]
    assert bucket["terminal:done"] == 1
    assert bucket["terminal:failed"] == 1
    assert bucket["terminal:cancelled"] == 1
    assert bucket["terminal:unknown"] == 1        # "exploded" never appears verbatim


def test_snapshot_reconciles_and_is_canonical(fake, monkeypatch):
    # Pin the writer to a COMPLETED past hour: the current hour is partial, and
    # validate_window rightly refuses a window that ends in the future.
    past = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    monkeypatch.setattr(obs, "_now", lambda: past)
    now = past
    asyncio.run(_start(job_id="a", route="bullmq_worker", chapters=10, words=40000))
    asyncio.run(_exec(job_id="a", executor="narration_worker"))
    asyncio.run(obs.record_terminal(terminal="done", duration_ms=90000, job_id="a"))
    monkeypatch.setattr(obs, "_now", lambda: datetime.now(UTC))
    snap = asyncio.run(obs.read_snapshot(now, now + timedelta(hours=1)))
    assert sum(snap["route"].values()) == snap["denominators"]["starts_total"]
    assert sum(snap["executor"].values()) == snap["denominators"]["executions_total"]
    assert snap["denominators"]["terminals_total"] <= snap["denominators"]["starts_total"]
    assert snap["quantile_method"] == "fixed_bucket_upper_bound"
    assert obs.canonical_json(snap) == obs.canonical_json(json.loads(obs.canonical_json(snap)))


def test_zero_event_window_is_insufficient_sample_not_a_clean_zero(fake):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    snap = asyncio.run(obs.read_snapshot(now - timedelta(hours=2), now))
    assert snap["coverage"] == "INSUFFICIENT_SAMPLE"
    assert snap["route"] == {}
    # Closed schema: every histogram key is PRESENT and explicitly None. A consumer must
    # never have to decide whether a missing key meant "no data" or "no such metric".
    assert set(snap["quantiles_ms"]) == set(obs.HISTOGRAMS)
    assert all(v is None for v in snap["quantiles_ms"].values())
    assert all(v is None for v in snap["histograms"].values())
    # And not one fabricated zero anywhere a denominator could be read as a measurement.
    assert all(v is None for v in snap["denominators"].values())
    assert snap["writer_health"]["ok"] is None and snap["writer_health"]["fail"] is None
    assert set(snap["delivery_coverage"].values()) == {"UNKNOWN"}


def test_concurrent_increments_are_not_lost(fake):
    async def drive():
        await asyncio.gather(*[
            obs.record_provider_call(provider="kie", phase="chapter", ok=True,
                                     tokens_in=1, tokens_out=1, latency_ms=10)
            for _ in range(50)])
    asyncio.run(drive())
    bucket = fake.store[obs.agg_key(obs.hour_key(datetime.now(UTC)))]
    assert bucket["calls:kie:chapter"] == 50
    assert bucket["tokens_in:kie:chapter"] == 50


def test_snapshot_never_contains_a_dedupe_key(fake, monkeypatch):
    past = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    monkeypatch.setattr(obs, "_now", lambda: past)
    now = past
    asyncio.run(_start(job_id="secret-job", words=100))
    monkeypatch.setattr(obs, "_now", lambda: datetime.now(UTC))
    snap = asyncio.run(obs.read_snapshot(now, now + timedelta(hours=1)))
    blob = obs.canonical_json(snap)
    assert "dedupe" not in blob
    assert "secret-job" not in blob


# ---------------------------------------------------------------------------
# Flag-off: no Redis work of any kind
# ---------------------------------------------------------------------------
def test_flag_off_performs_no_redis_operation(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(obs, "_client", lambda: r)
    monkeypatch.delenv(obs.FLAG, raising=False)
    asyncio.run(_start(job_id="j", words=100))
    asyncio.run(obs.record_terminal(terminal="done", duration_ms=1, job_id="j"))
    asyncio.run(obs.record_provider_call(provider="kie", phase="chapter", ok=True,
                                         tokens_in=1, tokens_out=1, latency_ms=1))
    asyncio.run(obs.record_gate_total(elapsed_ms=1, job_id="j"))
    assert r.log == []
    assert r.store == {}


# ---------------------------------------------------------------------------
# Logical calls, outcomes and provider families are three axes, not one
# ---------------------------------------------------------------------------
def _bucket(fake):
    return fake.store[obs.agg_key(obs.hour_key(datetime.now(UTC)))]


def test_call_outcome_never_shares_the_provider_call_namespace(fake):
    asyncio.run(obs.record_provider_call(provider="kie", phase="ch1", ok=True,
                                         tokens_in=1, tokens_out=2, latency_ms=5))
    asyncio.run(obs.record_provider_call(provider="kie", phase="ch1", ok=False,
                                         tokens_in=1, tokens_out=0, latency_ms=5))
    bucket = _bucket(fake)
    assert bucket["calls:kie:chapter"] == 2
    assert bucket["logical_calls:total"] == 2
    assert bucket["outcome:ok"] == 1
    assert bucket["outcome:not_ok"] == 1
    # The old shape put ok/not_ok under `calls:`, so summing the provider breakdown
    # silently counted every call twice.
    assert "calls:ok" not in bucket
    assert "calls:not_ok" not in bucket


def test_snapshot_group_excludes_the_total_rollup_stored_beside_it(fake, monkeypatch):
    past = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    monkeypatch.setattr(obs, "_now", lambda: past)
    for provider in ("kie", "laozhang", "kie"):
        asyncio.run(obs.record_provider_call(provider=provider, phase="ch1", ok=True,
                                             tokens_in=3, tokens_out=4, latency_ms=200))
    monkeypatch.setattr(obs, "_now", lambda: datetime.now(UTC))
    snap = asyncio.run(obs.read_snapshot(past, past + timedelta(hours=1)))
    assert "total" not in snap["provider_calls"]
    assert sum(snap["provider_calls"].values()) == 3
    assert snap["denominators"]["logical_calls_total"] == 3
    assert snap["call_outcome"] == {"ok": 3}
    assert snap["provider_latency_sum_ms"] == {"kie:chapter": 400, "laozhang:chapter": 200}


def test_instrumentation_scope_is_the_locked_census_and_does_not_move_with_traffic(
        fake, monkeypatch):
    past = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    monkeypatch.setattr(obs, "_now", lambda: past)
    quiet = asyncio.run(obs.read_snapshot(past - timedelta(hours=1), past))
    for _ in range(7):
        asyncio.run(obs.record_provider_call(provider="kie", phase="ch1", ok=True,
                                             tokens_in=1, tokens_out=1, latency_ms=1))
    monkeypatch.setattr(obs, "_now", lambda: datetime.now(UTC))
    busy = asyncio.run(obs.read_snapshot(past, past + timedelta(hours=1)))

    scope = busy["instrumentation_scope"]
    # The census is a per-FILE fact; an unattached "18" reads as a whole-tree total.
    assert scope["cheap_call_census_scope"] == "narration_api.py"
    assert scope["cheap_call_sites_literal_name"] == 13
    assert scope["cheap_call_sites_alias_resolved"] == 5
    assert scope["cheap_call_sites_static_total"] == 18
    assert scope["cheap_call_import_lines_not_call_sites"] == 10
    assert scope["cheap_call_raw_grep_lines_not_a_denominator"] == 23
    # The honest negatives: the cheap-call surface is NOT instrumented, and physical
    # per-rung HTTP attempts inside the failover client are not observable from here.
    assert scope["cheap_call_sites_instrumented"] == 0
    assert scope["physical_rung_attempts_instrumented"] is False
    assert scope["usage_sink_logical_calls_instrumented"] is True
    # The provider surface is PARTIAL by construction, and says so — three distinct
    # uninstrumented paths, and no promotion of P0 on top of them.
    assert scope["provider_surface_completeness"] == "PARTIAL"
    assert scope["p0_promotion_blocked_by_provider_gap"] is True
    assert scope["critic_revise_provider_latency"] == "UNKNOWN"
    assert scope["critic_revise_provider_path"] == (
        "direct_make_narasi_client_and_log_narasi_usage")
    assert scope["heartbeat_present"] is False
    assert scope["delivery_complete_claimable"] is False
    # Route is a dispatch census again, so nothing here may redefine it as execution.
    assert "route_observed_at" not in scope
    assert "enqueued_but_never_executed_observable" not in scope
    # Static axis: identical for a silent window and a busy one. Delivery is what moves.
    assert quiet["instrumentation_scope"] == busy["instrumentation_scope"]
    assert quiet["delivery_coverage"]["provider_calls"] == "UNKNOWN"
    # Observed, but never plain OBSERVED: the known gaps make it PARTIAL.
    assert busy["delivery_coverage"]["provider_calls"] == "PARTIAL"


def test_one_route_event_never_fabricates_a_provider_denominator(fake, monkeypatch):
    past = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    monkeypatch.setattr(obs, "_now", lambda: past)
    asyncio.run(_start(job_id="j-cov", chapters=2, words=6000))
    monkeypatch.setattr(obs, "_now", lambda: datetime.now(UTC))
    snap = asyncio.run(obs.read_snapshot(past, past + timedelta(hours=1)))
    assert snap["delivery_coverage"]["route"] == "OBSERVED"
    assert snap["denominators"]["starts_total"] == 1
    # The provider dimension was NOT observed. It used to read 0 here — "we measured
    # none" — purely because some other dimension had written that hour's hash.
    assert snap["delivery_coverage"]["provider_calls"] == "UNKNOWN"
    assert snap["denominators"]["logical_calls_total"] is None
    assert snap["denominators"]["outer_attempts_total"] is None
    assert snap["denominators"]["physical_rung_attempts_total"] is None
    assert set(snap["delivery_coverage"]) == set(
        name for name, _ in obs._GROUP_DIMENSIONS) | set(obs.HISTOGRAMS)


def test_a_fully_populated_window_is_unverified_not_complete(fake, monkeypatch):
    past = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    monkeypatch.setattr(obs, "_now", lambda: past)
    asyncio.run(_start(job_id="j-unv"))
    monkeypatch.setattr(obs, "_now", lambda: datetime.now(UTC))
    snap = asyncio.run(obs.read_snapshot(past, past + timedelta(hours=1)))
    # Every requested hour carried a hash. That proves a writer ran, not that every event
    # arrived — without a heartbeat a quiet hour and a lost one are indistinguishable.
    assert snap["window"]["hours_with_data"] == snap["window"]["hours_requested"]
    assert snap["coverage"] == "UNVERIFIED"
    assert "COMPLETE" not in obs.canonical_json(snap)


def test_writer_health_does_not_present_zero_failures_as_liveness(fake, monkeypatch):
    past = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    monkeypatch.setattr(obs, "_now", lambda: past)
    asyncio.run(_start(job_id="j-health"))
    monkeypatch.setattr(obs, "_now", lambda: datetime.now(UTC))
    snap = asyncio.run(obs.read_snapshot(past, past + timedelta(hours=1)))
    assert snap["writer_health"]["fail"] is None       # never written, so not zero
    assert snap["writer_health"]["ok"] == 1
    assert snap["writer_health"]["interpretation"] == (
        "attempted_writes_only;fail_zero_is_not_liveness")


def test_snapshot_keyset_is_closed(fake):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    snap = asyncio.run(obs.read_snapshot(now - timedelta(hours=1), now))
    assert set(snap) == {
        "schema_version", "namespace", "window", "coverage", "quantile_method",
        "instrumentation_scope", "delivery_coverage", "denominators", "histograms",
        "quantiles_ms", "writer_health", "metadata",
        "route", "executor", "terminal", "chapter_bucket", "size_bucket",
        "provider_calls", "call_outcome", "tokens_in", "tokens_out",
        "provider_latency_sum_ms",
    }


# ---------------------------------------------------------------------------
# The reader fails CLOSED on anything it did not write
# ---------------------------------------------------------------------------
def _seed_hour(fake, hour_dt, fields):
    fake.store[obs.agg_key(obs.hour_key(hour_dt))] = dict(fields)


def _past_hour():
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)


def test_an_unknown_redis_field_fails_closed_and_never_reaches_the_snapshot(fake):
    past = _past_hour()
    _seed_hour(fake, past, {"route:api_direct": 1, "route:tenant_marker": 7})
    with pytest.raises(ValueError) as exc:
        asyncio.run(obs.read_snapshot(past, past + timedelta(hours=1)))
    assert "P0A_SCHEMA_INVALID" in str(exc.value)
    # It used to be exported verbatim under `route`, straight into the JSON snapshot.
    assert "tenant_marker" not in str(exc.value)


@pytest.mark.parametrize("field", [
    "route:tenant-9f3c", "starts:total:extra", "calls:megacorp:chapter",
    "hist:job_duration_ms:le_31337", "terminal:exploded", "size:le_3k_words",
    "dedupe:abc", "note", "hist:critic_ms:count",
])
def test_every_shape_of_unexpected_field_fails_closed(fake, field):
    past = _past_hour()
    _seed_hour(fake, past, {field: 1})
    with pytest.raises(ValueError):
        asyncio.run(obs.read_snapshot(past, past + timedelta(hours=1)))


@pytest.mark.parametrize("value", ["", "abc", "1.5", "-3", None, "9f3c"])
def test_a_malformed_value_fails_closed_rather_than_being_skipped(fake, value):
    past = _past_hour()
    _seed_hour(fake, past, {"starts:total": value})
    with pytest.raises(ValueError):
        asyncio.run(obs.read_snapshot(past, past + timedelta(hours=1)))


def test_the_cli_reports_schema_drift_as_a_static_code(fake, monkeypatch, capsys):
    past = _past_hour()
    _seed_hour(fake, past, {"route:tenant_marker": 1})
    rc = obs._main(["narasi_observability", "snapshot",
                    "--from", past.strftime("%Y-%m-%dT%H:00:00Z"),
                    "--to", (past + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00Z")])
    captured = capsys.readouterr()
    assert rc == 3
    assert captured.err.strip() == "P0A_SCHEMA_INVALID"
    assert captured.out == ""
    assert "tenant_marker" not in captured.err


def test_the_expected_field_set_is_derived_from_the_closed_enums():
    expected = obs.EXPECTED_FIELDS
    for route in obs.ROUTES:
        assert "route:%s" % route in expected
    for provider in obs.PROVIDERS:
        for phase in obs.PHASES:
            assert "calls:%s:%s" % (provider, phase) in expected
    for name, bounds in obs.HISTOGRAMS.items():
        assert "hist:%s:count" % name in expected
        for label in obs._hist_labels(bounds):
            assert "hist:%s:%s" % (name, label) in expected
    assert "route:tenant_marker" not in expected


# ---------------------------------------------------------------------------
# UTC is validated in the READER, not only in the CLI parser
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("hours", [7, -5, 1])
def test_read_snapshot_refuses_a_non_utc_window(fake, hours):
    past = _past_hour()
    other = timezone(timedelta(hours=hours))
    with pytest.raises(ValueError) as exc:
        asyncio.run(obs.read_snapshot(past.astimezone(other),
                                      (past + timedelta(hours=1)).astimezone(other)))
    assert "P0A_WINDOW_NOT_UTC" in str(exc.value)


def test_validate_window_checks_the_offset_on_start_end_and_reference():
    utc_start = datetime(2026, 6, 1, tzinfo=UTC)
    utc_end = datetime(2026, 6, 2, tzinfo=UTC)
    east = timezone(timedelta(hours=7))
    for start, end, now in (
        (utc_start.astimezone(east), utc_end, datetime(2026, 7, 1, tzinfo=UTC)),
        (utc_start, utc_end.astimezone(east), datetime(2026, 7, 1, tzinfo=UTC)),
        (utc_start, utc_end, datetime(2026, 7, 1, tzinfo=UTC).astimezone(east)),
    ):
        with pytest.raises(ValueError) as exc:
            obs.validate_window(start, end, now=now)
        assert "P0A_WINDOW_NOT_UTC" in str(exc.value)
    obs.validate_window(utc_start, utc_end, now=datetime(2026, 7, 1, tzinfo=UTC))


def test_a_non_utc_window_cannot_silently_read_different_hours(fake):
    """The offending case: +07:00 was accepted, and the hours enumerated were then not
    the hours the window printed."""
    past = _past_hour()
    east = timezone(timedelta(hours=7))
    with pytest.raises(ValueError):
        asyncio.run(obs.read_snapshot(past.astimezone(east),
                                      (past + timedelta(hours=1)).astimezone(east)))
    assert [op for op in fake.log if op[0] == "hgetall"] == []


# ---------------------------------------------------------------------------
# Metadata is a closed keyset, not a passthrough
# ---------------------------------------------------------------------------
def test_metadata_drops_unknown_keys_and_validates_every_shape(monkeypatch):
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    md = obs.bounded_metadata({
        "deploy_sha": "z" * 40,                 # right length, not hex
        "config_digest": "a" * 64,
        "service": "python",
        "tenant_id": "tenant-9f3c",             # must not survive
        "redis_url": "redis://user:pa55@host",  # must not survive
    })
    assert set(md) == {"schema_version", "namespace", "service", "deploy_sha",
                       "config_digest", "peer_service", "peer_deploy_sha",
                       "peer_config_digest", "two_service_commit_parity",
                       "two_service_config_parity"}
    assert md["deploy_sha"] == "UNKNOWN"
    assert md["config_digest"] == "a" * 64
    blob = json.dumps(md)
    assert "9f3c" not in blob and "redis://" not in blob and "pa55" not in blob


def test_metadata_carries_two_service_parity_without_a_secret():
    same = {"service": "python", "deploy_sha": "d" * 40, "config_digest": "c" * 64,
            "peer_service": "narration_worker", "peer_deploy_sha": "d" * 40,
            "peer_config_digest": "c" * 64}
    md = obs.bounded_metadata(same)
    assert md["two_service_commit_parity"] == "MATCH"
    assert md["two_service_config_parity"] == "MATCH"
    drifted = dict(same, peer_deploy_sha="e" * 40)
    assert obs.bounded_metadata(drifted)["two_service_commit_parity"] == "MISMATCH"
    # A missing peer is UNKNOWN, never a silent MATCH.
    partial = dict(same)
    partial.pop("peer_deploy_sha")
    assert obs.bounded_metadata(partial)["two_service_commit_parity"] == "UNKNOWN"


def test_metadata_takes_the_deploy_sha_from_railways_non_secret_build_var(monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA",
                       "DEB771319157E37CBEE7689627FF2CFF6AEB47F9")
    assert obs.bounded_metadata()["deploy_sha"] == (
        "deb771319157e37cbee7689627ff2cff6aeb47f9")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "not-a-sha")
    assert obs.bounded_metadata()["deploy_sha"] == "UNKNOWN"


def test_an_unknown_service_label_is_never_passed_through():
    assert obs.bounded_metadata({"service": "tenant-42"})["service"] == "unknown"
    assert obs.bounded_metadata({"service": "narration_worker"})["service"] == (
        "narration_worker")


def test_reader_metadata_cannot_smuggle_a_free_form_label(fake):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    snap = asyncio.run(obs.read_snapshot(now - timedelta(hours=1), now,
                                         metadata={"note": "job 9f3c for tenant acme"}))
    assert "9f3c" not in obs.canonical_json(snap)
    assert "acme" not in obs.canonical_json(snap)


# ---------------------------------------------------------------------------
# Critic/revise timing — from the timers the gate phase already keeps
# ---------------------------------------------------------------------------
def test_phase_timing_accepts_only_the_closed_timed_phases(fake):
    assert asyncio.run(obs.record_phase_timing(phase="critic", elapsed_ms=45_000,
                                               job_id="j1")) is True
    assert asyncio.run(obs.record_phase_timing(phase="revise", elapsed_ms=700_000,
                                               job_id="j1")) is True
    for refused in ("chapter", "gate_total", "polish", "bible", "other", "ch1", None, 7):
        assert asyncio.run(obs.record_phase_timing(phase=refused, elapsed_ms=1_000,
                                                   job_id="j1")) is False
    bucket = _bucket(fake)
    assert bucket["hist:critic_phase_wall_ms:le_60000"] == 1
    assert bucket["hist:critic_phase_wall_ms:count"] == 1
    assert bucket["hist:critic_phase_wall_ms:sum"] == 45_000
    assert bucket["hist:revise_phase_wall_ms:le_1200000"] == 1
    assert bucket["hist:revise_phase_wall_ms:sum"] == 700_000


def test_phase_timing_is_once_per_job_per_phase(fake):
    for _ in range(4):
        asyncio.run(obs.record_phase_timing(phase="critic", elapsed_ms=1_000, job_id="j"))
    asyncio.run(obs.record_phase_timing(phase="revise", elapsed_ms=1_000, job_id="j"))
    bucket = _bucket(fake)
    assert bucket["hist:critic_phase_wall_ms:count"] == 1
    assert bucket["hist:revise_phase_wall_ms:count"] == 1      # a second phase is a separate claim
    asyncio.run(obs.record_phase_timing(phase="critic", elapsed_ms=1_000, job_id="j2"))
    assert _bucket(fake)["hist:critic_phase_wall_ms:count"] == 2   # a second job counts again


def test_phase_timing_rejects_hostile_elapsed_values(fake):
    for bad in (-1, True, 1.5, "1000", None, 10 ** 15):
        assert asyncio.run(obs.record_phase_timing(phase="critic", elapsed_ms=bad,
                                                   job_id="jx")) is False
    assert obs.agg_key(obs.hour_key(datetime.now(UTC))) not in fake.store


# ---------------------------------------------------------------------------
# UTC offsets
# ---------------------------------------------------------------------------
def test_hour_key_converts_a_non_utc_offset_instead_of_truncating_it():
    east = datetime(2026, 7, 31, 9, 30, tzinfo=timezone(timedelta(hours=7)))
    assert obs.hour_key(east) == "2026073102"        # 09:30+07:00 is 02:30 UTC
    west = datetime(2026, 7, 31, 21, 15, tzinfo=timezone(timedelta(hours=-5)))
    assert obs.hour_key(west) == "2026080102"        # and this one crosses into August


def test_window_parser_accepts_utc_spellings_and_rejects_every_other_offset():
    assert obs._parse_utc("2026-07-31T00:00:00Z") == datetime(2026, 7, 31, tzinfo=UTC)
    assert obs._parse_utc("2026-07-31T00:00:00+00:00") == datetime(2026, 7, 31, tzinfo=UTC)
    for offset in ("+07:00", "-05:00", "+00:01"):
        with pytest.raises(ValueError) as exc:
            obs._parse_utc("2026-07-31T00:00:00" + offset)
        assert "NOT_UTC" in str(exc.value)


def test_hours_between_crosses_the_utc_day_boundary():
    start = datetime(2026, 7, 31, 23, tzinfo=UTC)
    assert obs.hours_between(start, start + timedelta(hours=2)) == [
        "2026073123", "2026080100"]


# ---------------------------------------------------------------------------
# Split failure: the claim is spent, the write is lost, nothing is double counted
# ---------------------------------------------------------------------------
class WriteLegDownRedis(FakeRedis):
    """Claims succeed, every aggregate write fails."""

    def pipeline(self, transaction=True):
        raise RuntimeError("write leg down")


class FirstWriteFailsRedis(FakeRedis):
    """Only the first pipeline fails, so the failure counter itself can still land."""

    def __init__(self):
        super().__init__()
        self._tripped = False

    def pipeline(self, transaction=True):
        if not self._tripped:
            self._tripped = True
            raise RuntimeError("transient")
        return FakePipe(self.store, self.log)


def test_a_split_failure_burns_the_claim_and_never_double_counts(monkeypatch):
    broken = WriteLegDownRedis()
    monkeypatch.setenv(obs.FLAG, "1")
    monkeypatch.setattr(obs, "_client", lambda: broken)
    assert asyncio.run(obs.record_terminal(terminal="done", duration_ms=1_000,
                                           job_id="j")) is False
    assert any(op[0] == "set" for op in broken.log)          # the claim WAS taken

    # The write leg recovers. The retry must not resurrect the lost event: under-counting
    # is visible as a coverage gap, whereas a re-count silently inflates a denominator.
    healed = FakeRedis()
    healed.store.update(broken.store)                        # the claim marker persists
    monkeypatch.setattr(obs, "_client", lambda: healed)
    assert asyncio.run(obs.record_terminal(terminal="done", duration_ms=1_000,
                                           job_id="j")) is False
    assert obs.agg_key(obs.hour_key(datetime.now(UTC))) not in healed.store


def test_a_lost_write_is_visible_as_writer_fail(monkeypatch):
    r = FirstWriteFailsRedis()
    monkeypatch.setenv(obs.FLAG, "1")
    monkeypatch.setattr(obs, "_client", lambda: r)
    assert asyncio.run(_start(job_id="j")) is False
    bucket = r.store[obs.agg_key(obs.hour_key(datetime.now(UTC)))]
    assert bucket["writer:fail"] == 1
    assert "writer:ok" not in bucket
    assert "starts:total" not in bucket


def test_writer_health_rides_in_the_same_transaction_as_its_event(fake):
    asyncio.run(_start(job_id="j"))
    bucket = _bucket(fake)
    assert bucket["starts:total"] == 1
    assert bucket["writer:ok"] == 1
    # One transaction, not two: exactly one expire for the one hour key touched.
    assert len([op for op in fake.log if op[0] == "expire"]) == 1


# ---------------------------------------------------------------------------
# Operator CLI — bounded static codes, never an echo of the input
# ---------------------------------------------------------------------------
@pytest.fixture
def no_cli_connect(monkeypatch):
    """Neutralise the CLI's own client lifecycle for the reader tests below.

    Those tests inject a client at `obs._client`, so what they exercise is the reader.
    Without this the CLI's real `init_redis()` would run and dial `REDIS_URL` — which
    defaults to `redis://localhost:6379` — opening a real socket in a suite whose stated
    contract is that it opens none, and making the result depend on whatever happens to be
    listening on the machine. The lifecycle itself is bound directly, against the
    production wiring, in the section that follows.
    """
    import redis_client as rc
    calls = {"init": 0, "close": 0}

    async def _init():
        calls["init"] += 1

    async def _close():
        calls["close"] += 1

    monkeypatch.setattr(rc, "init_redis", _init)
    monkeypatch.setattr(rc, "close_redis", _close)
    return calls


def test_cli_never_echoes_its_input_in_a_diagnostic(monkeypatch, capsys, no_cli_connect):
    monkeypatch.setattr(obs, "_client", lambda: FakeRedis())
    hostile = "2026-13-45T99:99:99Z tenant-9f3c redis://user:pa55@h"
    rc = obs._main(["narasi_observability", "snapshot", "--from", hostile,
                    "--to", "2026-07-01T00:00:00Z"])
    err = capsys.readouterr().err
    assert rc == 2
    assert err.strip() in obs._CLI_WINDOW_CODES | {"P0A_WINDOW_INVALID"}
    for leak in ("9f3c", "redis://", "pa55", "2026-13-45", "Invalid isoformat"):
        assert leak not in err


@pytest.mark.parametrize("frm,to,code", [
    ("2026-06-02T00:00:00Z", "2026-06-01T00:00:00Z", "P0A_WINDOW_INVERTED"),
    ("2026-06-01T00:30:00Z", "2026-06-02T00:00:00Z", "P0A_WINDOW_PARTIAL_HOUR"),
    ("2026-06-01T00:00:00Z", "2099-01-01T00:00:00Z", "P0A_WINDOW_IN_FUTURE"),
    ("2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z", "P0A_WINDOW_TOO_LONG"),
    ("2026-06-01T00:00:00+07:00", "2026-06-02T00:00:00Z", "P0A_WINDOW_NOT_UTC"),
])
def test_cli_window_errors_are_exact_static_codes(monkeypatch, capsys, frm, to, code,
                                                  no_cli_connect):
    monkeypatch.setattr(obs, "_client", lambda: FakeRedis())
    assert obs._main(["narasi_observability", "snapshot", "--from", frm, "--to", to]) == 2
    assert capsys.readouterr().err.strip() == code


def test_cli_reports_redis_unavailable_without_the_url(monkeypatch, capsys, no_cli_connect):
    def boom():
        raise RuntimeError("redis://user:pa55@prod-redis:6379 connection refused")
    monkeypatch.setattr(obs, "_client", boom)
    past = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    rc = obs._main(["narasi_observability", "snapshot",
                    "--from", past.strftime("%Y-%m-%dT%H:00:00Z"),
                    "--to", (past + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00Z")])
    err = capsys.readouterr().err
    assert rc == 3
    assert err.strip() == "P0A_REDIS_UNAVAILABLE"
    assert "redis://" not in err and "pa55" not in err


def test_cli_degrades_an_unrecognised_read_failure_to_a_generic_code(monkeypatch, capsys,
                                                                    no_cli_connect):
    class Exploding(FakeRedis):
        async def hgetall(self, key):
            raise RuntimeError("AUTH failed for redis://user:pa55@prod-redis:6379")

    monkeypatch.setattr(obs, "_client", lambda: Exploding())
    past = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    rc = obs._main(["narasi_observability", "snapshot",
                    "--from", past.strftime("%Y-%m-%dT%H:00:00Z"),
                    "--to", (past + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00Z")])
    err = capsys.readouterr().err
    assert rc == 3
    assert err.strip() == "P0A_SNAPSHOT_FAILED"
    assert "pa55" not in err and "redis://" not in err


def test_cli_emits_canonical_json_and_a_bounded_service_label(monkeypatch, capsys,
                                                              no_cli_connect):
    r = FakeRedis()
    monkeypatch.setattr(obs, "_client", lambda: r)
    past = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    rc = obs._main(["narasi_observability", "snapshot",
                    "--from", past.strftime("%Y-%m-%dT%H:00:00Z"),
                    "--to", (past + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00Z")])
    out = capsys.readouterr().out
    assert rc == 4                                   # nonzero: no data is not a success
    payload = json.loads(out)
    assert obs.canonical_json(payload) == out.strip()
    assert payload["metadata"]["service"] == "operator_cli"
    assert payload["coverage"] == "INSUFFICIENT_SAMPLE"


# ---------------------------------------------------------------------------
# Operator CLI — the PRODUCTION client lifecycle
#
# Everything above hands the reader a client at `obs._client`. That proves the reader and
# says nothing about how a standalone `python -m narasi_observability snapshot` process
# gets one: it has no service startup, so `redis_client.client()` stayed None for its whole
# life and EVERY window failed with `P0A_REDIS_UNAVAILABLE` — forever, on any input. 191
# green tests missed it because the suite stubbed `_client` fifteen times and called the
# real one zero times.
#
# So these tests do NOT patch `obs._client`. They stub exactly one thing — the
# `aioredis.from_url` constructor — so `redis_client.init_redis()` runs its own real body,
# installs the client where production installs it, and the real `_client()` is what the
# real `read_snapshot` resolves. What binds that is not a shape check: it is that the
# client instance handed to `from_url` is the one that receives the reader's `hgetall`.
# ---------------------------------------------------------------------------
class LifecycleRedis(FakeRedis):
    """FakeRedis plus the two methods the client lifecycle itself calls."""

    def __init__(self, *, ping_error=None, read_error=None, **kw):
        super().__init__(**kw)
        self.ping_calls = 0
        self.close_calls = 0
        self._ping_error, self._read_error = ping_error, read_error

    async def ping(self):
        self.ping_calls += 1
        if self._ping_error is not None:
            raise self._ping_error
        return True

    async def aclose(self):
        self.close_calls += 1

    async def hgetall(self, key):
        if self._read_error is not None:
            raise self._read_error
        return await super().hgetall(key)


@pytest.fixture
def wire_production_redis(monkeypatch):
    """Wire the production lifecycle. `from_url` is the ONLY thing stubbed."""
    import redis_client as rc

    def _wire(client):
        monkeypatch.setattr(rc, "_redis", None)
        monkeypatch.setattr(rc.aioredis, "from_url", lambda url, **kw: client)
        return rc

    return _wire


def _past_hour(back=3):
    return (datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
            - timedelta(hours=back))


def _snapshot_argv(start, hours=1):
    return ["narasi_observability", "snapshot",
            "--from", start.strftime("%Y-%m-%dT%H:00:00Z"),
            "--to", (start + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:00:00Z")]


def test_the_cli_establishes_its_own_client_and_reaches_the_real_reader(
        wire_production_redis, capsys):
    import redis_client as rc
    client = LifecycleRedis()
    wire_production_redis(client)
    start = _past_hour()

    code = obs._main(_snapshot_argv(start))
    captured = capsys.readouterr()

    # The lifecycle ran for real, and exactly once each way. `ping` lives inside
    # `init_redis`'s body and `aclose` inside `close_redis`'s, so these count the real
    # functions rather than a wrapper's idea of them.
    assert client.ping_calls == 1
    assert client.close_calls == 1
    # THE binding: the reader's read landed on the very instance the constructor returned,
    # so the unpatched `_client()` is what resolved it — and on exactly the one enumerated
    # hour key, never KEYS or SCAN.
    assert client.log == [("hgetall", obs.agg_key(obs.hour_key(start)))]
    assert obs._client() is client

    assert code == 4                                 # empty window is not a success
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert obs.canonical_json(payload) == captured.out.strip()
    assert payload["coverage"] == "INSUFFICIENT_SAMPLE"
    assert payload["window"] == {"from": start.strftime("%Y-%m-%dT%H:00:00Z"),
                                 "to": (start + timedelta(hours=1)).strftime(
                                     "%Y-%m-%dT%H:00:00Z"),
                                 "hours_requested": 1, "hours_with_data": 0}
    # Absence stays UNKNOWN. A denominator of 0 here would read as "we measured none"
    # when the truth is "we measured nothing".
    assert all(v is None for v in payload["denominators"].values())
    assert rc.client() is client


def test_two_reads_of_one_window_are_byte_identical(wire_production_redis, capsys):
    """§7 requires running the CLI twice and comparing bytes, so the snapshot must carry
    no clock, no ordering nondeterminism and no run-scoped value."""
    client = LifecycleRedis()
    wire_production_redis(client)
    argv = _snapshot_argv(_past_hour())

    first_code = obs._main(argv)
    first = capsys.readouterr().out
    second_code = obs._main(argv)
    second = capsys.readouterr().out

    assert first == second
    assert (first_code, second_code) == (4, 4)
    # Two runs, two full lifecycles — the CLI does not leak a client between invocations.
    assert client.ping_calls == 2
    assert client.close_calls == 2


def test_the_cli_closes_its_client_even_when_the_read_fails(wire_production_redis, capsys):
    secret = "AUTH failed for rediss://user:pa55@prod-redis:6379"
    client = LifecycleRedis(read_error=RuntimeError(secret))
    wire_production_redis(client)

    code = obs._main(_snapshot_argv(_past_hour()))
    captured = capsys.readouterr()

    assert code == 3
    assert captured.out == ""
    assert captured.err.strip() == "P0A_SNAPSHOT_FAILED"
    for leak in ("pa55", "rediss://", "prod-redis", "AUTH failed"):
        assert leak not in captured.err
    assert client.ping_calls == 1
    # The close is in a `finally`, so it happens on the failure path too — otherwise the
    # run an operator repeats after an error is the run that leaves a socket behind.
    assert client.close_calls == 1


def test_a_failing_client_construction_is_a_static_code_and_still_closes(monkeypatch,
                                                                        capsys):
    """The one way `init_redis()` itself raises: the constructor rejects the URL. The
    exception text quotes that URL, so it must not reach stderr either."""
    import redis_client as rc
    secret = "invalid connection string rediss://user:pa55@prod-redis:6379"
    monkeypatch.setattr(rc, "_redis", None)

    def _boom(url, **kw):
        raise ValueError(secret)

    monkeypatch.setattr(rc.aioredis, "from_url", _boom)
    closes = []
    real_close = rc.close_redis

    async def _counted_close():
        closes.append(1)
        await real_close()

    monkeypatch.setattr(rc, "close_redis", _counted_close)

    code = obs._main(_snapshot_argv(_past_hour()))
    captured = capsys.readouterr()

    assert code == 3
    assert captured.out == ""
    assert captured.err.strip() == "P0A_SNAPSHOT_FAILED"
    for leak in ("pa55", "rediss://", "prod-redis", "invalid connection string"):
        assert leak not in captured.err
    assert closes == [1]                             # once, even though init raised
    assert rc.client() is None


def test_a_ping_failure_never_reaches_stderr_and_the_logger_is_restored(
        wire_production_redis, capsys):
    """`init_redis()` SWALLOWS a ping failure and logs the raw exception. With no handler
    configured, logging's last resort writes WARNING+ straight to stderr — which is where
    this CLI publishes its closed code set. None of that prose may get out."""
    import logging

    class Recorder(logging.Handler):
        def __init__(self):
            super().__init__(level=0)
            self.records = []

        def emit(self, record):
            self.records.append(record)

    secret = "Error connecting to rediss://user:pa55@prod-redis:6379"
    client = LifecycleRedis(ping_error=RuntimeError(secret))
    wire_production_redis(client)

    rlog = logging.getLogger("redis_client")
    recorder = Recorder()
    rlog.addHandler(recorder)
    before_handlers, before_propagate = rlog.handlers[:], rlog.propagate
    try:
        code = obs._main(_snapshot_argv(_past_hour()))
        captured = capsys.readouterr()

        assert recorder.records == []
        assert captured.err == ""
        assert code == 4                             # the read still succeeds; only noise
        assert secret not in captured.out

        # The containment is given back, not left clamped on a shared logger.
        assert rlog.handlers == before_handlers
        assert rlog.propagate is before_propagate

        # ...and the recorder was genuinely live, so its emptiness above is evidence
        # rather than a vacuous pass on a handler that could never have fired.
        rlog.error("probe %s", "value")
        assert len(recorder.records) == 1
    finally:
        rlog.removeHandler(recorder)


def test_the_disabled_writer_path_never_even_imports_the_redis_client():
    """Process-level, because that is the only place "no connection" is a real claim.

    In-process a stub can always be made to report zero. What cannot be faked is that a
    disabled deployment finishes a record call without `redis_client` ever entering
    `sys.modules` — the lazy import lives behind the flag guard, so its absence afterwards
    means no client was resolved and no socket could have been opened.
    """
    import subprocess
    code = ("import sys; sys.path.insert(0, %r)\n"
            "import asyncio, narasi_observability as obs\n"
            "print('IMPORTED', 'redis_client' in sys.modules, 'redis' in sys.modules)\n"
            "r = asyncio.run(obs.record_job_start(route='bullmq_worker', chapter_count=3,\n"
            "                                     total_words=1000, job_id='j-1'))\n"
            "print('RECORD', r)\n"
            "print('AFTER', 'redis_client' in sys.modules, 'redis' in sys.modules)\n"
            % os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))), "python"))
    env = dict(os.environ)
    env.pop(obs.FLAG, None)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=120, env=env)
    assert "IMPORTED False False" in out.stdout, out.stdout + out.stderr
    assert "RECORD False" in out.stdout, out.stdout + out.stderr
    assert "AFTER False False" in out.stdout, out.stdout + out.stderr


def test_importing_the_module_opens_no_socket_and_pulls_in_no_redis_library():
    """Adding a client lifecycle to the CLI must not drag the redis library up to import
    time. The import lives inside `_cli_snapshot`, exactly as it does inside `_client`, so
    a process that only imports this module still constructs no socket at all."""
    import subprocess
    code = ("import sys, socket; sys.path.insert(0, %r)\n"
            "_real = socket.socket\n"
            "def _poisoned(*a, **k):\n"
            "    raise AssertionError('P0A_IMPORT_OPENED_A_SOCKET')\n"
            "socket.socket = _poisoned\n"
            "import narasi_observability as obs\n"
            "socket.socket = _real\n"
            "print('MODS', 'redis_client' in sys.modules, 'redis' in sys.modules)\n"
            "print('CLI', callable(obs._cli_snapshot))\n"
            % os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))), "python"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=120)
    assert "MODS False False" in out.stdout, out.stdout + out.stderr
    assert "CLI True" in out.stdout, out.stdout + out.stderr


# ===========================================================================
# §10.2 / §10.3 — the REAL narration call path
#
# Everything above this line exercises the metrics module. That proves the module, not
# the wiring: a hook aimed at the wrong branch, or a size helper reading keys the request
# never carries, passes every module test while measuring nothing. These tests call the
# production functions themselves — narration_start, _run_narration_job, _UsageSink and
# _apply_v3_gates — with their own dependencies stubbed, and no network at all.
# ===========================================================================
import types  # noqa: E402

import narration_api as na  # noqa: E402
from fastapi import BackgroundTasks  # noqa: E402
from orchestrator.core import CallTelemetry  # noqa: E402


async def _anoop(*a, **k):
    return None


def _telemetry(task_id="ch1", provider="kie", model="claude-opus-4-6", ok=True):
    return CallTelemetry(model=model, role="worker", ok=ok, tokens_in=10, tokens_out=20,
                         cost_usd=0.0, latency_ms=300, attempts=1, finish_reason="stop",
                         error=None, task_id=task_id, provider=provider)


class _FakeQueue:
    """Stands in for bullmq.Queue. `add` and `close` fail independently, because the
    production contract treats them differently: a close() failure after a successful add
    must stay `bullmq_worker`."""
    add_fails = False
    close_fails = False
    events: list = []

    def __init__(self, name, opts):
        _FakeQueue.events.append(("ctor", name))

    async def add(self, name, data, opts):
        _FakeQueue.events.append(("add", data.get("job_id")))
        if _FakeQueue.add_fails:
            raise RuntimeError("queue unavailable")

    async def close(self):
        _FakeQueue.events.append(("close", None))
        if _FakeQueue.close_fails:
            raise RuntimeError("close failed")


class _User:
    tenant_id = "t-1"
    user_id = "u-1"


@pytest.fixture
def route_env(monkeypatch):
    """narration_start with its I/O removed: no DB, no metering, no queue, no network."""
    _FakeQueue.add_fails = False
    _FakeQueue.close_fails = False
    _FakeQueue.events = []
    monkeypatch.setitem(sys.modules, "bullmq", types.SimpleNamespace(Queue=_FakeQueue))
    monkeypatch.setattr(na, "_resolve_user_uuid", lambda *a, **k: _anoop())
    monkeypatch.setattr(na, "_living_person_guard", _anoop)
    monkeypatch.setattr(na, "_safe_progress", _anoop)
    monkeypatch.setattr(na, "_set_status", _anoop)
    monkeypatch.setattr(na, "_seed_checkboxes", _anoop, raising=False)
    monkeypatch.setattr(na, "metering", types.SimpleNamespace(begin_charge=_anoop))
    monkeypatch.setattr(na, "db", types.SimpleNamespace(
        count_active_narasi_jobs=_anoop, create_narasi_job=_anoop, log_usage=_anoop,
        checkpoint_narasi_meter=_anoop, get_known_bad_claims=_anoop,
        get_known_good_claims=_anoop))
    monkeypatch.setattr(na, "_run_narration_job", _anoop)
    return _FakeQueue


_BODY = {"chapters": [{"word_target": 1200}, {"words": 900}, {}],
         "style": "harari", "language": "id", "topic": "x"}


def _setup_bullmq(monkeypatch, bullmq, add_fails=False):
    if bullmq is None:
        monkeypatch.delenv("NARRATION_BULLMQ_ENABLED", raising=False)
    else:
        monkeypatch.setenv("NARRATION_BULLMQ_ENABLED", bullmq)
    _FakeQueue.add_fails = add_fails


def _http_start(body=None):
    """Returns (response, background) so a test can inspect what the framework is holding
    for after the response — and then run it explicitly."""
    background = BackgroundTasks()
    out = asyncio.run(na.narration_start(dict(body or _BODY), background, user=_User()))
    return out, background


@pytest.mark.parametrize("bullmq,add_fails,queued,route", [
    ("1", False, True, "bullmq_worker"),     # enqueued: that IS the dispatch outcome
    ("1", True, False, "api_fallback"),      # add failed, fell through in-process
    (None, False, False, "api_direct"),      # BullMQ off, ran in-process
])
def test_the_dispatch_event_is_deferred_to_the_framework(fake, route_env, monkeypatch,
                                                         bullmq, add_fails, queued, route):
    """Not one Redis operation may stand between a caller and its 202 — and `route` must
    still mean DISPATCH, so it is recorded here and handed to the framework to run after
    the response, rather than carried into the job and re-read as execution."""
    _setup_bullmq(monkeypatch, bullmq, add_fails)
    out, background = _http_start()

    assert out["ok"] is True
    assert bool(out.get("queued")) is queued
    assert fake.log == [] and fake.store == {}          # nothing at all before the 202
    assert len(background.tasks) == 1                   # queued, not executed

    asyncio.run(background())                           # what Starlette does next
    bucket = _bucket(fake)
    assert bucket["route:%s" % route] == 1
    assert bucket["starts:total"] == 1
    assert len([f for f in bucket if f.startswith("route:")]) == 1
    # Dispatch only: the execution counter belongs to the job and stays separate.
    assert "executions:total" not in bucket


def test_an_enqueued_job_that_never_runs_still_counts_as_bullmq_worker(fake, route_env,
                                                                       monkeypatch):
    """Exactly why the dispatch event cannot live in the job: this one is enqueued and no
    worker ever picks it up, and `bullmq_worker` must still be its route."""
    _setup_bullmq(monkeypatch, "1")
    _, background = _http_start()
    asyncio.run(background())
    bucket = _bucket(fake)
    assert bucket["route:bullmq_worker"] == 1
    assert bucket["starts:total"] == 1
    assert "executions:total" not in bucket             # nothing ever executed it


def test_the_dispatch_event_creates_no_detached_asyncio_task(fake, route_env, monkeypatch):
    """Framework-owned, not fire-and-forget. The enqueued branch starts no job task at
    all, so any asyncio task appearing here would have to be ours."""
    _setup_bullmq(monkeypatch, "1")

    async def drive():
        background = BackgroundTasks()
        before = set(asyncio.all_tasks())
        await na.narration_start(dict(_BODY), background, user=_User())
        created = set(asyncio.all_tasks()) - before - {asyncio.current_task()}
        return len(background.tasks), created

    queued, created = asyncio.run(drive())
    assert queued == 1
    assert created == set()


def test_the_flag_off_path_queues_no_background_task(fake, route_env, monkeypatch):
    _setup_bullmq(monkeypatch, "1")
    monkeypatch.delenv(obs.FLAG, raising=False)
    out, background = _http_start()
    assert out["ok"] is True
    assert background.tasks == []                       # nothing added at all
    assert fake.log == [] and fake.store == {}


def test_the_dispatch_event_carries_the_server_resolved_chapter_and_size_buckets(
        fake, route_env, monkeypatch):
    # 1200 + 900 + (the server's own 800 default for a chapter naming no target).
    _setup_bullmq(monkeypatch, None)
    _, background = _http_start()
    asyncio.run(background())
    bucket = _bucket(fake)
    assert bucket["chapters:2_3"] == 1
    assert bucket["size:le_5k_words"] == 1
    assert "2900" not in json.dumps(fake.store)         # the exact total is never stored


def test_a_dispatch_with_no_chapter_targets_buckets_unknown_not_a_small_band(
        fake, route_env, monkeypatch):
    _setup_bullmq(monkeypatch, None)
    _, background = _http_start({"topic": "x", "n_chapters": 3, "style": "harari"})
    asyncio.run(background())
    assert _bucket(fake)["size:unknown"] == 1


def test_the_worker_passes_only_the_executor_label():
    """The worker names no route: reaching it is execution, not dispatch."""
    import ast
    import inspect
    import narration_worker

    source = inspect.getsource(narration_worker._process)
    call = next(node for node in ast.walk(ast.parse(source.strip()))
                if isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "_run_narration_job")
    passed = {kw.arg: getattr(kw.value, "value", None) for kw in call.keywords}
    assert passed["executor"] == "narration_worker"
    assert "route" not in passed


# ---------------------------------------------------------------------------
# The background job: executor label, terminals, gate and phase timing
# ---------------------------------------------------------------------------
@pytest.fixture
def job_env(monkeypatch):
    trace = {"status": [], "progress": [], "finalize": [], "settle": 0, "refund": 0,
             "tasks": [], "order": []}

    async def gen(req, **_):
        sink = req.get("telemetry_sink")
        for task_id in ("ch1", "ch1:cont1", "polish:novel"):
            trace["tasks"].append(task_id)
            sink(_telemetry(task_id))
        return {"ok": True, "book": "## Chapter 1\nteks", "chapters": [{"no": 1}]}

    async def cancel_watcher(job_id, poll=1.5):
        await asyncio.sleep(3600)

    async def finalize(job_id, job_uuid, tenant_id, *, status, result, error):
        trace["order"].append("finalize:%s" % status)
        trace["finalize"].append((status, json.dumps(result, sort_keys=True, default=str),
                                  error))

    async def set_status(job_id, status, *a, **k):
        trace["status"].append(status)

    async def progress(job_id, message, *a, **k):
        trace["progress"].append(message)

    async def settle(*a, **k):
        trace["order"].append("settle")
        trace["settle"] += 1

    async def refund(*a, **k):
        trace["order"].append("refund")
        trace["refund"] += 1

    async def persist(*a, **k):
        trace["order"].append("persist")

    async def gates(result, body, **kw):
        timings = kw.get("p0a_timings")
        if timings is not None:
            timings["critic"] = 1.25
            timings["revise"] = 62.0

    monkeypatch.setattr(na, "generate_narration", gen)
    monkeypatch.setattr(na, "_cancel_watcher", cancel_watcher)
    monkeypatch.setattr(na, "_finalize", finalize)
    monkeypatch.setattr(na, "_set_status", set_status)
    monkeypatch.setattr(na, "_safe_progress", progress)
    monkeypatch.setattr(na, "_settle", settle)
    monkeypatch.setattr(na, "_refund", refund)
    monkeypatch.setattr(na, "_apply_v3_gates", gates)
    monkeypatch.setattr(na, "_reconcile_checkboxes", _anoop)
    monkeypatch.setattr(na, "_persist_chapters", persist)
    monkeypatch.setattr(na, "credits_lib", types.SimpleNamespace(touch_hold=_anoop))
    monkeypatch.setattr(na, "db", types.SimpleNamespace(
        get_known_bad_claims=_anoop, get_known_good_claims=_anoop, log_usage=_anoop,
        checkpoint_narasi_meter=_anoop))
    return trace


def _run_job(job_id="j-1", executor="narration_worker", body=None, total=1, drain=True):
    """Drive the REAL background job and report what it left behind.

    `drain` awaits the fire-and-forget usage-row tasks the sink owns, which is required
    before asserting on provider metrics — and is deliberately switched OFF by the
    survivor test, where gathering every pending task would await the very thing being
    counted. Survivors are measured inside the job's own loop; sampling
    `asyncio.all_tasks()` on a freshly constructed loop, as an earlier version did, always
    reports an empty set and can never fail."""
    outcome = {}

    async def drive():
        before = set(asyncio.all_tasks())
        await na._run_narration_job(
            body=body if body is not None else {"chapters": [{"word_target": 500}]},
            job_id=job_id, job_uuid=None, tenant_id="t", user_id="u", total=total,
            meter_op=None, model="m", executor=executor,
            # L1.1 §9 parity: the parameter is deliberately required with no default, so
            # every caller must state a verdict rather than inherit one. None means "no
            # snapshot transported", which with the mode flag off is the exact legacy
            # path these P0A tests exercise — parity then does nothing and imports
            # nothing, so their assertions are unchanged.
            canon_parity=None, canon_route="api_direct",
            canon_model_route="m")
        survivors = (set(asyncio.all_tasks()) - before) - {asyncio.current_task()}
        outcome["survivors"] = survivors
        if drain and survivors:
            await asyncio.gather(*survivors, return_exceptions=True)

    asyncio.run(drive())
    return outcome


@pytest.mark.parametrize("executor,label", [
    ("narration_worker", "executor:narration_worker"),
    ("python_api", "executor:python_api"),
    ("", "executor:unknown"),
])
def test_the_executor_label_reaches_the_metric_from_the_real_job(fake, job_env, executor,
                                                                 label):
    _run_job(executor=executor)
    bucket = _bucket(fake)
    assert bucket[label] == 1
    assert bucket["executions:total"] == 1


def test_the_job_records_no_route_only_its_executor(fake, job_env):
    """Route is dispatch and belongs to the API; the job records execution. If the job
    also wrote a route, an enqueued-but-never-executed job would silently vanish from the
    route census the moment the two were merged."""
    _run_job(job_id="exec-only")
    bucket = _bucket(fake)
    assert bucket["executions:total"] == 1
    assert not [f for f in bucket if f.startswith("route:")]
    assert "starts:total" not in bucket


def test_the_real_job_records_gate_and_phase_timing_after_the_gate_phase(fake, job_env):
    _run_job()
    bucket = _bucket(fake)
    assert bucket["hist:gate_total_ms:count"] == 1
    assert bucket["hist:critic_phase_wall_ms:count"] == 1
    assert bucket["hist:critic_phase_wall_ms:sum"] == 1250          # 1.25 s, in whole ms
    assert bucket["hist:revise_phase_wall_ms:count"] == 1
    assert bucket["hist:revise_phase_wall_ms:sum"] == 62_000


def test_the_real_job_records_the_done_terminal_and_a_duration(fake, job_env):
    _run_job()
    bucket = _bucket(fake)
    assert bucket["terminal:done"] == 1
    assert bucket["terminals:total"] == 1
    assert bucket["hist:job_duration_ms:count"] == 1


def test_the_real_job_records_a_failed_terminal(fake, job_env, monkeypatch):
    async def gen(req, **_):
        return {"ok": False, "error": "generation_failed"}
    monkeypatch.setattr(na, "generate_narration", gen)
    _run_job()
    assert _bucket(fake)["terminal:failed"] == 1


def test_the_real_job_records_a_cancelled_terminal(fake, job_env, monkeypatch):
    async def gen(req, **_):
        await asyncio.sleep(3600)

    async def cancel_watcher(job_id, poll=1.5):
        return None                      # fires immediately: a cancel was requested
    monkeypatch.setattr(na, "generate_narration", gen)
    monkeypatch.setattr(na, "_cancel_watcher", cancel_watcher)
    _run_job()
    assert _bucket(fake)["terminal:cancelled"] == 1


def test_provider_calls_from_the_real_job_land_under_bounded_labels(fake, job_env):
    _run_job()
    bucket = _bucket(fake)
    # ch1 and ch1:cont1 are both `chapter`; polish:novel is `polish`.
    assert bucket["calls:kie:chapter"] == 2
    assert bucket["calls:kie:polish"] == 1
    assert bucket["logical_calls:total"] == 3
    assert bucket["outcome:ok"] == 3
    blob = json.dumps(fake.store)
    for raw in ("claude-opus-4-6", "ch1:cont1", "polish:novel"):
        assert raw not in blob
    # The raw role must not become a label component. Checked against the field names
    # themselves, not by substring: `narration_worker` is a legitimate executor enum
    # value that happens to contain "worker", and a blunt substring test flags it.
    fields = [f for key, hash_ in fake.store.items() if key.startswith(obs.NAMESPACE)
              and isinstance(hash_, dict) for f in hash_]
    assert not any(f == "worker" or f.endswith(":worker") for f in fields)


# ---------------------------------------------------------------------------
# The provider hook rides the task _log_one already owns
# ---------------------------------------------------------------------------
def test_the_sink_call_creates_no_second_task_for_the_metric(fake, monkeypatch):
    monkeypatch.setattr(na, "db", types.SimpleNamespace(
        log_usage=_anoop, checkpoint_narasi_meter=_anoop))

    async def drive():
        sink = na._UsageSink("t", "u", None)
        before = set(asyncio.all_tasks())
        sink(_telemetry("ch3"))
        created = set(asyncio.all_tasks()) - before
        # Exactly ONE: the usage-row task the sink already scheduled. The metric rides
        # that task instead of adding its own to the generation path.
        assert len(created) == 1
        await asyncio.gather(*created)

    asyncio.run(drive())
    assert _bucket(fake)["calls:kie:chapter"] == 1


def test_the_metric_is_independent_of_a_failing_usage_row(fake, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(na, "db", types.SimpleNamespace(
        log_usage=boom, checkpoint_narasi_meter=boom))

    async def drive():
        sink = na._UsageSink("t", "u", None)
        await sink._log_one(_telemetry("ch4", provider="laozhang"))

    asyncio.run(drive())
    assert _bucket(fake)["calls:laozhang:chapter"] == 1


class _Clock:
    """A monotonic clock a test can move forward."""

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now


def test_a_provider_call_long_after_the_first_is_still_recorded(fake, monkeypatch):
    """The sink must hold NO lifetime deadline.

    A `Budget` is a wall-clock deadline measured from its construction. Sharing one across
    the sink's life meant that on a book running for an hour, every provider call after
    the deadline was discarded even against a perfectly healthy Redis — the metric would
    stop measuring exactly the long jobs it exists to measure. Each write now carries its
    own small timeout instead."""
    monkeypatch.setattr(na, "db", types.SimpleNamespace(
        log_usage=_anoop, checkpoint_narasi_meter=_anoop))
    clock = _Clock()
    monkeypatch.setattr(obs, "time", clock)

    async def drive():
        sink = na._UsageSink("t", "u", None)
        await sink._log_one(_telemetry("ch1"))
        clock.now += 45 * 60                 # three quarters of an hour into the book
        await sink._log_one(_telemetry("ch2"))
        clock.now += 3600                    # and an hour past that
        await sink._log_one(_telemetry("ch3"))

    asyncio.run(drive())
    assert _bucket(fake)["logical_calls:total"] == 3
    assert _bucket(fake)["calls:kie:chapter"] == 3
    # And no lifetime deadline may quietly come back.
    assert not hasattr(obs, "SINK_BUDGET_MS")


def test_a_hung_redis_bounds_each_provider_write_and_leaves_no_task(monkeypatch):
    monkeypatch.setenv(obs.FLAG, "1")
    monkeypatch.setattr(obs, "_client", lambda: HangingRedis())
    monkeypatch.setattr(obs, "PROVIDER_WRITE_TIMEOUT_MS", 80)
    monkeypatch.setattr(na, "db", types.SimpleNamespace(
        log_usage=_anoop, checkpoint_narasi_meter=_anoop))

    async def drive():
        sink = na._UsageSink("t", "u", None)
        before = set(asyncio.all_tasks())
        started = time.monotonic()
        for index in range(3):
            await sink._log_one(_telemetry("ch%d" % index))
        elapsed = (time.monotonic() - started) * 1000.0
        survivors = (set(asyncio.all_tasks()) - before) - {asyncio.current_task()}
        return elapsed, survivors

    elapsed_ms, survivors = asyncio.run(drive())
    assert survivors == set()                       # nothing left pending on a dead client
    assert elapsed_ms < 3 * 80 + 400, elapsed_ms    # each write bounded on its own


def test_one_telemetry_with_three_attempts_is_one_logical_call(fake, monkeypatch):
    """The old test called the writer three times and asserted it counted three — which
    tests nothing about production. A run_worker retry does not produce three
    CallTelemetry objects: it produces ONE, carrying attempts=3."""
    monkeypatch.setattr(na, "db", types.SimpleNamespace(
        log_usage=_anoop, checkpoint_narasi_meter=_anoop))

    async def drive():
        sink = na._UsageSink("t", "u", None)
        telemetry = _telemetry("ch1")
        telemetry.attempts = 3
        await sink._log_one(telemetry)

    asyncio.run(drive())
    bucket = _bucket(fake)
    assert bucket["logical_calls:total"] == 1        # one telemetry, one logical call
    assert bucket["outer_attempts:total"] == 3       # three run_worker-level attempts
    assert bucket["calls:kie:chapter"] == 1
    assert "outer_attempts:unattributed" not in bucket


def test_a_telemetry_without_a_usable_attempt_count_is_marked_unattributed(fake,
                                                                          monkeypatch):
    monkeypatch.setattr(na, "db", types.SimpleNamespace(
        log_usage=_anoop, checkpoint_narasi_meter=_anoop))

    async def drive():
        sink = na._UsageSink("t", "u", None)
        telemetry = _telemetry("ch1")
        telemetry.attempts = 0
        await sink._log_one(telemetry)

    asyncio.run(drive())
    bucket = _bucket(fake)
    # Never assumed to be 1: an assumed attempt is an invented measurement.
    assert bucket["outer_attempts:unattributed"] == 1
    assert "outer_attempts:total" not in bucket


def test_physical_rung_attempts_are_never_claimed(fake, job_env):
    _run_job(job_id="phys")
    past = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    snap = asyncio.run(obs.read_snapshot(past - timedelta(hours=1), past))
    assert snap["denominators"]["physical_rung_attempts_total"] is None
    assert snap["instrumentation_scope"]["physical_rung_attempts_instrumented"] is False


# ---------------------------------------------------------------------------
# Nothing is written before persistence or billing, and a hung Redis is bounded
# ---------------------------------------------------------------------------
def _trace_terminal(monkeypatch, order):
    real = obs.record_terminal

    async def traced(**kw):
        order.append("telemetry:terminal")
        return await real(**kw)
    monkeypatch.setattr(obs, "record_terminal", traced)


def test_the_done_terminal_metric_is_written_after_persist_and_settle(fake, job_env,
                                                                      monkeypatch):
    _trace_terminal(monkeypatch, job_env["order"])
    _run_job(job_id="order-done")
    order = job_env["order"]
    assert (order.index("persist") < order.index("finalize:done")
            < order.index("settle") < order.index("telemetry:terminal"))


def test_the_failed_terminal_metric_is_written_after_the_refund(fake, job_env,
                                                                monkeypatch):
    async def gen(req, **_):
        return {"ok": False, "error": "generation_failed"}
    monkeypatch.setattr(na, "generate_narration", gen)
    _trace_terminal(monkeypatch, job_env["order"])
    _run_job(job_id="order-failed")
    order = job_env["order"]
    assert (order.index("finalize:failed") < order.index("refund")
            < order.index("telemetry:terminal"))


def test_the_cancelled_terminal_metric_is_written_after_the_refund(fake, job_env,
                                                                   monkeypatch):
    async def gen(req, **_):
        await asyncio.sleep(3600)

    async def cancel_watcher(job_id, poll=1.5):
        return None
    monkeypatch.setattr(na, "generate_narration", gen)
    monkeypatch.setattr(na, "_cancel_watcher", cancel_watcher)
    _trace_terminal(monkeypatch, job_env["order"])
    _run_job(job_id="order-cancelled")
    order = job_env["order"]
    assert (order.index("finalize:cancelled") < order.index("refund")
            < order.index("telemetry:terminal"))


class _HangingPipe:
    def hincrby(self, *a, **k):
        return self

    def expire(self, *a, **k):
        return self

    async def execute(self):
        await asyncio.sleep(3600)


class HangingRedis(FakeRedis):
    """Every Redis operation hangs forever. The enabled path must still finish the job,
    and finish it within the declared budget."""

    def pipeline(self, transaction=True):
        return _HangingPipe()

    async def set(self, *a, **k):
        await asyncio.sleep(3600)

    async def hgetall(self, key):
        await asyncio.sleep(3600)


def test_a_hung_redis_costs_the_enabled_job_only_its_bounded_budget(job_env, monkeypatch):
    monkeypatch.setenv(obs.FLAG, "1")
    monkeypatch.setattr(obs, "_client", lambda: HangingRedis())
    monkeypatch.setattr(obs, "TELEMETRY_BUDGET_MS", 150)
    monkeypatch.setattr(obs, "EARLY_WRITE_BUDGET_MS", 80)
    monkeypatch.setattr(obs, "PROVIDER_WRITE_TIMEOUT_MS", 80)

    started = time.monotonic()
    _run_job(job_id="hang-1", drain=False)
    elapsed_ms = (time.monotonic() - started) * 1000.0

    # The job completed: persisted, finalized and settled, in that order.
    assert job_env["settle"] == 1
    assert [s for s in job_env["order"] if s in ("persist", "finalize:done", "settle")] \
        == ["persist", "finalize:done", "settle"]
    # A totally unresponsive Redis costs the job one early allowance (80 ms) plus ONE
    # flush allowance (150 ms) — the first bounded await inside the flush consumes the
    # budget and every later one is skipped instantly. The threshold is set so that a
    # per-EVENT timeout cannot pass it: the flush issues four events, so per-event
    # budgeting would cost 80 + 4×150 = 680 ms. And nowhere near the 3600 s it hangs for.
    assert elapsed_ms < 500, elapsed_ms


def test_the_flag_off_path_leaves_no_task_behind(job_env, monkeypatch):
    """Measured in the job's OWN loop, and without gathering: a gather would await the
    very survivors under test. The pre-existing C10 child-task leak is neither fixed nor
    hidden here — it is simply required to be identical with the flag on and off."""
    r = FakeRedis()
    monkeypatch.setattr(obs, "_client", lambda: r)

    monkeypatch.delenv(obs.FLAG, raising=False)
    off = _run_job(job_id="surv-off", drain=False)

    async def gone(*a, **k):
        return None
    monkeypatch.setattr(na, "_p0a", gone)
    monkeypatch.setattr(na, "_p0a_on", lambda: False)
    monkeypatch.setattr(na, "_p0a_new_budget", lambda early=False: None)
    control = _run_job(job_id="surv-control", drain=False)
    assert len(off["survivors"]) == len(control["survivors"])


# ---------------------------------------------------------------------------
# The gate phase hands over its OWN timers — no await was added inside it
# ---------------------------------------------------------------------------
@pytest.fixture
def gate_env(monkeypatch):
    """The real _apply_v3_gates, with every provider call replaced. `make_narasi_client`
    records and raises, so any attempt at a real network call is both blocked and
    assertable."""
    import laozhang_api as lz
    attempts = []

    def blocked(*a, **k):
        attempts.append(k.get("phase") or "unknown")
        raise RuntimeError("network blocked in test")

    async def critique(*a, **k):
        return ({"violations": [{"type": "timeline", "severity": "high", "evidence": "e",
                                 "description": "d", "fix": "Correct the date.",
                                 "chapter": 1}]}, 0)

    async def revise(*a, **k):
        return ("REVISED", 0)

    async def cheap(*a, **k):
        return ("", 0)

    monkeypatch.setenv("NARASI_DIET_MAX_LOOPS", "0")
    monkeypatch.setattr(lz, "make_narasi_client", blocked)
    monkeypatch.setattr(lz, "_narasi_cheap_call", cheap)
    monkeypatch.setattr(lz, "_narasi_critique_enabled", lambda: True)
    monkeypatch.setattr(lz, "_narasi_critique_revise_enabled", lambda: True)
    monkeypatch.setattr(lz, "NARASI_CRITIQUE_MIN_CHAPTERS", 1)
    monkeypatch.setattr(lz, "_narasi_consistency_critique", critique)
    monkeypatch.setattr(lz, "_narasi_consistency_revise", revise)
    return attempts


def _gate_inputs():
    book = "\n".join("## Chapter %d\n%s" % (i, "kata " * 80) for i in range(1, 5))
    return ({"ok": True, "book": book, "chapters": [{"no": i} for i in range(1, 5)]},
            {"chapters": [{"word_target": 800}] * 4, "style": "harari", "language": "id"})


def test_the_real_gate_phase_hands_over_its_critic_and_revise_timers(gate_env):
    result, body = _gate_inputs()
    timings: dict = {}
    asyncio.run(na._apply_v3_gates(result, body, tenant_id="t", user_id="u",
                                   job_uuid=None, sink=None, job_id="j",
                                   p0a_timings=timings))
    assert sorted(timings) == ["critic", "revise"]
    assert all(isinstance(v, float) and v >= 0.0 for v in timings.values())
    assert gate_env == []                       # and not one provider client was built


def test_the_gate_phase_is_untouched_when_the_collector_is_absent(gate_env):
    result, body = _gate_inputs()
    before = json.dumps(result, sort_keys=True, default=str)
    asyncio.run(na._apply_v3_gates(result, body, tenant_id="t", user_id="u",
                                   job_uuid=None, sink=None, job_id="j",
                                   p0a_timings=None))
    control, body2 = _gate_inputs()
    asyncio.run(na._apply_v3_gates(control, body2, tenant_id="t", user_id="u",
                                   job_uuid=None, sink=None, job_id="j2"))
    assert json.dumps(result, sort_keys=True, default=str) == json.dumps(
        control, sort_keys=True, default=str)
    assert before != json.dumps(result, sort_keys=True, default=str)   # gates DID run


# ---------------------------------------------------------------------------
# §10.3 Flag-off equivalence, on the real job path
# ---------------------------------------------------------------------------
def _observable(trace):
    return json.dumps({k: trace[k] for k in ("status", "progress", "finalize", "settle",
                                             "refund", "tasks", "order")}, sort_keys=True)


@pytest.mark.parametrize("flag", [None, "0", "false", ""])
def test_flag_off_equivalence_on_the_real_job_path(job_env, monkeypatch, flag):
    r = FakeRedis()
    monkeypatch.setattr(obs, "_client", lambda: r)
    if flag is None:
        monkeypatch.delenv(obs.FLAG, raising=False)
    else:
        monkeypatch.setenv(obs.FLAG, flag)

    _run_job(job_id="equiv-off")
    off = _observable(job_env)

    # No Redis work of any kind — not an aggregate, not a dedupe claim, not a read.
    assert r.log == []
    assert r.store == {}

    # Now the control: the same job with the P0A hooks removed outright, which is what
    # the code looked like before this package existed.
    for key in ("status", "progress", "finalize", "tasks", "order"):
        job_env[key].clear()
    job_env["settle"] = job_env["refund"] = 0

    async def gone(*a, **k):
        return None
    monkeypatch.setattr(na, "_p0a", gone)
    monkeypatch.setattr(na, "_p0a_on", lambda: False)
    monkeypatch.setattr(na, "_p0a_new_budget", lambda early=False: None)
    _run_job(job_id="equiv-control")
    control = _observable(job_env)

    # status, progress, payload, settle/refund, call order — and the exact ordering of
    # persistence against billing. Surviving tasks are covered by their own test, which
    # does not gather them away first.
    assert off == control


def test_flag_on_changes_the_aggregates_and_nothing_else(job_env, fake, monkeypatch):
    _run_job(job_id="equiv-on")
    on = _observable(job_env)
    for key in ("status", "progress", "finalize", "tasks", "order"):
        job_env[key].clear()
    job_env["settle"] = job_env["refund"] = 0

    monkeypatch.delenv(obs.FLAG, raising=False)
    _run_job(job_id="equiv-on-2")
    off = _observable(job_env)
    # The one difference between a P0A-enabled run and a disabled one is the aggregate
    # hash. Everything the product does — and everything the user sees — is identical.
    assert on == off
    assert _bucket(fake)["executions:total"] == 1


def test_module_has_no_import_time_side_effect():
    import subprocess
    code = ("import sys; sys.path.insert(0, %r); "
            "import narasi_observability; "
            "print('MODS', 'redis_client' in sys.modules)"
            % os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))), "python"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=120)
    assert "MODS False" in out.stdout, out.stdout + out.stderr


# ---------------------------------------------------------------------------
# F5 — ledger-hit outline/bible exemption + cross-gate violation dedup before
# budgeting (BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md, sha256 fecd3dcb5f9b
# 85f81b70790736c5304a1dbcf29fad9780fb6c1dc904d4fc20b5, section F5).
# ---------------------------------------------------------------------------

def test_ledger_hit_is_exempt_when_term_is_in_the_topic():
    assert na._ledger_hit_is_exempt("Tae-jun", "a story about Tae-jun's return", "") is True


def test_ledger_hit_is_exempt_when_term_is_in_the_bible():
    assert na._ledger_hit_is_exempt(
        "Tae-jun", "a K-drama about a rooftop lease",
        "Kang Tae-jun is the protagonist, an architect.") is True


def test_ledger_hit_is_not_exempt_when_term_is_in_neither():
    assert na._ledger_hit_is_exempt(
        "Tae-jun", "a K-drama about a rooftop lease", "The protagonist is an architect.") is False


def test_ledger_hit_is_not_exempt_for_empty_term():
    assert na._ledger_hit_is_exempt("", "anything", "anything") is False


def test_ledger_hit_is_exempt_matches_numbers_cross_language_via_the_bible():
    # premise_term_in_topic's own cross-language number matcher (en "eleven" <-> a
    # bible stating "11") must fire through the bible argument too -- proves this
    # reuses the SAME matcher as the existing topic check, not a weaker one.
    assert na._ledger_hit_is_exempt("11", "a story", "Chapter eleven is the finale.") is True


def test_v3g_violation_chapter_reads_the_structured_key():
    assert na._v3g_violation_chapter({"chapter": 3, "evidence": "no tag here"}) == 3


def test_v3g_violation_chapter_reads_the_thread_tracker_at_ch_suffix():
    assert na._v3g_violation_chapter({"evidence": '"a quoted line" @ch5'}) == 5


def test_v3g_violation_chapter_returns_none_when_unresolvable():
    assert na._v3g_violation_chapter({"evidence": "no chapter signal at all"}) is None
    assert na._v3g_violation_chapter({}) is None


def test_v3g_dedup_violations_collapses_an_exact_cross_source_duplicate():
    critic_v = {"type": "timeline", "severity": "high",
                "evidence": '"the clock read noon" @ch2', "fix": "fix A"}
    thread_v = {"type": "timeline", "severity": "high",
                "evidence": '"the clock read noon" @ch2', "fix": "fix B (different wording)"}
    out = na._v3g_dedup_violations([critic_v, thread_v])
    assert out == [critic_v]                      # first occurrence wins


def test_v3g_dedup_violations_keeps_distinct_findings_in_the_same_chapter():
    a = {"type": "timeline", "severity": "high", "evidence": '"clock read noon" @ch2', "fix": "f"}
    b = {"type": "timeline", "severity": "high", "evidence": '"door was locked" @ch2', "fix": "f"}
    assert na._v3g_dedup_violations([a, b]) == [a, b]


def test_v3g_dedup_violations_never_folds_outline_missing_beat_into_unresolved_thread():
    # F5 guardrail: even with IDENTICAL chapter + evidence text, a specific
    # outline_missing_beat (routes to the structural-patch lane, see
    # laozhang_api._AUTHORITY_STRUCTURAL_VIOLATIONS) must never be replaced by /
    # collapsed into a generic unresolved_thread (does not route there) -- type is
    # part of the dedup key, so the two can never share one.
    beat = {"type": "outline_missing_beat", "severity": "high",
            "evidence": '"Stay?" he asked @ch3', "fix": "show the deposition"}
    thread = {"type": "unresolved_thread", "severity": "high",
              "evidence": '"Stay?" he asked @ch3', "fix": "resolve the thread"}
    out = na._v3g_dedup_violations([beat, thread])
    assert out == [beat, thread]                  # both survive, distinctly
    from laozhang_api import _is_authority_structural_violation as is_structural
    assert is_structural(out[0]) is True
    assert is_structural(out[1]) is False


def test_v3g_dedup_violations_treats_unresolvable_chapter_as_always_unique():
    a = {"type": "register", "severity": "high", "evidence": "required move not executed: x", "fix": "f"}
    b = {"type": "register", "severity": "high", "evidence": "required move not executed: x", "fix": "f"}
    # identical type+evidence, but NEITHER has a resolvable chapter -- the
    # conservative default keeps both rather than guessing they're the same finding.
    assert na._v3g_dedup_violations([a, b]) == [a, b]


def test_v3g_dedup_violations_passes_through_non_dict_entries_unchanged():
    assert na._v3g_dedup_violations(["not-a-dict", 42]) == ["not-a-dict", 42]


def _fake_ledger_scan(*_a, **_k):
    """Stand-in for narasi_counters.scan_manuscript: _apply_v3_gates runs the REAL
    scan (behind `if book and entry is not None`) and overwrites result["counter_report"]
    with its return value BEFORE the ledger-enforce block reads it -- pre-seeding
    result["counter_report"] in a test is clobbered. Stub the scan itself instead."""
    return {"over_budget": [], "counters": {"ledger_hits": {
        "status": "FLAG", "bible_hits": 0, "hits": [
            {"term": "name:Tae-jun", "where": "manuscript", "count": 3,
             "snippet": "Tae-jun said nothing."},
        ]}}}


def _ledger_enforce_env(monkeypatch, canonical_facts):
    import laozhang_api as lz
    import narasi_counters as nc_mod
    result, body = _gate_inputs()
    result["canonical_facts"] = canonical_facts
    monkeypatch.setenv("NARASI_LEDGER_ENFORCE", "1")
    monkeypatch.setattr(nc_mod, "scan_manuscript", _fake_ledger_scan)
    captured = {}

    async def critique(*a, **k):
        return ({"violations": []}, 0)

    async def revise(*a, **k):
        captured["violations"] = a[1].get("violations")
        return ("REVISED", 0)

    monkeypatch.setattr(lz, "_narasi_consistency_critique", critique)
    monkeypatch.setattr(lz, "_narasi_consistency_revise", revise)
    return result, body, captured


def test_ledger_enforce_injects_a_violation_when_the_term_is_not_bible_owned(gate_env, monkeypatch):
    result, body, captured = _ledger_enforce_env(
        monkeypatch, "The protagonist is an unnamed architect.")   # does NOT own the name

    asyncio.run(na._apply_v3_gates(result, body, tenant_id="t", user_id="u",
                                   job_uuid=None, sink=None, job_id="j"))
    assert [v["type"] for v in captured["violations"]] == ["ledger_hit"]
    assert "Tae-jun" in captured["violations"][0]["evidence"]


def test_ledger_enforce_exempts_a_bible_owned_name_end_to_end(gate_env, monkeypatch):
    result, body, captured = _ledger_enforce_env(
        monkeypatch, "Kang Tae-jun is the protagonist, an architect.")   # OWNS the name

    asyncio.run(na._apply_v3_gates(result, body, tenant_id="t", user_id="u",
                                   job_uuid=None, sink=None, job_id="j"))
    assert not captured.get("violations")     # revise never saw a Tae-jun rename instruction


def test_v3g_merge_dedups_before_reaching_revise(gate_env, monkeypatch):
    result, body = _gate_inputs()

    import laozhang_api as lz
    captured = {}

    async def critique(*a, **k):
        return ({"violations": [
            {"type": "timeline", "severity": "high",
             "evidence": '"the clock read noon" @ch2', "fix": "fix A"},
            {"type": "timeline", "severity": "high",
             "evidence": '"the clock read noon" @ch2', "fix": "fix A duplicate"},
            {"type": "timeline", "severity": "high",
             "evidence": '"the door was locked" @ch2', "fix": "fix B"},
        ]}, 0)

    async def revise(*a, **k):
        captured["violations"] = a[1].get("violations")
        return ("REVISED", 0)

    monkeypatch.setattr(lz, "_narasi_consistency_critique", critique)
    monkeypatch.setattr(lz, "_narasi_consistency_revise", revise)

    asyncio.run(na._apply_v3_gates(result, body, tenant_id="t", user_id="u",
                                   job_uuid=None, sink=None, job_id="j"))
    assert len(captured["violations"]) == 2
    assert captured["violations"][0]["fix"] == "fix A"          # first occurrence kept
    assert captured["violations"][1]["fix"] == "fix B"


# ---------------------------------------------------------------------------
# F6b — non-authoritative score: hard predicates decide GO/NO-GO, never the
# model's own score. Report-only, parallel to the main repair path -- does not
# gate delivery itself (F1's marker-leak block already does that, unchanged).
# (BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md, F6b, and the 2026-08-14 night
# expanded closed-loop brief's own F6b section.)
# ---------------------------------------------------------------------------

def test_go_no_go_is_go_with_no_signals_at_all():
    v = na._narasi_go_no_go_verdict(raw_model_score=9.0, violations=[], marker_leak_detected=False)
    assert v == {"verdict": "GO", "raw_model_score": 9.0,
                 "score_verdict_mismatch": False, "blockers": []}


def test_go_no_go_marker_leak_always_blocks_regardless_of_score():
    v = na._narasi_go_no_go_verdict(raw_model_score=10.0, violations=[], marker_leak_detected=True)
    assert v["verdict"] == "NO-GO"
    assert v["blockers"] == ["internal_marker_leak"]
    assert v["score_verdict_mismatch"] is True


@pytest.mark.parametrize("severity", ["high", "critical"])
def test_go_no_go_boundary_break_blocks_only_at_high_or_critical_severity(severity):
    v = na._narasi_go_no_go_verdict(
        raw_model_score=7.0,
        violations=[{"type": "chapter_boundary_break", "severity": severity, "evidence": "e"}],
        marker_leak_detected=False)
    assert v["verdict"] == "NO-GO"
    assert v["blockers"] == ["chapter_boundary_break"]


@pytest.mark.parametrize("severity", ["medium", "low"])
def test_go_no_go_boundary_break_does_not_block_below_high_severity(severity):
    v = na._narasi_go_no_go_verdict(
        raw_model_score=7.0,
        violations=[{"type": "chapter_boundary_break", "severity": severity, "evidence": "e"}],
        marker_leak_detected=False)
    assert v["verdict"] == "GO"
    assert v["blockers"] == []


@pytest.mark.parametrize("vtype", ["outline_missing_beat", "pov"])
@pytest.mark.parametrize("severity", ["critical", "high", "medium", "low"])
def test_go_no_go_missing_beat_and_pov_tense_block_at_any_severity(vtype, severity):
    # F6 case 3 (final beat) and case 1 (tense drift, reported as type "pov" per
    # check 6's combined POV/PERSON/TENSE wording) -- the brief names these two
    # WITHOUT a severity qualifier, unlike boundary_break's explicit "severity high".
    v = na._narasi_go_no_go_verdict(
        raw_model_score=7.0,
        violations=[{"type": vtype, "severity": severity, "evidence": "e"}],
        marker_leak_detected=False)
    assert v["verdict"] == "NO-GO"
    assert v["blockers"] == [vtype]


def test_go_no_go_ignores_a_violation_type_that_is_not_one_of_the_named_blockers():
    v = na._narasi_go_no_go_verdict(
        raw_model_score=7.0,
        violations=[{"type": "entity_drift", "severity": "critical", "evidence": "e"}],
        marker_leak_detected=False)
    assert v["verdict"] == "GO"
    assert v["blockers"] == []


def test_go_no_go_golden_high_score_plus_blocker_is_still_no_go_and_flags_mismatch():
    # The brief's own explicit golden test: payload with score=9.0 PLUS one
    # blocker must still yield NO-GO, and score_verdict_mismatch=true.
    v = na._narasi_go_no_go_verdict(
        raw_model_score=9.0,
        violations=[{"type": "outline_missing_beat", "severity": "high",
                     "evidence": '"Stay?" he asked, no reply.'}],
        marker_leak_detected=False)
    assert v["verdict"] == "NO-GO"
    assert v["score_verdict_mismatch"] is True


def test_go_no_go_mismatch_requires_score_nine_or_above():
    v = na._narasi_go_no_go_verdict(
        raw_model_score=8.9,
        violations=[{"type": "outline_missing_beat", "severity": "high", "evidence": "e"}],
        marker_leak_detected=False)
    assert v["verdict"] == "NO-GO"
    assert v["score_verdict_mismatch"] is False, \
        "the brief scopes mismatch to a HIGH score contradicting a blocker, not any blocked case"


def test_go_no_go_low_score_with_a_real_blocker_is_not_a_mismatch():
    v = na._narasi_go_no_go_verdict(
        raw_model_score=2.0,
        violations=[{"type": "outline_missing_beat", "severity": "high", "evidence": "e"}],
        marker_leak_detected=False)
    assert v["verdict"] == "NO-GO"
    assert v["score_verdict_mismatch"] is False


def test_go_no_go_missing_or_non_numeric_score_never_claims_a_mismatch():
    for bad_score in (None, "9.0", float("nan")):
        v = na._narasi_go_no_go_verdict(
            raw_model_score=bad_score,
            violations=[{"type": "outline_missing_beat", "severity": "high", "evidence": "e"}],
            marker_leak_detected=False)
        assert v["verdict"] == "NO-GO"          # the blocker itself is still real
        assert v["score_verdict_mismatch"] is False, f"bad_score={bad_score!r}"


def test_go_no_go_deduplicates_and_sorts_blockers():
    v = na._narasi_go_no_go_verdict(
        raw_model_score=5.0,
        violations=[
            {"type": "pov", "severity": "high", "evidence": "e1"},
            {"type": "pov", "severity": "medium", "evidence": "e2"},
            {"type": "outline_missing_beat", "severity": "critical", "evidence": "e3"},
        ],
        marker_leak_detected=False)
    assert v["blockers"] == ["outline_missing_beat", "pov"]


def test_go_no_go_ignores_non_dict_violation_entries():
    v = na._narasi_go_no_go_verdict(
        raw_model_score=9.0, violations=[None, "not a dict", 42], marker_leak_detected=False)
    assert v["verdict"] == "GO"
