"""P0A — privacy-safe, aggregate-only narration observability.

Purpose (Canon Lite P0A): supply the aggregate dimensions P0 instruction I13 needs —
route, executor, job counts/statuses, chapter and requested-size buckets, durations,
provider calls/tokens/latency, and gate timing — WITHOUT any tenant, user, job, project,
session or request identifier, and without a single byte of prose.

Design constraints this module is written to (all from the P0A instructions):

* Flag ``NARASI_P0A_OBSERVABILITY_ENABLED`` defaults to ``0``. Disabled means no Redis
  write, no read, no background task, no new log, and no import-time side effect. The
  guard is a cheap function call; the Redis client is imported lazily, after the guard.
* Every label comes from a CLOSED enum. An unrecognised input becomes the literal
  ``other``/``unknown`` — never the raw value. Raw model names, ``CallTelemetry.provider``,
  ``task_id``, ``role``, ``finish_reason`` and ``error`` never leave the caller.
* Aggregates live in one versioned namespace, bucketed per UTC hour, TTL 35 days. The
  reader enumerates the exact hour keys for a bounded window (max 31 days) — never
  ``KEYS``/``SCAN``.
* Counters are integers only. Cost, if ever added, would be integer micro-USD; there is
  no floating-point Redis increment anywhere here.
* Exactly-once counting uses a dedupe marker derived as a one-way SHA-256 of the internal
  job id plus an operation name. The raw id is never stored, printed or returned, and
  dedupe keys are excluded from every snapshot.
* Telemetry is best-effort with respect to the product path: a metric failure must never
  fail, delay, cancel, retry, refund, settle or otherwise alter a narration job. It makes
  the EVIDENCE incomplete, never the behaviour different.

Absence is reported as ``UNKNOWN``/``INSUFFICIENT_SAMPLE`` — never as a fabricated zero.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

SCHEMA_VERSION = "narasi.p0a.observability.v1"
NAMESPACE = "narasi:p0a:v1"
FLAG = "NARASI_P0A_OBSERVABILITY_ENABLED"

_TTL_SECONDS = 35 * 24 * 3600          # 35 days, aggregates and dedupe alike
_MAX_WINDOW_HOURS = 31 * 24            # reader refuses anything longer

# ---------------------------------------------------------------------------
# Telemetry budget
# ---------------------------------------------------------------------------
# ONE wall-clock allowance per job for EVERY P0A await it makes — not a timeout per
# event. A per-event timeout multiplies: eight events × one second each is eight seconds
# of user-visible latency added by a metrics package. With a single budget, a hung Redis
# costs a job this much in total no matter how many events it would have written, and the
# events it can no longer afford are simply not written. Their absence is a coverage gap,
# which is the honest outcome; a delayed narration is not.
TELEMETRY_BUDGET_MS = 500
# The one write that must stay ahead of generation (see `record_job_start`) gets a tighter
# hard cap of its own, because it is the only telemetry that sits in front of user work.
EARLY_WRITE_BUDGET_MS = 250
# Provider-metric writes get a small HARD TIMEOUT EACH, not a shared lifetime deadline.
# A lifetime budget is a wall-clock deadline measured from the first call, so on a book
# that takes an hour every provider call after the deadline would be discarded even with
# a perfectly healthy Redis — the metric would quietly stop measuring exactly the long
# jobs it exists for. These writes ride a task the sink already owns and are off the
# critical path, so bounding each one individually costs the product nothing.
PROVIDER_WRITE_TIMEOUT_MS = 250


class Budget:
    """A monotonic deadline shared by every telemetry call on one job."""
    __slots__ = ("_deadline",)

    def __init__(self, total_ms: int = TELEMETRY_BUDGET_MS):
        self._deadline = time.monotonic() + max(0, int(total_ms)) / 1000.0

    def remaining_s(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def spent(self) -> bool:
        return self.remaining_s() <= 0.0

# ---------------------------------------------------------------------------
# Flag
# ---------------------------------------------------------------------------
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def enabled() -> bool:
    """Cheap guard. Only an explicit truthy token enables; unset/blank/anything else is
    disabled. Deliberately strict: a typo must fail CLOSED (telemetry off), never on."""
    raw = os.environ.get(FLAG)
    if type(raw) is not str:
        return False
    return raw.strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Closed enums — an unknown input never becomes a label
# ---------------------------------------------------------------------------
ROUTES = ("bullmq_worker", "api_direct", "api_fallback", "unknown")
EXECUTORS = ("narration_worker", "python_api", "unknown")
TERMINALS = ("done", "failed", "cancelled", "unknown")
PROVIDERS = ("kie", "laozhang", "atlascloud", "vertex", "other")
PHASES = ("chapter", "bible", "polish", "critic", "revise", "gate_total", "other")
CHAPTER_BUCKETS = ("1", "2_3", "4_5", "6_10", "11_20", "gt_20", "unknown")
SIZE_BUCKETS = ("le_5k_words", "5k_10k_words", "10k_20k_words",
                "20k_40k_words", "gt_40k_words", "unknown")


def _exact_str(v: Any) -> bool:
    return type(v) is str


def _exact_int(v: Any) -> bool:
    # bool is an int subclass; `type(...) is int` excludes it deliberately.
    return type(v) is int


def _from_enum(value: Any, allowed: tuple, fallback: str) -> str:
    """Exact-type gate BEFORE any membership test, so a hostile str subclass can never
    run __hash__/__eq__ here, and normalisation is lowercase-exact only."""
    if not _exact_str(value):
        return fallback
    candidate = value.strip().lower()
    return candidate if candidate in allowed else fallback


def normalize_route(value: Any) -> str:
    return _from_enum(value, ROUTES, "unknown")


def normalize_executor(value: Any) -> str:
    return _from_enum(value, EXECUTORS, "unknown")


def normalize_terminal(value: Any) -> str:
    return _from_enum(value, TERMINALS, "unknown")


def normalize_provider(value: Any) -> str:
    """`CallTelemetry.provider` is the serving aggregator when the narasi failover client
    handled the call, and empty when the plain client served. Empty maps to `other`, which
    is honest: the family is not observable, not absent."""
    return _from_enum(value, PROVIDERS, "other")


# Phase normalisation reads the REAL production `task_id` shapes, censused at the pinned
# SHA. The original task id NEVER leaves this function and no rule ever returns a
# substring of it. Every shape below is a literal `task_id=` site in the source:
#
#   ch{n} / ch{n}:cont{r}            -> chapter  static.py:488,490,492,538,541,543,1118,388
#   single                           -> chapter  router.py:406 (one-shot single chapter)
#   planner:bible                    -> bible    dynamic.py:2099
#   polish:{mode}[:chunk{i}/{n}]     -> polish   static.py:1567,1598,1607,1636
#   planner:outline, planner:outline-titlefix, planner:subtasks (dynamic.py:998,1029,252),
#   diet{loops} (narration_api.py:1543), dyn:{role}, dyn:synthesize (router.py:500,527),
#   cowork:{name}, cowork:synthesize (static.py:208,249), and the bare worker names
#   core.py:497/537 substitutes via `task_id or worker.name`
#                                    -> other
#
# `critic` and `revise` are deliberately unreachable from here. Those two calls do NOT go
# through `_narasi_cheap_call`: they build a client directly —
# `make_narasi_client(phase="critique")` in `_narasi_consistency_critique` and
# `make_narasi_client(phase="canon_diff_revise")` in `_narasi_consistency_revise` — and
# account through `_log_narasi_usage`, never through `_UsageSink`. So no CallTelemetry can
# carry them, and their PROVIDER latency stays UNKNOWN. What `record_phase_timing` records
# is phase WALL time from timers the gate phase already keeps; it is a different quantity
# and must never be presented as provider-call latency. (The 18 static `_narasi_cheap_call`
# sites are a separate, still-uninstrumented gap — not this one.)
_MAX_TASK_CHARS = 128
_ASCII_DIGITS = frozenset("0123456789")


def _all_ascii_digits(text: str) -> bool:
    """`str.isdigit()` is true for superscripts and Arabic-Indic digits; this is not."""
    return bool(text) and all(c in _ASCII_DIGITS for c in text)


def _is_chapter_task(low: str) -> bool:
    """`ch{digits}`, optionally continued as `ch{digits}:cont{digits}`. Hand-parsed rather
    than matched by regex so a hostile input can never drive catastrophic backtracking."""
    if not low.startswith("ch"):
        return False
    head, _, tail = low.partition(":")
    if not _all_ascii_digits(head[2:]):
        return False
    if not tail:
        return True
    return tail.startswith("cont") and _all_ascii_digits(tail[4:])


def normalize_phase(task_id: Any = None, role: Any = None) -> str:
    for source in (task_id, role):
        if not _exact_str(source):
            continue
        low = source.strip().lower()
        if not low or len(low) > _MAX_TASK_CHARS:
            continue
        if low in PHASES:
            return low
        if low == "single" or _is_chapter_task(low):
            return "chapter"
        if low == "planner:bible":
            return "bible"
        if low == "polish" or low.startswith("polish:"):
            return "polish"
    return "other"


def chapter_bucket(count: Any) -> str:
    """Boundary ownership, stated once and tested exhaustively: each bucket owns its own
    upper bound. 1 | 2-3 | 4-5 | 6-10 | 11-20 | >20."""
    if not _exact_int(count) or count < 1:
        return "unknown"
    if count == 1:
        return "1"
    if count <= 3:
        return "2_3"
    if count <= 5:
        return "4_5"
    if count <= 10:
        return "6_10"
    if count <= 20:
        return "11_20"
    return "gt_20"


def size_bucket(total_words: Any) -> str:
    """Derived ONLY from the server-resolved requested word target — never manuscript
    bytes and never stored prose. The exact requested count is never persisted.
    Boundaries: <=5000 | <=10000 | <=20000 | <=40000 | >40000.

    Zero is `unknown`, not `le_5k_words`: no real narration request asks for nothing, so a
    resolved total of 0 means the target could not be read. Bucketing it as the smallest
    real band would manufacture a measurement out of a missing one."""
    if not _exact_int(total_words) or total_words < 1:
        return "unknown"
    if total_words <= 5000:
        return "le_5k_words"
    if total_words <= 10000:
        return "5k_10k_words"
    if total_words <= 20000:
        return "10k_20k_words"
    if total_words <= 40000:
        return "20k_40k_words"
    return "gt_40k_words"


# ---------------------------------------------------------------------------
# Histograms — predeclared disjoint integer-millisecond bounds
# ---------------------------------------------------------------------------
# Bounds are chosen to cover the documented production envelope (a 40K-word job has been
# measured at ~66 minutes end to end, with a single merged revise call over 10 minutes)
# and are FROZEN: they must not be re-tuned after production data is seen.
JOB_DURATION_BOUNDS_MS = (
    30_000, 60_000, 120_000, 300_000, 600_000, 900_000, 1_800_000,
    2_700_000, 3_600_000, 5_400_000, 7_200_000,
)
PROVIDER_LATENCY_BOUNDS_MS = (
    250, 500, 1_000, 2_500, 5_000, 10_000, 20_000, 40_000, 80_000, 160_000, 320_000,
)
GATE_TOTAL_BOUNDS_MS = (
    10_000, 30_000, 60_000, 120_000, 300_000, 600_000, 900_000, 1_800_000, 3_600_000,
)
# Critic and revise are each ONE full-book call on the critique model. The measured
# envelope has a single merged revise pass running well past ten minutes on a 40K-word
# book, so the band runs to an hour before overflow. Frozen with the rest.
#
# The `_phase_wall_` in these names is load-bearing: this is the wall-clock span of the
# gate phase, measured by the caller's own monotonic timer. It is NOT provider-call
# latency — it includes prompt assembly, chunking and retries — and the surface must never
# let it stand in for the provider latency those phases do not report.
PHASE_TIMING_BOUNDS_MS = (
    5_000, 15_000, 30_000, 60_000, 120_000, 300_000,
    600_000, 1_200_000, 1_800_000, 3_600_000,
)

HISTOGRAMS = {
    "job_duration_ms": JOB_DURATION_BOUNDS_MS,
    "provider_latency_ms": PROVIDER_LATENCY_BOUNDS_MS,
    "gate_total_ms": GATE_TOTAL_BOUNDS_MS,
    "critic_phase_wall_ms": PHASE_TIMING_BOUNDS_MS,
    "revise_phase_wall_ms": PHASE_TIMING_BOUNDS_MS,
}
_OVERFLOW = "overflow"


def bucket_label(bounds: tuple, value: int) -> str:
    """The first bound the value does not exceed owns it; anything larger is `overflow`.
    Buckets are disjoint by construction."""
    for bound in bounds:
        if value <= bound:
            return "le_%d" % bound
    return _OVERFLOW


def _hist_labels(bounds: tuple) -> tuple:
    return tuple("le_%d" % b for b in bounds) + (_OVERFLOW,)


def quantile_from_histogram(bounds: tuple, counts: dict, q: float) -> Optional[int]:
    """Approximate quantile as the UPPER BOUND of the first bucket whose cumulative count
    reaches `q`. Returns None when there is no sample, and None for `overflow` — an
    unbounded bucket has no honest upper bound to report."""
    labels = _hist_labels(bounds)
    total = 0
    for label in labels:
        total += int(counts.get(label, 0) or 0)
    if total <= 0:
        return None
    target = q * total
    seen = 0
    for index, label in enumerate(labels):
        seen += int(counts.get(label, 0) or 0)
        if seen >= target:
            return bounds[index] if index < len(bounds) else None
    return None


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------
def hour_key(when: datetime) -> str:
    """`YYYYMMDDHH` in UTC. Naive datetimes are rejected rather than assumed local."""
    if when.tzinfo is None:
        raise ValueError("P0A_NAIVE_DATETIME")
    return when.astimezone(timezone.utc).strftime("%Y%m%d%H")


def agg_key(hour: str) -> str:
    return "%s:agg:%s" % (NAMESPACE, hour)


def dedupe_key(job_id: Any, operation: str) -> str:
    """One-way SHA-256 of the internal job id plus the operation. The raw id is never
    stored, printed or returned; this key is private and excluded from snapshots."""
    raw = "%s|%s" % ("" if job_id is None else str(job_id), operation)
    return "%s:dedupe:%s" % (NAMESPACE, hashlib.sha256(raw.encode("utf-8")).hexdigest())


def hours_between(start: datetime, end: datetime) -> list:
    """Every whole UTC hour key in [start, end). The caller has already validated the
    window; this only enumerates, so the reader never needs KEYS or SCAN."""
    cursor = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    stop = end.astimezone(timezone.utc)
    out = []
    while cursor < stop:
        out.append(hour_key(cursor))
        cursor += timedelta(hours=1)
    return out


# ---------------------------------------------------------------------------
# Writer — best-effort, never raises into the product path
# ---------------------------------------------------------------------------
def _client():
    """Lazy. Imported only AFTER the flag guard, so a disabled deployment performs no
    import-time work and holds no connection."""
    import redis_client as _rc
    return _rc.client()


def _bounded_int(value: Any, *, maximum: int) -> Optional[int]:
    """Exact int, non-negative, bounded. `bool` is excluded, floats/NaN/str are rejected
    rather than coerced — a coerced metric is a silently wrong metric."""
    if not _exact_int(value) or value < 0 or value > maximum:
        return None
    return value


_MAX_TOKENS = 100_000_000
_MAX_MS = 30 * 24 * 3600 * 1000
_MAX_ATTEMPTS = 1_000                  # run_worker retries, not physical HTTP attempts


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _bounded(coro, budget: Optional[Budget]):
    """Spend at most what is left of the job's ONE budget on this await."""
    if budget is None:
        return await coro
    remaining = budget.remaining_s()
    if remaining <= 0.0:
        coro.close()                       # never started; no 'never awaited' warning
        raise TimeoutError("P0A_BUDGET_SPENT")
    import asyncio
    return await asyncio.wait_for(coro, timeout=remaining)


