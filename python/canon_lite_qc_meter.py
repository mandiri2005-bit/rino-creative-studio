"""Accounting-safe physical-attempt meter for Canon Lite L2B.

This module is intentionally inert until a caller injects a provider. It does not import or
construct any production provider, and importing it performs no network, database, Redis or
configuration work. D-METER-23 therefore remains enforceable during dark deploy.

The meter is a decorator around canon_lite_extractor.ProviderCall. It writes an ``attempted``
row before each physical call, never retains request bytes or provider error text, and resolves
cost from Decimal rates frozen by PostgreSQL. A metering failure invalidates only the shadow
measurement: it marks the current job ineligible, arms the negative-only kill latch, increments
a bounded counter and returns control without exposing the underlying exception.

Reset ownership: Rino Yufahri, no delegation. The Redis latch has no TTL and application code
contains no clear operation. The operator procedure is represented by RESET_PROCEDURE and
RESET_ATTESTATION_FIELDS below; clearing remains a direct operator action, outside this module.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable, Mapping, Optional
from uuid import UUID


log = logging.getLogger("canon_lite_qc_meter")

PLATFORM_QC_TENANT_ID = UUID("670711f2-ecc9-5577-9bdf-cc77611d1b4b")

KILL_KEY = "l2b:meter_killed"
INFLIGHT_KEY = "l2b:inflight"
WRITE_FAIL_KEY = "qc_meter:write_fail"

PHASE_CATALOG_VERSION = "l2b-meter-phases-v1"
PHASE_CATALOG = ("canon_lite_l2_extract",)

_MAX_SCAN = 1024
_POLICY_MIN = 1
_POLICY_MAX = 10 ** 15
_ASCII_DIGITS = frozenset("0123456789")
_ASCII_NONZERO = frozenset("123456789")
_WRITE_FAIL_COUNTER_CAP = 65535
_CANCELLATION_COUNTER_CAP = 65535

DECLARED_INFLIGHT_CODES = frozenset({
    "inflight_zero",
    "inflight_positive",
    "inflight_negative",
    "inflight_absent",
    "inflight_unreadable",
    "inflight_malformed",
    "inflight_out_of_policy_range",
    "inflight_policy_invalid",
})

_FAILURE_CODES = frozenset({
    "begin_conflict",
    "begin_write_failed",
    "finish_write_failed",
    "inflight_counter_failed",
    "inflight_counter_invalid",
    "resolve_conflict",
    "resolve_missing",
    "resolve_write_failed",
    "usage_payload_invalid",
})

MAX_INFLIGHT_DERIVATION = (
    "replicas × concurrent jobs per worker × extractor concurrency; "
    "attempt cap is excluded because retries inside the extractor are serial"
)

RESET_ATTESTATION_FIELDS = (
    "queue_depth_zero",
    "no_live_executor_task_on_any_replica",
    "provider_accounting_has_no_open_request",
    "inflight_counter_repaired_by_operator",
)
RESET_ATTESTATION_NOTE_TEMPLATE = (
    "recorded_at_utc",
    "operator",
    *RESET_ATTESTATION_FIELDS,
)

RESET_PROCEDURE = (
    "Phase A: product resolver says L2B off; explicit readable l2b:inflight is zero; "
    "platform_qc_reap has run; attempted-row count is zero.",
    "If the counter is blocked or leaked, record all four operator attestations before repair.",
    "Phase B: investigate and reconcile every physical attempt; mismatch is a failure.",
    "Phase C: under the advisory lock insert clearing; delete the Redis key; read it back; "
    "insert cleared only after the cache is confirmed absent.",
    "Phase D: resume only after the authoritative read-back is false.",
)

_process_killed = False
_cancelled_attempts = 0


class MeterConfigurationError(RuntimeError):
    """A bounded configuration failure; the message is always a declared code."""


class MeteringBlocked(RuntimeError):
    """The shadow provider call was deliberately suppressed."""


class ReconciliationMismatch(RuntimeError):
    """Emitted physical attempts and durable rows disagree."""


@dataclass(frozen=True, slots=True)
class InflightVerdict:
    passes: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class PhaseAVerdict:
    passes: bool
    reason_code: str


def _policy_ok(value: Any) -> bool:
    # bool is an int subclass, so isinstance(value, int) is unsafe here.
    return type(value) is int and _POLICY_MIN <= value <= _POLICY_MAX


def _canonical_magnitude(value: str) -> bool:
    return bool(value) and value[0] in _ASCII_NONZERO and all(
        ch in _ASCII_DIGITS for ch in value)


def classify_inflight(
    raw: Any,
    *,
    unreadable: bool = False,
    max_inflight: int,
) -> InflightVerdict:
    """Total classifier for Phase A's Redis counter.

    Policy validation is deliberately the first branch. Only canonical ASCII ``"0"`` (or
    ``b"0"``) under a valid policy passes; absent, unreadable, malformed, negative, positive
    and out-of-policy values all block with distinct bounded codes. No ``isdigit`` or
    unbounded ``int`` conversion is used.
    """
    if not _policy_ok(max_inflight):
        return InflightVerdict(False, "inflight_policy_invalid")
    if unreadable:
        return InflightVerdict(False, "inflight_unreadable")
    if raw is None:
        return InflightVerdict(False, "inflight_absent")
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > _MAX_SCAN:
            return InflightVerdict(False, "inflight_malformed")
        try:
            raw = bytes(raw).decode("ascii")
        except UnicodeDecodeError:
            return InflightVerdict(False, "inflight_malformed")
    if not isinstance(raw, str) or len(raw) > _MAX_SCAN:
        return InflightVerdict(False, "inflight_malformed")
    if raw == "0":
        return InflightVerdict(True, "inflight_zero")
    if raw.startswith("-"):
        code = "inflight_negative" if _canonical_magnitude(raw[1:]) else "inflight_malformed"
        return InflightVerdict(False, code)
    if not _canonical_magnitude(raw):
        return InflightVerdict(False, "inflight_malformed")

    bound = str(max_inflight)
    if len(raw) > len(bound) or (len(raw) == len(bound) and raw > bound):
        return InflightVerdict(False, "inflight_out_of_policy_range")
    return InflightVerdict(False, "inflight_positive")


def load_max_inflight(environ: Optional[Mapping[str, str]] = None) -> int:
    """Load the required production policy without a default or shipped policy value."""
    raw = (os.environ if environ is None else environ).get("L2B_MAX_INFLIGHT")
    if not isinstance(raw, str) or not _canonical_magnitude(raw):
        raise MeterConfigurationError("inflight_policy_missing_or_invalid")
    ceiling = str(_POLICY_MAX)
    if len(raw) > len(ceiling) or (len(raw) == len(ceiling) and raw > ceiling):
        raise MeterConfigurationError("inflight_policy_missing_or_invalid")
    value = int(raw)  # bounded to at most 16 ASCII digits by the checks above
    if not _policy_ok(value):
        raise MeterConfigurationError("inflight_policy_missing_or_invalid")
    return value


# ===========================================================================
# DG-4 §1.1 — host sentinel. SERVER-SET, never payload-derived.
# ===========================================================================
#
# narration-worker is the SOLE metered host. The `python` service runs the same
# `_run_narration_job` via api_direct/api_fallback and has NO concurrency bound, so its
# term in `replicas × jobs-per-worker × extractor_concurrency` is undefined — a sum
# containing it would be undefined too. The role is therefore established by the worker's
# own boot code and can never be asserted by a job payload: a forgeable host claim would
# let any caller opt itself into metering.

HOST_ROLE_NARRATION_WORKER = "narration_worker"
_HOST_ROLES = frozenset({HOST_ROLE_NARRATION_WORKER})
_host_role: Optional[str] = None


def declare_host_role(role: str) -> None:
    """Establish this PROCESS's metered-host role. Called once, from worker boot only."""
    global _host_role
    if role not in _HOST_ROLES:
        raise MeterConfigurationError("host_role_invalid")
    if _host_role is not None and _host_role != role:
        # Re-declaring a different role would mean one process claiming two hosts; the
        # concurrency term is per-process, so that arithmetic could never be sound.
        raise MeterConfigurationError("host_role_conflict")
    _host_role = role