async def _apply(fields: dict, *, when: Optional[datetime] = None,
                 budget: Optional[Budget] = None) -> bool:
    """Increment every field of ONE hour bucket and refresh its TTL atomically.

    Returns True on a completed write. Never raises: a telemetry failure must not fail,
    delay, cancel, retry, refund, settle or alter a narration job — it only makes the
    evidence incomplete.
    """
    try:
        client = _client()
        if client is None:
            await _writer_fail(budget)
            return False
        key = agg_key(hour_key(when or _now()))
        pipe = client.pipeline(transaction=True)
        for field, amount in fields.items():
            pipe.hincrby(key, field, amount)
        # Writer health rides in the SAME transaction as the event it describes: one
        # round-trip instead of two, and a success counter that cannot disagree with the
        # increments it is meant to vouch for.
        pipe.hincrby(key, "writer:ok", 1)
        pipe.expire(key, _TTL_SECONDS)
        await _bounded(pipe.execute(), budget)
        return True
    except Exception:  # noqa: BLE001 - telemetry must never escape into generation
        await _writer_fail(budget)
        return False


async def _writer_fail(budget: Optional[Budget] = None) -> None:
    """Record that a write was lost, so the reader can tell "no events" from "could not
    write". Best-effort, and bounded by the SAME budget: if Redis is the thing that is
    down, failing to record the failure is simply silent, and the missing hour shows up as
    a coverage gap. A failure counter is never worth extending a job for."""
    try:
        client = _client()
        if client is None:
            return
        key = agg_key(hour_key(_now()))
        pipe = client.pipeline(transaction=True)
        pipe.hincrby(key, "writer:fail", 1)
        pipe.expire(key, _TTL_SECONDS)
        await _bounded(pipe.execute(), budget)
    except Exception:  # noqa: BLE001
        return


async def _claim(job_id: Any, operation: str,
                 budget: Optional[Budget] = None) -> bool:
    """Exactly-once gate. True means THIS caller won the claim and should count.

    On any Redis error — or an exhausted budget — this returns False. It fails CLOSED, so
    a transient fault under-counts (visible as a coverage gap) rather than inflating a
    denominator.
    """
    try:
        client = _client()
        if client is None:
            return False
        return bool(await _bounded(client.set(dedupe_key(job_id, operation), "1",
                                              nx=True, ex=_TTL_SECONDS), budget))
    except Exception:  # noqa: BLE001
        return False


def _hist_fields(name: str, bounds: tuple, value: int) -> dict:
    return {
        "hist:%s:%s" % (name, bucket_label(bounds, value)): 1,
        "hist:%s:count" % name: 1,
        "hist:%s:sum" % name: value,
    }


async def record_job_start(*, route: Any, chapter_count: Any, total_words: Any,
                           job_id: Any, budget: Optional[Budget] = None) -> bool:
    """The DISPATCH event: which way the job was sent, and the two request buckets.

    Recorded once per job, after the dispatch outcome is known — an enqueue that
    succeeded counts as `bullmq_worker` whether or not the worker ever runs it, which is
    what makes the route a dispatch census rather than a lower bound on execution. A
    Queue.close() failure after a successful add is still `bullmq_worker`.

    Its denominator (`starts:total`) is deliberately NOT shared with the execution event:
    the gap between the two is exactly the population of jobs that were dispatched and
    never executed, and collapsing them into one counter destroys that signal.
    """
    if not enabled():
        return False
    if not await _claim(job_id, "start", budget):
        return False
    fields = {
        "route:%s" % normalize_route(route): 1,
        "chapters:%s" % chapter_bucket(chapter_count): 1,
        "size:%s" % size_bucket(total_words): 1,
        "starts:total": 1,
    }
    return await _apply(fields, budget=budget)