def metered_host_ok() -> bool:
    """True only in a process whose own boot code declared the metered host role."""
    return _host_role == HOST_ROLE_NARRATION_WORKER


def reset_host_role_for_tests() -> None:
    """Test-only. Production never clears an established role."""
    global _host_role
    _host_role = None


# ===========================================================================
# DG-4 — extractor concurrency. Required, no default, narration-worker only.
# ===========================================================================
#
# `max_concurrency` is a REQUIRED parameter of extract_all(); MAX_CONCURRENCY = 8 is a
# validation ceiling, not a value. Shipping a default here would invent the third factor
# of the max_inflight formula, so the loader refuses rather than guesses. Three distinct
# codes, because "missing", "malformed" and "out of range" are three different operator
# mistakes and collapsing them hides which one was made.

EXTRACTOR_CONCURRENCY_ENV = "CANON_LITE_EXTRACTOR_CONCURRENCY"


def load_extractor_concurrency(
    environ: Optional[Mapping[str, str]] = None,
    *,
    max_concurrency: int = 8,
) -> int:
    """Load the required per-process extractor concurrency. Worker-only, no default."""
    if not metered_host_ok():
        # The module ships to `python` too, where this variable is intentionally absent.
        # Evaluation order is mode -> host sentinel -> config, so reaching a config load
        # off-host means the caller skipped a gate.
        raise MeterConfigurationError("extractor_concurrency_host_not_permitted")
    raw = (os.environ if environ is None else environ).get(EXTRACTOR_CONCURRENCY_ENV)
    if raw is None or not isinstance(raw, str) or not raw.strip():
        raise MeterConfigurationError("extractor_concurrency_missing")
    if not _canonical_magnitude(raw):
        raise MeterConfigurationError("extractor_concurrency_invalid")
    if len(raw) > 3:
        raise MeterConfigurationError("extractor_concurrency_out_of_range")
    value = int(raw)
    if not (1 <= value <= max_concurrency):
        raise MeterConfigurationError("extractor_concurrency_out_of_range")
    return value


def phase_a_ready(
    *,
    l2b_enabled: bool,
    inflight_raw: Any,
    inflight_unreadable: bool,
    attempted_rows: int,
    max_inflight: int,
) -> PhaseAVerdict:
    """Evaluate Phase A from the product resolver's verdict and two drain signals."""
    inflight = classify_inflight(
        inflight_raw, unreadable=inflight_unreadable, max_inflight=max_inflight)
    if not inflight.passes:
        return PhaseAVerdict(False, inflight.reason_code)
    if type(l2b_enabled) is not bool or l2b_enabled:
        return PhaseAVerdict(False, "l2b_flag_not_off")
    if type(attempted_rows) is not int or attempted_rows < 0:
        return PhaseAVerdict(False, "attempted_count_invalid")
    if attempted_rows:
        return PhaseAVerdict(False, "attempted_rows_open")
    return PhaseAVerdict(True, "phase_a_ready")


# ── Assist ACTIVATION readiness ──────────────────────────────────────────────────
#
# 🔴 WHY THIS IS A SEPARATE GATE FROM phase_a_ready. phase_a_ready above answers "is it
#    safe to RESET / turn L2B off" — it demands `l2b_flag_not_off` and zero open attempted
#    rows. It is a drain gate, and it can never answer "is Assist actually able to run",
#    because a passing reset gate says the flag is OFF. Using it as an activation check
#    would be backwards.
#
#    The incident this closes (production job s0di2o1g, 2026-08-11): mode was `assist`, the
#    tenant WAS in the allowlist, canon injection armed and the census PASSed — and Assist
#    still checked nothing, because CANON_LITE_QC_PROVIDER_API_KEY was absent on
#    narration-worker. maybe_run_metered_wave got as far as its lazy provider import before
#    raising `qc_provider_api_key_missing`, and the terminal seam flattened that to
#    `l3_metered_wave_error` / `stage=no_session`. The job LOOKED like a successful canary:
#    outcome=unchecked, rounds=0, chapters_repaired=0, delivery_binding=None. A missing
#    credential must be legible BEFORE any outline or chapter money is spent, and must never
#    read as "clean".
#
# ⚠️ The provider module is deliberately NOT imported here — refusing to build the provider is
#    the point of the gate. Names and the ratified route come from canon_lite_qc_contract,
#    which the provider imports too, so there is ONE definition and drift is structurally
#    impossible rather than merely detectable by a test someone has to remember to run.
from canon_lite_qc_contract import (          # noqa: E402 — inert, no SDK/client/secret/I-O
    QC_API_KEY_ENV,
    QC_BASE_URL_ENV,
    QC_PROVIDER_BASE_URL,
)