async def record_execution(*, executor: Any, job_id: Any,
                           budget: Optional[Budget] = None) -> bool:
    """The EXECUTION event: which service actually ran the job.

    One per real execution — a BullMQ redelivery or a duplicate-delivery suppression must
    not inflate this denominator, which is what the claim enforces."""
    if not enabled():
        return False
    if not await _claim(job_id, "exec", budget):
        return False
    return await _apply({"executor:%s" % normalize_executor(executor): 1,
                         "executions:total": 1}, budget=budget)


async def record_terminal(*, terminal: Any, duration_ms: Any, job_id: Any,
                          budget: Optional[Budget] = None) -> bool:
    """One bounded terminal status plus the total-duration observation, once per job.

    Flushed only AFTER the job has persisted its result and settled or refunded: a metric
    must never sit between a user's manuscript and their money.
    """
    if not enabled():
        return False
    if not await _claim(job_id, "terminal", budget):
        return False
    fields = {"terminal:%s" % normalize_terminal(terminal): 1, "terminals:total": 1}
    bounded = _bounded_int(duration_ms, maximum=_MAX_MS)
    if bounded is not None:
        fields.update(_hist_fields("job_duration_ms", JOB_DURATION_BOUNDS_MS, bounded))
    return await _apply(fields, budget=budget)


async def record_provider_call(*, provider: Any, phase: Any, ok: Any,
                               tokens_in: Any, tokens_out: Any,
                               latency_ms: Any, attempts: Any = None,
                               budget: Optional[Budget] = None) -> bool:
    """One LOGICAL provider call — exactly one `CallTelemetry` delivered to `_UsageSink`.

    THREE different call counts exist, and conflating any two of them overstates coverage:

      * `logical_calls` — one per CallTelemetry. What this counter is.
      * `outer_attempts` — `CallTelemetry.attempts`, the run_worker-level retries folded
        into that one telemetry. Summed separately; a logical call that carries no usable
        attempt count is recorded as unattributed rather than assumed to be 1.
      * physical per-rung HTTP attempts inside the failover client — NOT observable from
        here at all, and reported as UNKNOWN. Never inferred from either number above.

    Two further gaps stay outside this counter: the 18 static `_narasi_cheap_call` sites
    never reach `_UsageSink`, and neither do critic/revise, which build their client
    directly. That is why the whole provider surface is reported PARTIAL.

    Outcome lives under its own `outcome:` prefix so a provider/phase total is never
    contaminated by an ok/not_ok tally sharing its namespace. No dedupe here by design;
    only per-job events are exactly-once."""
    if not enabled():
        return False
    p = normalize_provider(provider)
    ph = normalize_phase(phase)
    fields = {"calls:%s:%s" % (p, ph): 1,
              "logical_calls:total": 1,
              "outcome:%s" % ("ok" if ok is True else "not_ok"): 1}
    att = _bounded_int(attempts, maximum=_MAX_ATTEMPTS)
    if att is not None and att >= 1:
        fields["outer_attempts:total"] = att
    else:
        fields["outer_attempts:unattributed"] = 1
    ti = _bounded_int(tokens_in, maximum=_MAX_TOKENS)
    to = _bounded_int(tokens_out, maximum=_MAX_TOKENS)
    if ti is not None:
        fields["tokens_in:%s:%s" % (p, ph)] = ti
    if to is not None:
        fields["tokens_out:%s:%s" % (p, ph)] = to
    lat = _bounded_int(latency_ms, maximum=_MAX_MS)
    if lat is not None:
        fields.update(_hist_fields("provider_latency_ms", PROVIDER_LATENCY_BOUNDS_MS, lat))
        fields["latency_sum:%s:%s" % (p, ph)] = lat
    return await _apply(fields, budget=budget)


async def record_gate_total(*, elapsed_ms: Any, job_id: Any,
                            budget: Optional[Budget] = None) -> bool:
    """Gate-phase wall clock, from the caller's existing monotonic timer. No new await is
    introduced inside the gates, no manuscript value is instrumented, and the write itself
    happens after the job has persisted and settled."""
    if not enabled():
        return False
    bounded = _bounded_int(elapsed_ms, maximum=_MAX_MS)
    if bounded is None:
        return False
    if not await _claim(job_id, "gate", budget):
        return False
    return await _apply(_hist_fields("gate_total_ms", GATE_TOTAL_BOUNDS_MS, bounded),
                        budget=budget)


TIMED_PHASES = ("critic", "revise")
_PHASE_WALL_HISTOGRAM = {"critic": "critic_phase_wall_ms",
                         "revise": "revise_phase_wall_ms"}


async def record_phase_timing(*, phase: Any, elapsed_ms: Any, job_id: Any,
                              budget: Optional[Budget] = None) -> bool:
    """Critic/revise PHASE WALL TIME, taken from the monotonic timers `_apply_v3_gates`
    already computes (`_t_crit`, `_v3g_t_rev`).

    This is not, and must never be reported as, provider-call latency. Neither call goes
    through `_narasi_cheap_call`: `_narasi_consistency_critique` builds its client with
    `make_narasi_client(phase="critique")` and `_narasi_consistency_revise` with
    `make_narasi_client(phase="canon_diff_revise")`, both accounting through
    `_log_narasi_usage` — so no `CallTelemetry` reaches `_UsageSink` and the provider
    latency of these two phases stays UNKNOWN. What is recorded here is the whole phase:
    prompt assembly, chunking and any retry included.

    Reusing the existing timers adds no new timer, no new await inside the gates and no
    provider call — the caller hands over an already-elapsed value once the gate phase has
    returned. Once per job per phase; a phase outside the closed timed set is refused."""
    if not enabled():
        return False
    ph = normalize_phase(phase)
    if ph not in TIMED_PHASES:
        return False
    bounded = _bounded_int(elapsed_ms, maximum=_MAX_MS)
    if bounded is None:
        return False
    if not await _claim(job_id, "phase:%s" % ph, budget):
        return False
    return await _apply(
        _hist_fields(_PHASE_WALL_HISTOGRAM[ph], PHASE_TIMING_BOUNDS_MS, bounded),
        budget=budget)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------
# AXIS 1 — instrumentation scope: a STATIC source fact, fixed at the pinned SHA and
# independent of how much traffic ran. It answers "what can this package see at all?".
# The call-site census is authoritative and is not re-derived at runtime: 13 literal-name
# call expressions + 5 alias-resolved calls (`_cfcall`, `_dpcall`, `_nlcall`, `_eacall`,
# `_xcall`) = 18 static `_narasi_cheap_call` sites; the 10 matching import lines are NOT
# call-sites, and the raw 23-line grep count is not a denominator — it is simply 13 + 10.
# The census is scoped to `narration_api.py`, and the scope is recorded here because "18
# static call-sites" with no file attached is the kind of number that later gets read as
# a whole-tree total. Runtime invocations, outer retries and physical rung attempts are
# separate dimensions and are never folded into it.
INSTRUMENTATION_SCOPE = {
    "cheap_call_census_scope": "narration_api.py",
    "cheap_call_sites_static_total": 18,
    "cheap_call_sites_literal_name": 13,
    "cheap_call_sites_alias_resolved": 5,
    "cheap_call_import_lines_not_call_sites": 10,
    "cheap_call_raw_grep_lines_not_a_denominator": 23,
    "cheap_call_sites_instrumented": 0,
    "usage_sink_logical_calls_instrumented": True,
    "outer_attempts_source": "CallTelemetry.attempts",
    "physical_rung_attempts_instrumented": False,
    # Critic and revise do NOT go through _narasi_cheap_call. Each builds a client
    # directly — make_narasi_client(phase="critique") and
    # make_narasi_client(phase="canon_diff_revise") — and accounts through
    # _log_narasi_usage, never through _UsageSink. Their PROVIDER latency is therefore
    # unobservable; what the surface carries is phase WALL time from existing timers.
    "critic_revise_provider_path": "direct_make_narasi_client_and_log_narasi_usage",
    "critic_revise_provider_latency": "UNKNOWN",
    "critic_revise_wall_time_source": "existing_monotonic_timers",
    # Three separate uninstrumented surfaces feed one conclusion: the provider call,
    # token and latency dimensions are PARTIAL, and §14 cannot promote P0 on them.
    "provider_surface_completeness": "PARTIAL",
    "provider_surface_gaps": ("cheap_call_sites", "critic_revise_direct_client",
                              "physical_rung_attempts"),
    "p0_promotion_blocked_by_provider_gap": True,
    # There is no heartbeat, so an hour with no hash cannot be told apart from an hour
    # with no traffic. Delivery is never claimed COMPLETE.
    "heartbeat_present": False,
    "delivery_complete_claimable": False,
}