def assist_activation_ready(
    *,
    tenant_id: Any,
    environ: Optional[Mapping[str, str]] = None,
) -> PhaseAVerdict:
    """Can Assist actually run for THIS tenant on THIS host, right now?

    Evaluated in a fixed order so the reason_code names the FIRST thing missing rather than
    an arbitrary one: mode -> tenant -> host -> extractor concurrency -> inflight policy ->
    provider key -> ratified route. Returns a bounded verdict and never raises.

    reason_code is always one of the fixed strings below — never a secret value, never an
    exception message, never provider text. Telemetry made of attacker- or provider-supplied
    prose is how a config failure turns into an unreadable blob.
    """
    env = os.environ if environ is None else environ

    # Steps 1-2 go through the TWO OFFICIAL RESOLVERS and nothing else.
    #
    # 🔴 An earlier version of this gate re-parsed NARASI_CANON_LITE_ASSIST_TENANTS by hand.
    #    That made it a THIRD implementation of a decision that already has exactly two
    #    sanctioned forms, which is the same class of defect the split between resolve_mode
    #    and resolve_effective_mode was created to end: the allowlist semantics could drift
    #    here without either resolver changing, and the drift would look like a tenant gate
    #    working. canon_lite.assist_tenants() is the only allowlist parser.
    import canon_lite

    # 1. Deployment CONFIGURATION — global, tenant-blind.
    if canon_lite.resolve_mode(env) != canon_lite.MODE_ASSIST:
        return PhaseAVerdict(False, "assist_not_armed")

    # 2. The DECISION for this job — global mode narrowed by the allowlist. Reaching here
    #    means the global mode is assist, so anything other than assist now is the tenant
    #    being excluded (an empty allowlist admits nobody, by design).
    if canon_lite.resolve_effective_mode(
            env, tenant_id=tenant_id) != canon_lite.MODE_ASSIST:
        return PhaseAVerdict(False, "tenant_not_allowed")

    # 3. Host sentinel. The module ships to the `python` service too, where the worker-only
    #    config is intentionally absent; a config load off-host means a skipped gate.
    if not metered_host_ok():
        return PhaseAVerdict(False, "host_not_permitted")

    # 4. Extractor concurrency — reuse the ratified loader so bounds stay in one place.
    try:
        load_extractor_concurrency(env)
    except MeterConfigurationError:
        return PhaseAVerdict(False, "extractor_concurrency_missing")

    # 5. Inflight policy must be present and canonical. Reuse load_max_inflight for the same
    #    reason as step 4: the bounds and the env name stay defined in exactly one place, so
    #    this gate cannot drift from the loader the wave itself uses.
    try:
        load_max_inflight(env)
    except MeterConfigurationError:
        return PhaseAVerdict(False, "inflight_policy_missing")

    # 6. The credential the incident was missing. Presence only — the VALUE is never read
    #    into a verdict, logged, or compared.
    if not str(env.get(QC_API_KEY_ENV) or "").strip():
        return PhaseAVerdict(False, "qc_provider_api_key_missing")

    # 7. Route must stay the ratified one. The override env is optional but, when set, must
    #    MATCH exactly — a silently repointed QC route bills a different endpoint under
    #    guarantees that were ratified for this one.
    override = env.get(QC_BASE_URL_ENV)
    if override is not None and override != QC_PROVIDER_BASE_URL:
        return PhaseAVerdict(False, "qc_provider_route_not_ratified")

    return PhaseAVerdict(True, "assist_activation_ready")


def phase_a_from_product_resolver(
    *,
    inflight_raw: Any,
    inflight_unreadable: bool,
    attempted_rows: int,
    max_inflight: int,
    environ: Optional[Mapping[str, str]] = None,
) -> PhaseAVerdict:
    """Operator Phase A entrypoint; the flag verdict always comes from Canon Lite's resolver."""
    import canon_lite

    effective_mode = canon_lite.resolve_mode(environ)
    return phase_a_ready(
        l2b_enabled=effective_mode != canon_lite.MODE_OFF,
        inflight_raw=inflight_raw,
        inflight_unreadable=inflight_unreadable,
        attempted_rows=attempted_rows,
        max_inflight=max_inflight,
    )