# AXIS 2 — delivery coverage: a RUNTIME fact, per dimension, for the window actually read.
# There are only three honest answers. `COMPLETE` is not one of them: without a heartbeat
# an hour with no hash is indistinguishable from an hour with no traffic, so "we saw
# everything" is a claim this surface can never support.
_COVERAGE_UNKNOWN = "UNKNOWN"        # nothing observed for this dimension
_COVERAGE_OBSERVED = "OBSERVED"      # observed, and nothing known to be missing from it
_COVERAGE_PARTIAL = "PARTIAL"        # observed, but a KNOWN uninstrumented gap remains

_GROUP_DIMENSIONS = (
    ("route", "route"), ("executor", "executor"), ("terminal", "terminal"),
    ("chapter_bucket", "chapters"), ("size_bucket", "size"),
    ("provider_calls", "calls"), ("call_outcome", "outcome"),
    ("tokens_in", "tokens_in"), ("tokens_out", "tokens_out"),
    ("provider_latency_sum_ms", "latency_sum"),
)

# Every provider-derived dimension is PARTIAL by construction: the cheap-call sites, the
# critic/revise direct-client path and physical rung attempts are all uninstrumented.
_PARTIAL_DIMENSIONS = frozenset({
    "provider_calls", "call_outcome", "tokens_in", "tokens_out",
    "provider_latency_sum_ms", "provider_latency_ms",
})

_SERVICES = ("python", "narration_worker", "operator_cli", "unknown")
_HEXDIGITS = frozenset("0123456789abcdef")


def _is_hex(value: Any, length: int) -> bool:
    return (_exact_str(value) and len(value) == length
            and all(c in _HEXDIGITS for c in value.lower()))


def _expected_fields() -> frozenset:
    """Every field name this package can legitimately write, derived from the SAME closed
    enums the writers use.

    Anything else found in an hour hash is a bug, a schema drift or another writer in our
    namespace. The reader must fail closed on it rather than export it: a `group()` that
    forwards whatever it finds turns the aggregate hash into an unbounded label channel,
    and skipping the field quietly would hide the drift instead."""
    names = {"starts:total", "executions:total", "terminals:total", "logical_calls:total",
             "outer_attempts:total", "outer_attempts:unattributed",
             "outcome:ok", "outcome:not_ok", "writer:ok", "writer:fail"}
    for route in ROUTES:
        names.add("route:%s" % route)
    for executor in EXECUTORS:
        names.add("executor:%s" % executor)
    for terminal in TERMINALS:
        names.add("terminal:%s" % terminal)
    for bucket in CHAPTER_BUCKETS:
        names.add("chapters:%s" % bucket)
    for bucket in SIZE_BUCKETS:
        names.add("size:%s" % bucket)
    for provider in PROVIDERS:
        for phase in PHASES:
            names.add("calls:%s:%s" % (provider, phase))
            names.add("tokens_in:%s:%s" % (provider, phase))
            names.add("tokens_out:%s:%s" % (provider, phase))
            names.add("latency_sum:%s:%s" % (provider, phase))
    for name, bounds in HISTOGRAMS.items():
        names.add("hist:%s:count" % name)
        names.add("hist:%s:sum" % name)
        for label in _hist_labels(bounds):
            names.add("hist:%s:%s" % (name, label))
    return frozenset(names)


EXPECTED_FIELDS = _expected_fields()


def bounded_metadata(extra: Optional[dict] = None) -> dict:
    """CLOSED metadata keyset with shape-validated values, covering BOTH Python services.

    An unrecognised key is DROPPED rather than passed through, and every value must match
    its declared shape or becomes `UNKNOWN` — otherwise "metadata" is an unbounded label
    smuggled into the snapshot. The two peer fields exist because §9 requires the snapshot
    to establish two-service build/config parity; a digest and a 40-hex commit carry that
    without a secret, a URL or a free-form value."""
    src = extra if isinstance(extra, dict) else {}

    def service(key: str) -> str:
        value = src.get(key) or ""
        return value if (_exact_str(value) and value in _SERVICES) else "unknown"

    def sha(key: str, env: Optional[str] = None) -> str:
        value = src.get(key) or (os.environ.get(env) if env else None) or ""
        return value.lower() if _is_hex(value, 40) else "UNKNOWN"

    def digest(key: str) -> str:
        value = src.get(key) or ""
        return value.lower() if _is_hex(value, 64) else "UNKNOWN"

    out = {
        "schema_version": SCHEMA_VERSION,
        "namespace": NAMESPACE,
        "service": service("service"),
        "deploy_sha": sha("deploy_sha", "RAILWAY_GIT_COMMIT_SHA"),
        "config_digest": digest("config_digest"),
        "peer_service": service("peer_service"),
        "peer_deploy_sha": sha("peer_deploy_sha"),
        "peer_config_digest": digest("peer_config_digest"),
    }

    def parity(left: str, right: str) -> str:
        if left == "UNKNOWN" or right == "UNKNOWN":
            return "UNKNOWN"
        return "MATCH" if left == right else "MISMATCH"

    out["two_service_commit_parity"] = parity(out["deploy_sha"], out["peer_deploy_sha"])
    out["two_service_config_parity"] = parity(out["config_digest"],
                                              out["peer_config_digest"])
    return out


def _parse_utc(text: Any) -> datetime:
    """Parses only; never echoes. `datetime.fromisoformat` raises a ValueError whose text
    quotes the offending input, so it is caught and replaced by a static code — an operator
    error message must not become a channel for whatever was typed on the command line."""
    if not _exact_str(text):
        raise ValueError("P0A_WINDOW_NOT_UTC")
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:  # noqa: BLE001 - the exception text carries the raw input
        raise ValueError("P0A_WINDOW_NOT_UTC")
    if parsed.tzinfo is None:
        raise ValueError("P0A_WINDOW_NOT_UTC")
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("P0A_WINDOW_NOT_UTC")
    return parsed.astimezone(timezone.utc)


def validate_window(start: datetime, end: datetime, *, now: Optional[datetime] = None):
    """Reject non-UTC, inverted, future, partial-hour, or >31-day windows. The window must
    describe whole UTC hours: a partial hour cannot be distinguished from a missing one."""
    reference = now or _now()
    # UTC is checked on the OFFSET, not on tz-awareness, and on the reference too. A
    # caller passing 09:00+07:00 programmatically was silently accepted before: the CLI
    # parser rejected such offsets, but nothing stopped `read_snapshot` being called
    # directly with one, and the window then covered different hours than it printed.
    for moment in (start, end, reference):
        if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
            raise ValueError("P0A_WINDOW_NOT_UTC")
    if end <= start:
        raise ValueError("P0A_WINDOW_INVERTED")
    for moment in (start, end):
        if (moment.minute, moment.second, moment.microsecond) != (0, 0, 0):
            raise ValueError("P0A_WINDOW_PARTIAL_HOUR")
    if end > reference:
        raise ValueError("P0A_WINDOW_IN_FUTURE")
    if (end - start) > timedelta(hours=_MAX_WINDOW_HOURS):
        raise ValueError("P0A_WINDOW_TOO_LONG")


async def read_snapshot(start: datetime, end: datetime, *,
                        metadata: Optional[dict] = None) -> dict:
    """One canonical aggregate snapshot for an explicit whole-hour UTC window.

    Only the enumerated hour keys are read — never KEYS or SCAN. Hours with no hash are
    reported as missing rather than folded into a zero, so "we observed nothing" and "we
    could not observe" stay distinguishable.
    """
    validate_window(start, end)
    hours = hours_between(start, end)
    totals: dict = {}
    present = 0
    client = None
    try:
        client = _client()
    except Exception:  # noqa: BLE001
        client = None
    if client is None:
        raise RuntimeError("P0A_REDIS_UNAVAILABLE")

    for hour in hours:
        raw = await client.hgetall(agg_key(hour))
        if not raw:
            continue
        present += 1
        for field, value in raw.items():
            name = field.decode("utf-8") if isinstance(field, bytes) else str(field)
            # Fail CLOSED. An unexpected field or a malformed value is drift, and the one
            # thing that must not happen is exporting it or silently dropping it.
            if name not in EXPECTED_FIELDS:
                raise ValueError("P0A_SCHEMA_INVALID")
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                raise ValueError("P0A_SCHEMA_INVALID")
            if parsed < 0:
                raise ValueError("P0A_SCHEMA_INVALID")
            totals[name] = totals.get(name, 0) + parsed

    def group(prefix: str) -> dict:
        """Every field under one prefix, minus the `total` roll-up stored beside it: a
        denominator sharing a namespace with its own breakdown gets counted twice by any
        consumer that sums the group."""
        out = {}
        for name, value in totals.items():
            if not name.startswith(prefix + ":"):
                continue
            label = name[len(prefix) + 1:]
            if label == "total":
                continue
            out[label] = value
        return dict(sorted(out.items()))

    def denominator(field: str) -> Optional[int]:
        """A denominator exists only where THAT dimension was actually observed.

        Keyed on the field, not on "some hour had some hash": one route event used to make
        every provider denominator read 0, which says "we measured none" when the truth is
        "we measured nothing". Absence is UNKNOWN."""
        return totals[field] if field in totals else None

    def coverage_of(name: str, payload: Any) -> str:
        if not payload:
            return _COVERAGE_UNKNOWN
        return (_COVERAGE_PARTIAL if name in _PARTIAL_DIMENSIONS
                else _COVERAGE_OBSERVED)

    groups = {name: group(prefix) for name, prefix in _GROUP_DIMENSIONS}

    histograms: dict = {}
    quantiles: dict = {}
    for name, bounds in HISTOGRAMS.items():
        buckets = {}
        for label in _hist_labels(bounds):
            key = "hist:%s:%s" % (name, label)
            if key in totals:
                buckets[label] = totals[key]
        if not buckets:
            # The key is ALWAYS present. A closed schema means no consumer has to guess
            # whether a missing histogram meant "no data" or "no such metric".
            histograms[name] = None
            quantiles[name] = None
            continue
        histograms[name] = {
            "buckets": dict(sorted(buckets.items())),
            "count": totals.get("hist:%s:count" % name, 0),
            "sum": totals.get("hist:%s:sum" % name, 0),
            "bounds_ms": list(bounds),
        }
        quantiles[name] = {
            "p50": quantile_from_histogram(bounds, buckets, 0.50),
            "p95": quantile_from_histogram(bounds, buckets, 0.95),
        }

    # Hour-hash presence is NOT coverage. A hash proves a writer ran that hour; it proves
    # nothing about the events that never arrived, and with no heartbeat a silent hour and
    # a lost hour look identical. So the best this surface can say is UNVERIFIED.
    if present == 0:
        coverage = "INSUFFICIENT_SAMPLE"
    elif present < len(hours):
        coverage = "PARTIAL"
    else:
        coverage = "UNVERIFIED"

    delivery_coverage = {name: coverage_of(name, payload)
                         for name, payload in groups.items()}
    for name in HISTOGRAMS:
        delivery_coverage[name] = coverage_of(name, histograms[name])

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "namespace": NAMESPACE,
        "window": {"from": start.strftime("%Y-%m-%dT%H:00:00Z"),
                   "to": end.strftime("%Y-%m-%dT%H:00:00Z"),
                   "hours_requested": len(hours),
                   "hours_with_data": present},
        "coverage": coverage,
        "quantile_method": "fixed_bucket_upper_bound",
        # The two axes stay apart: what the package CAN see (static, source-derived) and
        # what it DID see (runtime, per dimension). Collapsing them is how a silent
        # instrumentation gap gets read as a quiet production week.
        "instrumentation_scope": dict(INSTRUMENTATION_SCOPE),
        "delivery_coverage": dict(sorted(delivery_coverage.items())),
        "denominators": {
            # Dispatch and execution keep separate denominators on purpose: their
            # difference is the population of jobs that were enqueued and never ran.
            "starts_total": denominator("starts:total"),
            "executions_total": denominator("executions:total"),
            "terminals_total": denominator("terminals:total"),
            # Three distinct call counts. `logical_calls` is one per CallTelemetry;
            # `outer_attempts` sums CallTelemetry.attempts; physical per-rung HTTP
            # attempts are not observable from inside the allowlist and stay None. They
            # must never be read as one another.
            "logical_calls_total": denominator("logical_calls:total"),
            "outer_attempts_total": denominator("outer_attempts:total"),
            "outer_attempts_unattributed": denominator("outer_attempts:unattributed"),
            "physical_rung_attempts_total": None,
        },
        "histograms": histograms,
        "quantiles_ms": quantiles,
        # Writer counters describe ATTEMPTED writes. `fail == 0` means no failure was
        # recorded, which is not evidence that anything was delivered — a writer that
        # never ran, or one whose failure write also failed, both look like zero.
        "writer_health": {"ok": denominator("writer:ok"),
                          "fail": denominator("writer:fail"),
                          "interpretation": "attempted_writes_only;"
                                            "fail_zero_is_not_liveness"},
        "metadata": bounded_metadata(metadata),
    }
    snapshot.update(groups)
    return snapshot


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Operator-only CLI — no HTTP endpoint anywhere in this package
# ---------------------------------------------------------------------------
# Diagnostics are drawn from a CLOSED code set. `str(exception)` is never written to
# stderr, not even for exceptions this module raises itself: the same `except` clause also
# catches exceptions from libraries below it, whose message text can quote the failing
# input, a Redis URL or a credential. An unrecognised failure degrades to a generic code.
_CLI_WINDOW_CODES = frozenset({
    "P0A_WINDOW_NOT_UTC", "P0A_WINDOW_INVERTED", "P0A_WINDOW_PARTIAL_HOUR",
    "P0A_WINDOW_IN_FUTURE", "P0A_WINDOW_TOO_LONG", "P0A_NAIVE_DATETIME",
})
_CLI_READ_CODES = frozenset({"P0A_REDIS_UNAVAILABLE", "P0A_SCHEMA_INVALID"})