def validate_reset_attestation(note: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    missing = tuple(name for name in RESET_ATTESTATION_FIELDS if note.get(name) is not True)
    return not missing, missing


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    tokens_in: int
    tokens_out: int
    provider_reported_cost_usd: Optional[Decimal]

    def __post_init__(self) -> None:
        for name in ("tokens_in", "tokens_out"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 10_000_000:
                raise ValueError("usage_payload_invalid")
        reported = self.provider_reported_cost_usd
        if reported is not None and (
            not isinstance(reported, Decimal) or not Decimal("0") <= reported <= Decimal("100")
        ):
            raise ValueError("usage_payload_invalid")


@dataclass(frozen=True, slots=True)
class AttemptContext:
    run_id: str
    job_uuid: UUID
    job_external_id: Optional[str]
    phase: str
    provider: str
    model_upstream: str
    pricing_version: str
    rate_in_usd_per_m: Decimal
    rate_out_usd_per_m: Decimal
    attempt_timeout_s: Decimal

    def __post_init__(self) -> None:
        bounds = (
            ("run_id", self.run_id, 64),
            ("phase", self.phase, 64),
            ("provider", self.provider, 64),
            ("model_upstream", self.model_upstream, 128),
            ("pricing_version", self.pricing_version, 64),
        )
        for name, value, limit in bounds:
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise MeterConfigurationError(f"{name}_invalid")
        if self.phase not in PHASE_CATALOG:
            raise MeterConfigurationError("phase_not_in_catalog")
        if not isinstance(self.job_uuid, UUID):
            raise MeterConfigurationError("job_uuid_invalid")
        if self.job_external_id is not None and (
            not isinstance(self.job_external_id, str)
            or not self.job_external_id.strip()
            or len(self.job_external_id) > 64
        ):
            raise MeterConfigurationError("job_external_id_invalid")
        for name in ("rate_in_usd_per_m", "rate_out_usd_per_m"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not Decimal("0") <= value <= Decimal("10000"):
                raise MeterConfigurationError(f"{name}_invalid")
        if not isinstance(self.attempt_timeout_s, Decimal) or not (
            Decimal("0") < self.attempt_timeout_s <= Decimal("900")
        ):
            raise MeterConfigurationError("attempt_timeout_s_invalid")


class QcUsageSink:
    """Thin adapter over database.py's five granted SQL functions."""

    def __init__(self, database_module: Any = None) -> None:
        self._database_module = database_module

    def _db(self) -> Any:
        if self._database_module is None:
            import database
            self._database_module = database
        return self._database_module

    async def begin(self, context: AttemptContext, *, unit_index: int, attempt_ordinal: int) -> dict:
        return await self._db().platform_qc_begin_attempt(
            context.run_id, context.job_uuid, context.job_external_id,
            context.phase, unit_index, attempt_ordinal,
            context.provider, context.model_upstream, context.pricing_version,
            context.rate_in_usd_per_m, context.rate_out_usd_per_m,
            context.attempt_timeout_s)

    async def finish(self, attempt_id: str, state: str) -> dict:
        return await self._db().platform_qc_finish_attempt(attempt_id, state)

    async def resolve(self, attempt_id: str, usage: ProviderUsage) -> dict:
        return await self._db().platform_qc_resolve_cost(
            attempt_id, usage.tokens_in, usage.tokens_out,
            usage.provider_reported_cost_usd)

    async def arm(self, reason_code: str) -> dict:
        return await self._db().platform_qc_arm_kill(reason_code)

    async def is_armed(self) -> bool:
        return await self._db().platform_qc_kill_is_armed()


def _redis_client() -> Any:
    import redis_client
    return redis_client.client()


def _mark_ineligible() -> None:
    try:
        import canon_lite
        canon_lite.mark_job_canon_ineligible()
    except Exception:
        # Eligibility is a process-local safety signal. Its own failure must never leak a
        # dynamic exception or turn a shadow measurement into a customer-facing error.
        pass


def _bounded_log(code: str) -> None:
    log.error("platform_qc_meter code=%s", code)


async def _increment_bounded_counter(client: Any) -> None:
    if client is None:
        return
    try:
        value = await client.incr(WRITE_FAIL_KEY)
        if type(value) is int and value > _WRITE_FAIL_COUNTER_CAP:
            await client.set(WRITE_FAIL_KEY, str(_WRITE_FAIL_COUNTER_CAP))
    except Exception:
        pass


async def _write_cache_arm(client: Any) -> None:
    if client is None:
        return
    try:
        # Deliberately no ex=/px=/TTL argument.
        await client.set(KILL_KEY, "1")
    except Exception:
        pass


class MeteredProvider:
    """Provider decorator that records one row per physical extractor attempt."""

    def __init__(
        self,
        provider: Callable[[Any], Awaitable[Mapping[str, Any]]],
        *,
        sink: QcUsageSink,
        context: AttemptContext,
        usage_reader: Callable[[Mapping[str, Any]], ProviderUsage],
        max_inflight: int,
        redis_getter: Callable[[], Any] = _redis_client,
    ) -> None:
        if not callable(provider):
            raise MeterConfigurationError("provider_invalid")
        if not callable(usage_reader):
            raise MeterConfigurationError("usage_reader_invalid")
        if not _policy_ok(max_inflight):
            raise MeterConfigurationError("inflight_policy_invalid")
        self._provider = provider
        self._sink = sink
        self._context = context
        self._usage_reader = usage_reader
        self._max_inflight = max_inflight
        self._redis_getter = redis_getter
        self._emitted_attempts = 0

    def _redis(self) -> Any:
        try:
            return self._redis_getter()
        except Exception:
            return None

    @property
    def emitted_attempts(self) -> int:
        return self._emitted_attempts

    def assert_reconciled(self, recorded_attempts: int) -> None:
        if type(recorded_attempts) is not int or recorded_attempts != self._emitted_attempts:
            raise ReconciliationMismatch("attempt_count_mismatch")

    async def _arm(self, code: str, *, write_authority: bool = True) -> None:
        global _process_killed
        if code not in _FAILURE_CODES:
            code = "inflight_counter_invalid"
        _mark_ineligible()
        _process_killed = True
        client = self._redis()
        await _write_cache_arm(client)  # cache first
        if write_authority:
            try:
                await self._sink.arm(code)
            except Exception:
                pass
        await _increment_bounded_counter(client)
        _bounded_log(code)

    async def _killed(self) -> bool:
        if _process_killed:
            return True
        client = self._redis()
        if client is None:
            return True
        try:
            cached = await client.get(KILL_KEY)
        except Exception:
            return True
        if cached is not None:
            return True
        try:
            authoritative = bool(await self._sink.is_armed())
        except Exception:
            return True
        if authoritative:
            await _write_cache_arm(client)
        return authoritative

    async def _enter_inflight(self) -> Any:
        client = self._redis()
        if client is None:
            await self._arm("inflight_counter_failed")
            raise MeteringBlocked("inflight_counter_failed")
        try:
            value = await client.incr(INFLIGHT_KEY)
        except Exception:
            await self._arm("inflight_counter_failed")
            raise MeteringBlocked("inflight_counter_failed")
        if type(value) is not int or value < 1 or value > self._max_inflight:
            await self._arm("inflight_counter_invalid")
            try:
                await client.decr(INFLIGHT_KEY)
            except Exception:
                pass
            raise MeteringBlocked("inflight_counter_invalid")
        return client

    async def _leave_inflight(self, client: Any, *, cancelled: bool) -> None:
        try:
            value = await client.decr(INFLIGHT_KEY)
        except Exception:
            await self._arm("inflight_counter_failed", write_authority=not cancelled)
            return
        if type(value) is not int or value < 0:
            await self._arm("inflight_counter_invalid", write_authority=not cancelled)

    async def __call__(self, request: Any) -> Mapping[str, Any]:
        global _cancelled_attempts

        if await self._killed():
            _mark_ineligible()
            raise MeteringBlocked("meter_killed")

        unit_index = getattr(request, "chapter_index", None)
        attempt_ordinal = getattr(request, "attempt", None)
        if type(unit_index) is not int or type(attempt_ordinal) is not int:
            await self._arm("begin_write_failed")
            raise MeteringBlocked("begin_write_failed")

        inflight_client = await self._enter_inflight()
        cancelled = False
        try:
            try:
                begun = await self._sink.begin(
                    self._context, unit_index=unit_index,
                    attempt_ordinal=attempt_ordinal)
            except Exception:
                await self._arm("begin_write_failed")
                raise MeteringBlocked("begin_write_failed")

            attempt_id = begun.get("attempt_id") if isinstance(begun, Mapping) else None
            outcome = begun.get("outcome") if isinstance(begun, Mapping) else None
            if not attempt_id or outcome not in ("inserted", "replay", "conflict"):
                await self._arm("begin_write_failed")
                raise MeteringBlocked("begin_write_failed")
            if outcome == "conflict":
                await self._arm("begin_conflict")
                raise MeteringBlocked("begin_conflict")

            self._emitted_attempts += 1
            try:
                raw = await self._provider(request)
            except asyncio.CancelledError:
                cancelled = True
                _cancelled_attempts = min(
                    _cancelled_attempts + 1, _CANCELLATION_COUNTER_CAP)
                raise
            except Exception:
                try:
                    await self._sink.finish(str(attempt_id), "failed")
                except Exception:
                    await self._arm("finish_write_failed")
                raise

            try:
                finished = await self._sink.finish(str(attempt_id), "succeeded")
            except Exception:
                await self._arm("finish_write_failed")
                return raw

            # A reaper may have won. That is a bounded late finish, not permission to
            # overwrite timeout; cost still resolves independently on the same row.
            if not isinstance(finished, Mapping):
                await self._arm("finish_write_failed")
                return raw

            try:
                usage = self._usage_reader(raw)
                if not isinstance(usage, ProviderUsage):
                    raise ValueError("usage_payload_invalid")
            except Exception:
                await self._arm("usage_payload_invalid")
                return raw

            try:
                resolved = await self._sink.resolve(str(attempt_id), usage)
            except Exception:
                await self._arm("resolve_write_failed")
                return raw

            resolution = resolved.get("outcome") if isinstance(resolved, Mapping) else None
            if resolution == "conflict":
                await self._arm("resolve_conflict")
            elif resolution == "missing":
                await self._arm("resolve_missing")
            elif resolution not in ("applied", "already_same"):
                await self._arm("resolve_write_failed")
            return raw
        finally:
            await self._leave_inflight(inflight_client, cancelled=cancelled)


@dataclass(frozen=True, slots=True)
class ReaperResult:
    reaped: int
    cache_state: str
    authority_repaired: bool
    repair_skipped_reason: Optional[str]
    effective_armed: bool


async def _read_cache_state(client: Any) -> str:
    if client is None:
        return "unreadable"
    try:
        value = await client.get(KILL_KEY)
    except Exception:
        return "unreadable"
    return "absent" if value is None else "armed"


async def run_reaper_once(
    *,
    database_url: str,
    redis_client: Any,
    connector: Optional[Callable[..., Awaitable[Any]]] = None,
) -> ReaperResult:
    """Run sweep → Redis read → authority sync, as the dedicated reaper role."""
    if not database_url:
        raise MeterConfigurationError("reaper_database_url_missing")
    if connector is None:
        import asyncpg
        connector = asyncpg.connect

    connection = await connector(database_url)
    try:
        role = await connection.fetchval("SELECT current_user")
        if role != "platform_qc_reaper":
            raise MeterConfigurationError("reaper_role_invalid")

        # This order is structural: Redis cannot suppress the lifecycle sweep.
        reaped = int(await connection.fetchval(
            "SELECT public.platform_qc_reap()"))
        cache_state = await _read_cache_state(redis_client)
        row = await connection.fetchrow(
            """SELECT authority_repaired, repair_skipped_reason, effective_armed
                 FROM public.platform_qc_kill_sync($1)""",
            cache_state)
        return ReaperResult(
            reaped=reaped,
            cache_state=cache_state,
            authority_repaired=bool(row["authority_repaired"]),
            repair_skipped_reason=row["repair_skipped_reason"],
            effective_armed=bool(row["effective_armed"]),
        )
    finally:
        await connection.close()


async def _main_async() -> int:
    database_url = os.environ.get("PLATFORM_QC_REAPER_DATABASE_URL")
    if not database_url:
        raise MeterConfigurationError("reaper_database_url_missing")
    import redis_client as rc
    await rc.init_redis()
    try:
        result = await run_reaper_once(
            database_url=database_url, redis_client=rc.client())
        log.info(
            "platform_qc_reaper reaped=%d cache_state=%s authority_repaired=%s "
            "repair_skipped_reason=%s effective_armed=%s",
            result.reaped, result.cache_state, result.authority_repaired,
            result.repair_skipped_reason, result.effective_armed)
        return 0
    finally:
        await rc.close_redis()


def main() -> int:
    try:
        return asyncio.run(_main_async())
    except Exception:
        _bounded_log("reaper_run_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