# Fixed-schema metadata interface. Each flag maps to one closed metadata key with one
# declared shape; an operator typo is REJECTED with a static code rather than quietly
# becoming `UNKNOWN` in the evidence package. Nothing here accepts a free-form value.
_CLI_META_FLAGS = {
    "--service": ("service", "service"),
    "--deploy-sha": ("deploy_sha", "sha40"),
    "--config-digest": ("config_digest", "hex64"),
    "--peer-service": ("peer_service", "service"),
    "--peer-deploy-sha": ("peer_deploy_sha", "sha40"),
    "--peer-config-digest": ("peer_config_digest", "hex64"),
}


def _valid_meta(shape: str, value: str) -> bool:
    if shape == "service":
        return value in _SERVICES
    if shape == "sha40":
        return _is_hex(value, 40)
    return _is_hex(value, 64)


def _emit_code(exc: BaseException, allowed: frozenset, fallback: str) -> None:
    code = exc.args[0] if (exc.args and type(exc.args[0]) is str) else ""
    sys.stderr.write("%s\n" % (code if code in allowed else fallback))


def _main(argv: list) -> int:
    import asyncio
    if len(argv) < 2 or argv[1] != "snapshot":
        sys.stderr.write("P0A_USAGE\n")
        return 2
    args = {}
    meta = {"service": "operator_cli"}
    rest = argv[2:]
    while rest:
        flag = rest.pop(0)
        if flag in ("--from", "--to") and rest:
            args[flag] = rest.pop(0)
        elif flag in _CLI_META_FLAGS and rest:
            key, shape = _CLI_META_FLAGS[flag]
            value = rest.pop(0).strip().lower()
            if not _valid_meta(shape, value):
                sys.stderr.write("P0A_METADATA_INVALID\n")
                return 2
            meta[key] = value
        else:
            sys.stderr.write("P0A_USAGE\n")
            return 2
    if "--from" not in args or "--to" not in args:
        sys.stderr.write("P0A_USAGE\n")
        return 2
    try:
        start = _parse_utc(args["--from"])
        end = _parse_utc(args["--to"])
        validate_window(start, end)
    except Exception as exc:  # noqa: BLE001
        _emit_code(exc, _CLI_WINDOW_CODES, "P0A_WINDOW_INVALID")
        return 2
    try:
        snapshot = asyncio.run(read_snapshot(start, end, metadata=meta))
    except Exception as exc:  # noqa: BLE001 - never surface prose that may carry a secret
        _emit_code(exc, _CLI_READ_CODES, "P0A_SNAPSHOT_FAILED")
        return 3
    sys.stdout.write(canonical_json(snapshot) + "\n")
    return 0 if snapshot["coverage"] != "INSUFFICIENT_SAMPLE" else 4


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
