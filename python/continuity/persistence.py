"""C-02/C-06 -- dormant repository over the five C-03 continuity stores.

Offline, code-only wiring: this module talks only to the injected asyncpg-compatible pool it is
constructed with. It never imports the global ``database``/``storage`` modules, never opens a
second connection or transaction inside one public call, and never trusts a caller-supplied
derived field (contract identity, hash, or idempotency key) -- every one is re-derived from
trusted raw input before a single byte reaches SQL. Not wired into the live pipeline by this
module: C-06's claims/violations/coverage surface is a dormant store API only until B-02 exists,
and cleanup protection here is a schema-safety primitive independent of runtime traffic.

Every public method:

1. validates all non-DB inputs (exact types, bounds, cross-field shape) before acquiring a
   connection -- a ``PersistenceError`` here never touches the pool;
2. opens exactly one connection and one transaction;
3. sets the explicit tenant via ``set_config('app.current_tenant_id', $1, true)`` and repeats
   ``tenant_id`` in every predicate alongside RLS (belt-and-suspenders, matching the rest of this
   codebase's ``_q_*`` convention in ``database.py``); and
4. returns a detached plain-dict snapshot, never a live asyncpg.Record or a reference back into
   caller-owned containers.

Idempotency: contracts, violations, and recovery metadata use a fixed UUIDv5 derived from their
full identity/content payload (Section 5), so an exact retry always computes the SAME id and a
genuine content change always computes a DIFFERENT one -- the only way two different-content rows
can collide on identity is through a separate natural-key UNIQUE constraint (contracts'
``(tenant_id, job_id, contract_version)``), which is always a real conflict, never a retry. Claims
and coverage instead rely on their existing C-03 natural-key UNIQUE constraints directly (DB-random
``id``). Either way, the pattern is: attempt the INSERT, and on a unique-violation targeting the
row's own identity, re-read the existing row inside the SAME transaction and compare it against
what this call was about to write -- an exact match returns the existing row, any difference in a
content field raises ``IDEMPOTENCY_CONFLICT``. This is safe under real concurrency because each
attempt runs in its own connection/transaction and Postgres serializes the conflicting commits.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import math
import re
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from . import factory as _factory
from .gates import _claims_schema_errors
from .models import canonical_json, deep_freeze_copy, sha256_hex

_CONTRACT_NS = _uuid.UUID("ab459385-0918-4b0e-8885-02dc99093adc")
_VIOLATION_NS = _uuid.UUID("c4ab28df-7d64-42e4-a5ec-9d30da75e8a5")
_RECOVERY_NS = _uuid.UUID("88c514d7-4b9a-4c1d-a048-2428c396de66")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CHAPTER_ID_RE = re.compile(r"^ch_[0-9a-f]{32}$")

_VIOLATION_KEYS = frozenset({"predicate_id", "predicate_set_version", "violation_type", "severity", "claim_id", "evidence"})
_SEVERITIES = frozenset({"info", "low", "medium", "high", "blocker"})
_EVIDENCE_KEYS = frozenset({"text", "start", "end", "source_hash"})
_RESOLUTION_STATES = frozenset({"repaired", "dismissed", "unresolved"})
_COVERAGE_KEYS = frozenset({"chapter_id", "predicate_id", "state"})
_COVERAGE_STATES = frozenset({"complete", "partial", "failed", "not_applicable"})


class PersistenceError(Exception):
    """Raised for any invalid input or bounded persistence failure. Carries a stable ``code``;
    the message is always static and generic -- never manuscript text, claims payload, violation
    evidence, or a raw external exception's own text/arguments."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__((message or code)[:300])


def _fail(code: str, message: str = ""):
    raise PersistenceError(code, message)


def _exact_uuid(value: Any, label: str) -> str:
    if type(value) is not str or not _UUID_RE.fullmatch(value):
        _fail(f"{label.upper()}_INVALID", f"{label} must be an exact UUID string")
    return value


def _exact_hash(value: Any, label: str) -> str:
    if type(value) is not str or not _HASH_RE.fullmatch(value):
        _fail(f"{label.upper()}_INVALID", f"{label} must be an exact lowercase SHA-256 hex string")
    return value


def _utf8_byte_length(value: str) -> int | None:
    """The one strict-UTF-8 boundary for this module: the exact byte length of ``value`` if it
    encodes cleanly, else ``None`` (e.g. an unpaired surrogate). Every caller applies its own
    contextual error code -- a raw ``UnicodeEncodeError`` never crosses a public boundary."""
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _exact_str(value: Any, label: str, *, max_bytes: int | None = None, min_bytes: int = 0) -> str:
    if type(value) is not str:
        _fail(f"{label.upper()}_INVALID", f"{label} must be an exact string")
    size = _utf8_byte_length(value)
    if size is None:
        _fail(f"{label.upper()}_INVALID", f"{label} must be strict UTF-8")
    if size < min_bytes:
        _fail(f"{label.upper()}_INVALID", f"{label} must be a nonblank string of at least {min_bytes} byte(s)")
    if max_bytes is not None and size > max_bytes:
        _fail(f"{label.upper()}_TOO_LARGE", f"{label} exceeds its byte bound")
    return value


def _freeze_json(value: Any, *, code: str, _seen: frozenset = frozenset()) -> Any:
    """Recursively validate that ``value`` is built exclusively from exact JSON-shaped built-in
    types (``dict`` with exact strict-UTF-8 ``str`` keys, ``list``, strict-UTF-8 ``str``, ``int``,
    finite ``float``, ``bool``, ``None``) and return a detached deep copy in the same pass --
    tuples, sets, subclasses, cycles, unpaired-surrogate strings/keys, and non-finite numbers all
    reject as ``PersistenceError`` before the caller's own mutable containers are ever touched
    again."""
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is str:
        if _utf8_byte_length(value) is None:
            _fail(code, "strings must be strict UTF-8")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _fail(code, "numbers must be finite")
        return value
    if type(value) is dict:
        marker = id(value)
        if marker in _seen:
            _fail(code, "containers must be acyclic")
        nested = _seen | {marker}
        result = {}
        for key, item in value.items():
            if type(key) is not str or _utf8_byte_length(key) is None:
                _fail(code, "mapping keys must be exact strict-UTF-8 strings")
            result[key] = _freeze_json(item, code=code, _seen=nested)
        return result
    if type(value) is list:
        marker = id(value)
        if marker in _seen:
            _fail(code, "containers must be acyclic")
        nested = _seen | {marker}
        return [_freeze_json(item, code=code, _seen=nested) for item in value]
    _fail(code, "value must be an exact JSON-shaped type")


def _canonical_or_fail(value: Any, code: str) -> str:
    """The single bounded canonicalization boundary for caller-influenced JSON evaluated before
    pool acquisition. ``canonical_json`` can still raise a raw ``ValueError`` for an exact integer
    whose decimal expansion exceeds the runtime's int-to-string conversion bound
    (``sys.set_int_max_str_digits``) -- and ``TypeError``/``RecursionError`` for other unencodable
    shapes ``_freeze_json`` cannot pre-screen -- so every such failure must become a bounded
    ``PersistenceError`` before any connection is acquired, never a raw exception across the public
    boundary."""
    try:
        return canonical_json(value)
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise PersistenceError(code, "value is not safely canonicalizable") from exc


def _exact_positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        _fail(f"{label.upper()}_INVALID", f"{label} must be an exact positive integer")
    return value


def _exact_nonneg_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        _fail(f"{label.upper()}_INVALID", f"{label} must be an exact nonnegative integer")
    return value


def _exact_bounded_int(value: Any, label: str, lo: int, hi: int) -> int:
    if type(value) is not int or value < lo or value > hi:
        _fail(f"{label.upper()}_INVALID", f"{label} must be an exact integer between {lo} and {hi}")
    return value


def _canonical_utc_or_none(value: Any) -> datetime | None:
    """The one detach-and-validate primitive for every datetime boundary in this module. Reads
    the source ``tzinfo``'s ``.utcoffset()`` EXACTLY ONCE and never re-enters it -- ``astimezone()``
    would call it a second time, and a stateful ``tzinfo`` can answer zero on the first call and a
    different offset on the second, shifting the value after it was already accepted as UTC. The
    offset must be an exact built-in ``timedelta`` equal to zero: a subclass overriding equality to
    fake a zero comparison is rejected by the exact-type check before its equality method is ever
    invoked. The returned snapshot copies only the already-UTC wall-clock fields under the exact
    ``timezone.utc`` singleton -- no arithmetic, no second callback. Returns ``None`` on any
    violation so callers can apply their own contextual code."""
    if type(value) is not datetime or value.tzinfo is None:
        return None
    try:
        offset = value.utcoffset()
        if type(offset) is not timedelta or offset != timedelta(0):
            return None
        return value.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _exact_aware_datetime(value: Any, label: str) -> datetime:
    result = _canonical_utc_or_none(value)
    if result is None:
        _fail(f"{label.upper()}_INVALID", f"{label} must be an exact UTC timezone-aware datetime")
    return result


def _exact_keys(item: Any, expected: frozenset, code: str, message: str) -> None:
    if type(item) is not dict or set(item) != expected or not all(type(k) is str for k in item):
        _fail(code, message)


def _row(record: asyncpg.Record | None) -> dict | None:
    if record is None:
        return None
    return deep_freeze_copy({key: (str(value) if isinstance(value, _uuid.UUID) else value) for key, value in dict(record).items()})


def _validate_evidence_item(item: Any) -> None:
    _exact_keys(item, _EVIDENCE_KEYS, "EVIDENCE_INVALID", "each evidence item must have exactly text/start/end/source_hash")
    if type(item["text"]) is not str or len(item["text"].encode("utf-8")) > 500:
        _fail("EVIDENCE_INVALID", "evidence text must be an exact string at most 500 bytes")
    if type(item["start"]) is not int or type(item["end"]) is not int:
        _fail("EVIDENCE_INVALID", "evidence start/end must be exact integers")
    if item["start"] < 0 or item["start"] > item["end"]:
        _fail("EVIDENCE_INVALID", "evidence offsets must satisfy 0 <= start <= end")
    _exact_hash(item.get("source_hash"), "evidence_source_hash")


def _validate_violation_item(item: Any) -> dict:
    _exact_keys(item, _VIOLATION_KEYS, "VIOLATION_INVALID", "each violation must have exactly the six named fields")
    _exact_str(item["predicate_id"], "predicate_id", min_bytes=1, max_bytes=128)
    _exact_str(item["predicate_set_version"], "predicate_set_version", min_bytes=1, max_bytes=64)
    _exact_str(item["violation_type"], "violation_type", min_bytes=1, max_bytes=128)
    if type(item["severity"]) is not str or item["severity"] not in _SEVERITIES:
        _fail("VIOLATION_INVALID", "severity must be one of the five exact severities")
    claim_id = item["claim_id"]
    if claim_id is not None:
        _exact_uuid(claim_id, "claim_id")
    evidence = item["evidence"]
    if evidence is not None:
        evidence = _freeze_json(evidence, code="EVIDENCE_INVALID")
        if type(evidence) is not list or not (1 <= len(evidence) <= 100):
            _fail("VIOLATION_INVALID", "evidence must be null or a list of 1..100 items")
        if len(_canonical_or_fail(evidence, "VIOLATION_INVALID").encode("utf-8")) > 262144:
            _fail("VIOLATION_INVALID", "evidence exceeds its serialized byte bound")
        for evidence_item in evidence:
            _validate_evidence_item(evidence_item)
    return {**item, "evidence": evidence}


def _validate_coverage_item(item: Any) -> None:
    _exact_keys(item, _COVERAGE_KEYS, "COVERAGE_INVALID", "each coverage record must have exactly chapter_id/predicate_id/state")
    if type(item["chapter_id"]) is not str or not _CHAPTER_ID_RE.fullmatch(item["chapter_id"]):
        _fail("COVERAGE_INVALID", "coverage chapter_id must be an exact stable chapter id")
    _exact_str(item["predicate_id"], "predicate_id", min_bytes=1, max_bytes=128)
    if type(item["state"]) is not str or item["state"] not in _COVERAGE_STATES:
        _fail("COVERAGE_INVALID", "coverage state must be one of the four exact states")


class ContinuityRepository:
    """Explicit-tenant, RLS-aware repository over the five C-03 continuity stores. Constructed
    with an asyncpg-compatible pool; never imports or calls the global ``database``/``storage``
    modules, never performs provider/network/Redis/filesystem/environment access."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ── Contracts (C-02) ────────────────────────────────────────────────────────────────────

    async def persist_contract(
        self, tenant_id, job_id, factory_request, factory_artifact_obj, *,
        project_id=None, contract_version=1, supersedes_contract_id=None,
        amendment_reason=None, affected_chapter_ids=None, revalidation_plan=None,
    ) -> dict:
        tenant_id = _exact_uuid(tenant_id, "tenant_id")
        job_id = _exact_uuid(job_id, "job_id")
        contract_version = _exact_positive_int(contract_version, "contract_version")
        if project_id is not None:
            project_id = _exact_uuid(project_id, "project_id")
        if supersedes_contract_id is not None:
            supersedes_contract_id = _exact_uuid(supersedes_contract_id, "supersedes_contract_id")
        if amendment_reason is not None:
            _exact_str(amendment_reason, "amendment_reason", max_bytes=2000)
        if affected_chapter_ids is None:
            affected_chapter_ids = []
        if type(affected_chapter_ids) is not list or not all(
            type(x) is str and _CHAPTER_ID_RE.fullmatch(x) for x in affected_chapter_ids
        ):
            _fail("AFFECTED_CHAPTER_IDS_INVALID", "affected_chapter_ids must be a list of exact stable chapter ids")
        affected_chapter_ids = list(affected_chapter_ids)
        if len(set(affected_chapter_ids)) != len(affected_chapter_ids):
            _fail("AFFECTED_CHAPTER_IDS_INVALID", "affected_chapter_ids must not contain duplicates")
        if revalidation_plan is None:
            revalidation_plan = {}
        revalidation_plan = _freeze_json(revalidation_plan, code="REVALIDATION_PLAN_INVALID")
        if type(revalidation_plan) is not dict:
            _fail("REVALIDATION_PLAN_INVALID", "revalidation_plan must be an exact object or None")
        if len(_canonical_or_fail(revalidation_plan, "REVALIDATION_PLAN_INVALID").encode("utf-8")) > 16384:
            _fail("REVALIDATION_PLAN_INVALID", "revalidation_plan exceeds its serialized byte bound")

        try:
            validated_request = _factory.validate_factory_request(factory_request)
            bundle = _factory.validate_factory_artifact(factory_request, factory_artifact_obj)
        except _factory.FactoryValidationError as exc:
            raise PersistenceError("FACTORY_ARTIFACT_INVALID", "factory artifact/request failed validation") from exc

        story_contract = bundle["story_contract"]
        story_contract_hash = bundle["story_contract_hash"]
        target_language = story_contract["target_language"]
        contract_schema_version = story_contract["schema_version"]
        versions = validated_request["versions"]
        ordered_chapter_ids = list(bundle["ordered_chapter_ids"])
        chapter_slice_hashes = {item["chapter_id"]: item["slice_hash"] for item in bundle["slices"]}

        if not set(affected_chapter_ids) <= set(ordered_chapter_ids):
            _fail("AFFECTED_CHAPTER_IDS_INVALID", "affected_chapter_ids must be a subset of the contract's chapter ids")

        row_id = _uuid.uuid5(_CONTRACT_NS, canonical_json(
            [tenant_id, job_id, contract_version, story_contract_hash]
        ))

        intended = {
            "project_id": project_id, "supersedes_contract_id": supersedes_contract_id,
            "amendment_reason": amendment_reason, "affected_chapter_ids": affected_chapter_ids,
            "revalidation_plan": revalidation_plan,
        }

        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
                try:
                    # A nested `conn.transaction()` becomes a SAVEPOINT here (we are already
                    # inside the outer transaction). Without it, a UniqueViolationError from this
                    # INSERT would poison the whole outer transaction, and the re-read SELECT in
                    # the except block below would itself fail with InFailedSQLTransactionError.
                    async with conn.transaction():
                        record = await conn.fetchrow(
                            """
                            INSERT INTO narasi_continuity_contracts (
                                id, tenant_id, project_id, job_id, contract_version, supersedes_contract_id,
                                status, amendment_reason, affected_chapter_ids, revalidation_plan,
                                story_contract, story_contract_hash, target_language, contract_schema_version,
                                contract_prompt_version, compiler_version, slicer_version,
                                ordered_chapter_ids, chapter_slice_hashes
                            ) VALUES (
                                $1, $2, $3, $4, $5, $6, 'validated', $7, $8, $9,
                                $10, $11, $12, $13, $14, $15, $16, $17, $18
                            ) RETURNING *
                            """,
                            row_id, _uuid.UUID(tenant_id), _uuid.UUID(project_id) if project_id else None,
                            _uuid.UUID(job_id), contract_version,
                            _uuid.UUID(supersedes_contract_id) if supersedes_contract_id else None,
                            amendment_reason, affected_chapter_ids, revalidation_plan,
                            story_contract, story_contract_hash, target_language, contract_schema_version,
                            versions["contract_prompt"], versions["compiler"], versions["slicer"],
                            ordered_chapter_ids, chapter_slice_hashes,
                        )
                except asyncpg.UniqueViolationError as exc:
                    if exc.constraint_name == "uq_c03_contracts_tenant_job_ver":
                        raise PersistenceError(
                            "IDEMPOTENCY_CONFLICT", "a different contract already occupies this job/version"
                        ) from exc
                    existing = await conn.fetchrow(
                        "SELECT * FROM narasi_continuity_contracts WHERE id=$1 AND tenant_id=$2",
                        row_id, _uuid.UUID(tenant_id),
                    )
                    if existing is None:
                        raise PersistenceError("PERSISTENCE_DB_ERROR", "contract identity conflict without a readable row") from exc
                    existing_dict = dict(existing)
                    for key, value in intended.items():
                        existing_value = existing_dict[key]
                        if isinstance(existing_value, _uuid.UUID):
                            existing_value = str(existing_value)
                        if existing_value != value:
                            raise PersistenceError("IDEMPOTENCY_CONFLICT", f"retry conflicts on {key}") from exc
                    record = existing
                return _row(record)
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    async def get_contract(self, tenant_id, contract_id) -> dict | None:
        tenant_id = _exact_uuid(tenant_id, "tenant_id")
        contract_id = _exact_uuid(contract_id, "contract_id")
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
                record = await conn.fetchrow(
                    "SELECT * FROM narasi_continuity_contracts WHERE id=$1 AND tenant_id=$2",
                    _uuid.UUID(contract_id), _uuid.UUID(tenant_id),
                )
                return _row(record)
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    async def _mark_contract_status(self, tenant_id, contract_id, *, from_status: str, to_status: str, set_generation_started: bool) -> dict:
        tenant_id = _exact_uuid(tenant_id, "tenant_id")
        contract_id = _exact_uuid(contract_id, "contract_id")
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
                if set_generation_started:
                    record = await conn.fetchrow(
                        """UPDATE narasi_continuity_contracts SET status=$3, generation_started_at=now()
                           WHERE id=$1 AND tenant_id=$2 AND status=$4 RETURNING *""",
                        _uuid.UUID(contract_id), _uuid.UUID(tenant_id), to_status, from_status,
                    )
                else:
                    record = await conn.fetchrow(
                        """UPDATE narasi_continuity_contracts SET status=$3
                           WHERE id=$1 AND tenant_id=$2 AND status=$4 RETURNING *""",
                        _uuid.UUID(contract_id), _uuid.UUID(tenant_id), to_status, from_status,
                    )
                if record is None:
                    raise PersistenceError("CONTRACT_STALE_TRANSITION", "contract is not in the required prior status")
                return _row(record)
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    async def mark_contract_active(self, tenant_id, contract_id) -> dict:
        return await self._mark_contract_status(
            tenant_id, contract_id, from_status="validated", to_status="active", set_generation_started=True
        )

    async def mark_contract_rejected(self, tenant_id, contract_id) -> dict:
        return await self._mark_contract_status(
            tenant_id, contract_id, from_status="validated", to_status="rejected", set_generation_started=False
        )

    async def mark_contract_superseded(self, tenant_id, contract_id) -> dict:
        return await self._mark_contract_status(
            tenant_id, contract_id, from_status="active", to_status="superseded", set_generation_started=False
        )

    # ── Claims (C-06) ───────────────────────────────────────────────────────────────────────

    async def persist_claims(
        self, tenant_id, contract_id, job_id, story_contract_hash, *,
        chapter_text, claims, extractor_schema_version, extractor_prompt_version,
        extractor_epoch, model_route, expires_at=None,
    ) -> dict:
        tenant_id = _exact_uuid(tenant_id, "tenant_id")
        contract_id = _exact_uuid(contract_id, "contract_id")
        job_id = _exact_uuid(job_id, "job_id")
        story_contract_hash = _exact_hash(story_contract_hash, "story_contract_hash")
        _exact_str(chapter_text, "chapter_text")
        _exact_str(extractor_schema_version, "extractor_schema_version", min_bytes=1, max_bytes=32)
        _exact_str(extractor_prompt_version, "extractor_prompt_version", min_bytes=1, max_bytes=32)
        extractor_epoch = _exact_nonneg_int(extractor_epoch, "extractor_epoch")
        _exact_str(model_route, "model_route", min_bytes=1, max_bytes=128)
        if expires_at is not None:
            # V6P03: reassign to the detached canonical-UTC snapshot -- every downstream use
            # (the range check here, and the SQL parameter below) reads only this value, never
            # the caller-owned datetime whose tzinfo could keep mutating after this validation.
            expires_at = _exact_aware_datetime(expires_at, "expires_at")
            now = datetime.now(timezone.utc)
            if expires_at <= now or expires_at > now + timedelta(days=30):
                raise PersistenceError("CLAIMS_EXPIRY_INVALID", "expires_at must be later than now and at most 30 days out")

        # V4P03: take ONE detached snapshot of the caller-owned claims before any await/acquire, and
        # use only this snapshot for schema validation, content/hash derivation, the persisted
        # envelope, in-transaction cross-binding, and the SQL identity columns. Reading the caller's
        # original dict for the SQL chapter_id/chapter_number after acquisition let a mutation during
        # acquire() persist a row whose chapter_id column disagreed with its own claims envelope.
        # V5P01: `deep_freeze_copy`'s `isinstance` checks normalize tuples/subclasses and recurse
        # forever on a cycle -- use the same exact-type, cycle-safe `_freeze_json` boundary already
        # required for revalidation_plan/coverage/evidence, not a separate permissive copier.
        claims = _freeze_json(claims, code="CLAIMS_SCHEMA_INVALID")
        schema_errors = _claims_schema_errors(claims)
        if schema_errors:
            raise PersistenceError("CLAIMS_SCHEMA_INVALID", "claims failed Claims v1 schema validation")
        real_content_hash = sha256_hex(chapter_text)
        if claims["content_hash"] != real_content_hash:
            raise PersistenceError("CLAIMS_CONTENT_HASH_MISMATCH", "claims.content_hash does not match chapter_text")

        envelope = deep_freeze_copy([{
            "claims_hash": sha256_hex(_canonical_or_fail(claims, "CLAIMS_SCHEMA_INVALID")),
            "kind": "claims_v1",
            "schema_version": "1",
            "value": claims,
        }])

        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
                contract = await conn.fetchrow(
                    "SELECT * FROM narasi_continuity_contracts WHERE id=$1 AND tenant_id=$2 AND job_id=$3 AND story_contract_hash=$4",
                    _uuid.UUID(contract_id), _uuid.UUID(tenant_id), _uuid.UUID(job_id), story_contract_hash,
                )
                if contract is None:
                    raise PersistenceError("CONTRACT_NOT_FOUND", "no matching contract for this tenant/job/hash binding")
                story_contract = contract["story_contract"]
                if claims["contract_id"] != story_contract["contract_id"]:
                    raise PersistenceError("CLAIMS_CROSS_BINDING_INVALID", "claims.contract_id does not bind to the real contract")
                if claims["target_language"] != contract["target_language"]:
                    raise PersistenceError("CLAIMS_CROSS_BINDING_INVALID", "claims.target_language does not bind to the real contract")
                chapter = next(
                    (c for c in story_contract["chapter_contracts"] if c["chapter_id"] == claims["chapter_id"]), None
                )
                if chapter is None or chapter["chapter_number"] != claims["chapter_number"]:
                    raise PersistenceError("CLAIMS_CROSS_BINDING_INVALID", "claims.chapter_id/chapter_number does not bind to the real contract")

                try:
                    async with conn.transaction():
                        record = await conn.fetchrow(
                            """
                            INSERT INTO narasi_continuity_claims (
                                tenant_id, contract_id, job_id, chapter_id, chapter_content_hash,
                                story_contract_hash, extractor_schema_version, extractor_prompt_version,
                                extractor_epoch, model_route, claims, expires_at
                            ) VALUES (
                                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, COALESCE($12, now() + interval '30 days')
                            ) RETURNING *
                            """,
                            _uuid.UUID(tenant_id), _uuid.UUID(contract_id), _uuid.UUID(job_id), claims["chapter_id"],
                            real_content_hash, story_contract_hash, extractor_schema_version, extractor_prompt_version,
                            extractor_epoch, model_route, envelope, expires_at,
                        )
                except asyncpg.UniqueViolationError as exc:
                    existing = await conn.fetchrow(
                        """SELECT * FROM narasi_continuity_claims
                           WHERE tenant_id=$1 AND job_id=$2 AND chapter_id=$3 AND chapter_content_hash=$4
                             AND story_contract_hash=$5 AND extractor_epoch=$6 AND model_route=$7""",
                        _uuid.UUID(tenant_id), _uuid.UUID(job_id), claims["chapter_id"], real_content_hash,
                        story_contract_hash, extractor_epoch, model_route,
                    )
                    if existing is None:
                        raise PersistenceError("PERSISTENCE_DB_ERROR", "claims identity conflict without a readable row") from exc
                    existing_is_default = existing["expires_at"] == existing["created_at"] + timedelta(days=30)
                    retry_is_default = expires_at is None
                    expiry_conflict = existing_is_default != retry_is_default or (
                        not retry_is_default and existing["expires_at"] != expires_at
                    )
                    if (
                        str(existing["contract_id"]) != contract_id
                        or existing["extractor_schema_version"] != extractor_schema_version
                        or existing["extractor_prompt_version"] != extractor_prompt_version
                        or existing["claims"] != envelope
                        or expiry_conflict
                    ):
                        raise PersistenceError("IDEMPOTENCY_CONFLICT", "retry conflicts on claims payload") from exc
                    record = existing
                return _row(record)
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    # ── Violations (C-06) ───────────────────────────────────────────────────────────────────

    async def persist_violations(self, tenant_id, contract_id, job_id, story_contract_hash, violations, *, evidence_expires_at=None):
        tenant_id = _exact_uuid(tenant_id, "tenant_id")
        contract_id = _exact_uuid(contract_id, "contract_id")
        job_id = _exact_uuid(job_id, "job_id")
        story_contract_hash = _exact_hash(story_contract_hash, "story_contract_hash")
        if type(violations) is not list or not (1 <= len(violations) <= 100):
            raise PersistenceError("VIOLATION_BATCH_INVALID", "violations must be a list of 1..100 items")
        violations = [_validate_violation_item(item) for item in violations]
        predicate_set_versions = {item["predicate_set_version"] for item in violations}
        if len(predicate_set_versions) > 1:
            raise PersistenceError("VIOLATION_BATCH_INVALID", "a batch may not mix predicate_set_version values")
        if evidence_expires_at is not None:
            # V6P03: same canonical-UTC reassignment discipline as persist_claims.expires_at.
            evidence_expires_at = _exact_aware_datetime(evidence_expires_at, "evidence_expires_at")
            now = datetime.now(timezone.utc)
            if evidence_expires_at <= now or evidence_expires_at > now + timedelta(days=30):
                raise PersistenceError("EVIDENCE_EXPIRY_INVALID", "evidence_expires_at must be later than now and at most 30 days out")

        planned = []
        seen_ids = set()
        for item in violations:
            claim_id = item["claim_id"]
            evidence = item["evidence"]
            row_id = _uuid.uuid5(_VIOLATION_NS, canonical_json([
                tenant_id, contract_id, job_id, story_contract_hash, item["predicate_id"],
                item["predicate_set_version"], item["violation_type"], item["severity"], claim_id, evidence,
            ]))
            if row_id in seen_ids:
                raise PersistenceError("VIOLATION_BATCH_INVALID", "duplicate violation identity within one batch")
            seen_ids.add(row_id)
            planned.append((item, claim_id, evidence, row_id))

        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
                results = []
                for item, claim_id, evidence, row_id in planned:
                    if claim_id is not None:
                        bound = await conn.fetchval(
                            """SELECT 1 FROM narasi_continuity_claims
                               WHERE id=$1 AND tenant_id=$2 AND contract_id=$3 AND job_id=$4 AND story_contract_hash=$5""",
                            _uuid.UUID(claim_id), _uuid.UUID(tenant_id), _uuid.UUID(contract_id),
                            _uuid.UUID(job_id), story_contract_hash,
                        )
                        if bound is None:
                            raise PersistenceError("CLAIM_BINDING_INVALID", "claim_id does not bind to this tenant/contract/job/hash")
                    try:
                        async with conn.transaction():
                            record = await conn.fetchrow(
                                """
                                INSERT INTO narasi_continuity_violations (
                                    id, tenant_id, contract_id, claim_id, job_id, story_contract_hash,
                                    predicate_id, predicate_set_version, violation_type, severity,
                                    evidence, evidence_expires_at
                                ) VALUES (
                                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                                    CASE WHEN $11::jsonb IS NULL THEN NULL ELSE COALESCE($12, now() + interval '30 days') END
                                )
                                RETURNING *
                                """,
                                row_id, _uuid.UUID(tenant_id), _uuid.UUID(contract_id),
                                _uuid.UUID(claim_id) if claim_id else None, _uuid.UUID(job_id), story_contract_hash,
                                item["predicate_id"], item["predicate_set_version"], item["violation_type"],
                                item["severity"], evidence, evidence_expires_at,
                            )
                    except asyncpg.UniqueViolationError as exc:
                        record = await conn.fetchrow(
                            "SELECT * FROM narasi_continuity_violations WHERE id=$1 AND tenant_id=$2",
                            row_id, _uuid.UUID(tenant_id),
                        )
                        if record is None:
                            raise PersistenceError("PERSISTENCE_DB_ERROR", "violation identity conflict without a readable row") from exc
                        if evidence is not None:
                            existing_is_default = record["evidence_expires_at"] == record["created_at"] + timedelta(days=30)
                            retry_is_default = evidence_expires_at is None
                            expiry_conflict = existing_is_default != retry_is_default or (
                                not retry_is_default and record["evidence_expires_at"] != evidence_expires_at
                            )
                            if expiry_conflict:
                                raise PersistenceError("IDEMPOTENCY_CONFLICT", "retry conflicts on evidence_expires_at") from exc
                    results.append(_row(record))
                return results
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    async def resolve_violation(self, tenant_id, violation_id, resolution_state, attempt_count) -> dict:
        tenant_id = _exact_uuid(tenant_id, "tenant_id")
        violation_id = _exact_uuid(violation_id, "violation_id")
        if type(resolution_state) is not str or resolution_state not in _RESOLUTION_STATES:
            raise PersistenceError("RESOLUTION_STATE_INVALID", "resolution_state must be repaired, dismissed, or unresolved")
        attempt_count = _exact_nonneg_int(attempt_count, "attempt_count")
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
                record = await conn.fetchrow(
                    """UPDATE narasi_continuity_violations SET attempt_count=$3, resolution_state=$4, resolved_at=now()
                       WHERE id=$1 AND tenant_id=$2 AND resolution_state='open' AND attempt_count<=$3
                       RETURNING *""",
                    _uuid.UUID(violation_id), _uuid.UUID(tenant_id), attempt_count, resolution_state,
                )
                if record is not None:
                    return _row(record)
                existing = await conn.fetchrow(
                    "SELECT * FROM narasi_continuity_violations WHERE id=$1 AND tenant_id=$2",
                    _uuid.UUID(violation_id), _uuid.UUID(tenant_id),
                )
                if existing is None:
                    raise PersistenceError("VIOLATION_NOT_FOUND", "violation not found")
                if existing["resolution_state"] != "open":
                    if existing["resolution_state"] == resolution_state and existing["attempt_count"] == attempt_count:
                        return _row(existing)
                    raise PersistenceError("IDEMPOTENCY_CONFLICT", "violation already resolved with a different outcome")
                raise PersistenceError("ATTEMPT_COUNT_INVALID", "attempt_count cannot decrease")
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    async def purge_violation_evidence(self, tenant_id, violation_id) -> dict:
        tenant_id = _exact_uuid(tenant_id, "tenant_id")
        violation_id = _exact_uuid(violation_id, "violation_id")
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
                record = await conn.fetchrow(
                    """UPDATE narasi_continuity_violations
                       SET evidence=NULL, evidence_expires_at=NULL, evidence_purged_at=now()
                       WHERE id=$1 AND tenant_id=$2 AND evidence IS NOT NULL
                       RETURNING *""",
                    _uuid.UUID(violation_id), _uuid.UUID(tenant_id),
                )
                if record is not None:
                    return _row(record)
                existing = await conn.fetchrow(
                    "SELECT * FROM narasi_continuity_violations WHERE id=$1 AND tenant_id=$2",
                    _uuid.UUID(violation_id), _uuid.UUID(tenant_id),
                )
                if existing is None:
                    raise PersistenceError("VIOLATION_NOT_FOUND", "violation not found")
                if existing["evidence_purged_at"] is not None:
                    return _row(existing)
                raise PersistenceError("EVIDENCE_NOT_PRESENT", "violation has no evidence to purge")
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    # ── Coverage (C-06) ─────────────────────────────────────────────────────────────────────

    async def persist_coverage(
        self, tenant_id, contract_id, job_id, story_contract_hash, final_candidate_text, coverage, *,
        predicate_set_version, extractor_schema_version, extractor_prompt_version, extractor_epoch, diff_version,
    ) -> dict:
        tenant_id = _exact_uuid(tenant_id, "tenant_id")
        contract_id = _exact_uuid(contract_id, "contract_id")
        job_id = _exact_uuid(job_id, "job_id")
        story_contract_hash = _exact_hash(story_contract_hash, "story_contract_hash")
        _exact_str(final_candidate_text, "final_candidate_text")
        coverage = _freeze_json(coverage, code="COVERAGE_INVALID")
        if type(coverage) is not list or not (1 <= len(coverage) <= 10000):
            raise PersistenceError("COVERAGE_INVALID", "coverage must be a list of 1..10000 items")
        coverage_serialized = _canonical_or_fail(coverage, "COVERAGE_INVALID")
        if len(coverage_serialized.encode("utf-8")) > 1048576:
            raise PersistenceError("COVERAGE_INVALID", "coverage exceeds its serialized byte bound")
        seen_pairs = set()
        for item in coverage:
            _validate_coverage_item(item)
            pair = (item["chapter_id"], item["predicate_id"])
            if pair in seen_pairs:
                raise PersistenceError("COVERAGE_INVALID", "duplicate chapter_id/predicate_id pair")
            seen_pairs.add(pair)
        _exact_str(predicate_set_version, "predicate_set_version", min_bytes=1, max_bytes=64)
        _exact_str(extractor_schema_version, "extractor_schema_version", min_bytes=1, max_bytes=32)
        _exact_str(extractor_prompt_version, "extractor_prompt_version", min_bytes=1, max_bytes=32)
        extractor_epoch = _exact_nonneg_int(extractor_epoch, "extractor_epoch")
        _exact_str(diff_version, "diff_version", min_bytes=1, max_bytes=32)

        final_candidate_hash = sha256_hex(final_candidate_text)
        coverage_hash = sha256_hex(coverage_serialized)

        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
                contract = await conn.fetchrow(
                    "SELECT 1 FROM narasi_continuity_contracts WHERE id=$1 AND tenant_id=$2 AND job_id=$3 AND story_contract_hash=$4",
                    _uuid.UUID(contract_id), _uuid.UUID(tenant_id), _uuid.UUID(job_id), story_contract_hash,
                )
                if contract is None:
                    raise PersistenceError("CONTRACT_NOT_FOUND", "no matching contract for this tenant/job/hash binding")
                try:
                    async with conn.transaction():
                        record = await conn.fetchrow(
                            """
                            INSERT INTO narasi_continuity_coverage (
                                tenant_id, contract_id, job_id, story_contract_hash, final_candidate_hash,
                                predicate_set_version, extractor_schema_version, extractor_prompt_version,
                                extractor_epoch, diff_version, coverage, coverage_hash
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                            RETURNING *
                            """,
                            _uuid.UUID(tenant_id), _uuid.UUID(contract_id), _uuid.UUID(job_id), story_contract_hash,
                            final_candidate_hash, predicate_set_version, extractor_schema_version,
                            extractor_prompt_version, extractor_epoch, diff_version, coverage, coverage_hash,
                        )
                except asyncpg.UniqueViolationError as exc:
                    existing = await conn.fetchrow(
                        """SELECT * FROM narasi_continuity_coverage
                           WHERE tenant_id=$1 AND job_id=$2 AND final_candidate_hash=$3 AND predicate_set_version=$4""",
                        _uuid.UUID(tenant_id), _uuid.UUID(job_id), final_candidate_hash, predicate_set_version,
                    )
                    if existing is None:
                        raise PersistenceError("PERSISTENCE_DB_ERROR", "coverage identity conflict without a readable row") from exc
                    if (
                        str(existing["contract_id"]) != contract_id
                        or existing["coverage_hash"] != coverage_hash
                        or existing["extractor_schema_version"] != extractor_schema_version
                        or existing["extractor_prompt_version"] != extractor_prompt_version
                        or existing["extractor_epoch"] != extractor_epoch
                        or existing["diff_version"] != diff_version
                    ):
                        raise PersistenceError("IDEMPOTENCY_CONFLICT", "retry conflicts on coverage content") from exc
                    record = existing
                return _row(record)
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    async def get_coverage(self, tenant_id, job_id, final_candidate_hash, predicate_set_version) -> dict | None:
        tenant_id = _exact_uuid(tenant_id, "tenant_id")
        job_id = _exact_uuid(job_id, "job_id")
        final_candidate_hash = _exact_hash(final_candidate_hash, "final_candidate_hash")
        _exact_str(predicate_set_version, "predicate_set_version", min_bytes=1, max_bytes=64)
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
                record = await conn.fetchrow(
                    """SELECT * FROM narasi_continuity_coverage
                       WHERE tenant_id=$1 AND job_id=$2 AND final_candidate_hash=$3 AND predicate_set_version=$4""",
                    _uuid.UUID(tenant_id), _uuid.UUID(job_id), final_candidate_hash, predicate_set_version,
                )
                return _row(record)
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    # ── Retention ───────────────────────────────────────────────────────────────────────────

    async def delete_expired_claims(self, tenant_id, *, cutoff, limit=100) -> list:
        tenant_id = _exact_uuid(tenant_id, "tenant_id")
        # V6P03: reassign to the canonical detached snapshot before pool acquisition, so a
        # caller-owned tzinfo mutated during/after acquire() cannot change what SQL receives.
        cutoff = _exact_aware_datetime(cutoff, "cutoff")
        limit = _exact_bounded_int(limit, "limit", 1, 1000)
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
                rows = await conn.fetch(
                    """DELETE FROM narasi_continuity_claims WHERE tenant_id=$1 AND id IN (
                           SELECT id FROM narasi_continuity_claims
                           WHERE tenant_id=$1 AND expires_at<=$2
                           ORDER BY expires_at, id LIMIT $3
                       ) RETURNING id""",
                    _uuid.UUID(tenant_id), cutoff, limit,
                )
                return [str(row["id"]) for row in rows]
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    async def purge_expired_violation_evidence(self, tenant_id, *, cutoff, limit=100) -> list:
        tenant_id = _exact_uuid(tenant_id, "tenant_id")
        # V6P03: same canonical-UTC reassignment discipline as delete_expired_claims.
        cutoff = _exact_aware_datetime(cutoff, "cutoff")
        limit = _exact_bounded_int(limit, "limit", 1, 1000)
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
                rows = await conn.fetch(
                    """UPDATE narasi_continuity_violations SET evidence=NULL, evidence_expires_at=NULL, evidence_purged_at=now()
                       WHERE tenant_id=$1 AND id IN (
                           SELECT id FROM narasi_continuity_violations
                           WHERE tenant_id=$1 AND evidence IS NOT NULL AND evidence_expires_at<=$2
                           ORDER BY evidence_expires_at, id LIMIT $3
                       ) RETURNING id""",
                    _uuid.UUID(tenant_id), cutoff, limit,
                )
                return [str(row["id"]) for row in rows]
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    # ── Recovery metadata (C-07 private surface -- called only by continuity.recovery) ─────

    def _recovery_id(self, tenant_id: str, contract_id: str, job_id: str, story_contract_hash: str, final_candidate_hash: str) -> str:
        return str(_uuid.uuid5(_RECOVERY_NS, canonical_json(
            [tenant_id, contract_id, job_id, story_contract_hash, final_candidate_hash]
        )))

    @asynccontextmanager
    async def _recovery_lock(self, recovery_id: str):
        """A cross-process advisory lock scoped to one deterministic recovery identity, so
        independent ``RecoveryService``/``ContinuityRepository`` instances sharing the same
        database serialize around the SAME identity before encryption/upload -- a process-local
        lock alone cannot do this. Session-scoped (``pg_advisory_lock``), held on one connection
        checked out for the whole critical section and released in ``finally``."""
        conn = await self._pool.acquire()
        key = int(sha256_hex(recovery_id)[:16], 16) - 2**63
        try:
            await conn.execute("SELECT pg_advisory_lock($1)", key)
            try:
                yield conn
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", key)
        finally:
            await self._pool.release(conn)

    async def _recovery_get(self, tenant_id: str, recovery_id: str, *, _conn=None) -> dict | None:
        async def _query(conn):
            await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
            record = await conn.fetchrow(
                "SELECT * FROM narasi_continuity_recovery_artifacts WHERE id=$1 AND tenant_id=$2",
                _uuid.UUID(recovery_id), _uuid.UUID(tenant_id),
            )
            return _row(record)
        try:
            if _conn is not None:
                async with _conn.transaction():
                    return await _query(_conn)
            async with self._pool.acquire() as conn, conn.transaction():
                return await _query(conn)
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    async def _recovery_contract_binding(self, tenant_id: str, contract_id: str, job_id: str, story_contract_hash: str) -> bool:
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
                found = await conn.fetchval(
                    "SELECT 1 FROM narasi_continuity_contracts WHERE id=$1 AND tenant_id=$2 AND job_id=$3 AND story_contract_hash=$4",
                    _uuid.UUID(contract_id), _uuid.UUID(tenant_id), _uuid.UUID(job_id), story_contract_hash,
                )
                return found is not None
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    async def _recovery_insert(self, tenant_id: str, recovery_id: str, *, contract_id, job_id, story_contract_hash,
                                final_candidate_hash, object_bucket, object_key, object_version,
                                encryption_algorithm, encryption_key_id, ciphertext_sha256,
                                ciphertext_size_bytes, terminal_at, expires_at, _conn=None) -> dict:
        intended = {
            "contract_id": contract_id, "job_id": job_id, "story_contract_hash": story_contract_hash,
            "final_candidate_hash": final_candidate_hash, "object_bucket": object_bucket,
            "object_key": object_key, "object_version": object_version,
            "encryption_algorithm": encryption_algorithm, "encryption_key_id": encryption_key_id,
            "ciphertext_sha256": ciphertext_sha256, "ciphertext_size_bytes": ciphertext_size_bytes,
            "terminal_at": terminal_at, "expires_at": expires_at,
        }

        async def _do(conn):
            await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
            try:
                async with conn.transaction():
                    record = await conn.fetchrow(
                        """
                        INSERT INTO narasi_continuity_recovery_artifacts (
                            id, tenant_id, contract_id, job_id, story_contract_hash, final_candidate_hash,
                            object_bucket, object_key, object_version, encryption_algorithm, encryption_key_id,
                            ciphertext_sha256, ciphertext_size_bytes, terminal_at, expires_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                        RETURNING *
                        """,
                        _uuid.UUID(recovery_id), _uuid.UUID(tenant_id), _uuid.UUID(contract_id), _uuid.UUID(job_id),
                        story_contract_hash, final_candidate_hash, object_bucket, object_key, object_version,
                        encryption_algorithm, encryption_key_id, ciphertext_sha256, ciphertext_size_bytes,
                        terminal_at, expires_at,
                    )
            except asyncpg.UniqueViolationError as exc:
                existing = await conn.fetchrow(
                    "SELECT * FROM narasi_continuity_recovery_artifacts WHERE id=$1 AND tenant_id=$2",
                    _uuid.UUID(recovery_id), _uuid.UUID(tenant_id),
                )
                if existing is None:
                    raise PersistenceError("PERSISTENCE_DB_ERROR", "recovery identity conflict without a readable row") from exc
                existing_dict = dict(existing)
                for key, value in intended.items():
                    existing_value = existing_dict[key]
                    if isinstance(existing_value, _uuid.UUID):
                        existing_value = str(existing_value)
                    if existing_value != value:
                        raise PersistenceError("IDEMPOTENCY_CONFLICT", f"retry conflicts on {key}") from exc
                record = existing
            return _row(record)

        try:
            if _conn is not None:
                async with _conn.transaction():
                    return await _do(_conn)
            async with self._pool.acquire() as conn, conn.transaction():
                return await _do(conn)
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    async def _recovery_append_audit(self, tenant_id: str, recovery_id: str, event: dict, *, _conn=None) -> dict | None:
        """Atomically appends one audit entry only while ``access_audit`` is still under its
        100-entry bound. Returns ``None`` when the row does not exist; raises
        ``PersistenceError("AUDIT_FULL", ...)`` -- a distinguishable capacity miss, never
        confused with not-found -- when the row exists but is already at capacity."""
        async def _do(conn):
            await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
            record = await conn.fetchrow(
                """UPDATE narasi_continuity_recovery_artifacts
                   SET access_audit = access_audit || $3::jsonb
                   WHERE id=$1 AND tenant_id=$2 AND jsonb_array_length(access_audit) < 100
                   RETURNING *""",
                _uuid.UUID(recovery_id), _uuid.UUID(tenant_id), [event],
            )
            if record is not None:
                return _row(record)
            exists = await conn.fetchval(
                "SELECT 1 FROM narasi_continuity_recovery_artifacts WHERE id=$1 AND tenant_id=$2",
                _uuid.UUID(recovery_id), _uuid.UUID(tenant_id),
            )
            if exists is None:
                return None
            raise PersistenceError("AUDIT_FULL", "access audit is at its bound")
        try:
            if _conn is not None:
                async with _conn.transaction():
                    return await _do(_conn)
            async with self._pool.acquire() as conn, conn.transaction():
                return await _do(conn)
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    async def _recovery_delete(self, tenant_id: str, recovery_id: str, event: dict, *, _conn=None) -> dict | None:
        """Atomically appends one audit entry and marks the row deleted (idempotently -- an
        already-deleted row keeps its original ``deleted_at``) only while ``access_audit`` is
        still under its 100-entry bound. Same not-found/capacity-miss distinction as
        ``_recovery_append_audit``."""
        async def _do(conn):
            await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
            record = await conn.fetchrow(
                """UPDATE narasi_continuity_recovery_artifacts
                   SET access_audit = access_audit || $3::jsonb, deleted_at = COALESCE(deleted_at, now())
                   WHERE id=$1 AND tenant_id=$2 AND jsonb_array_length(access_audit) < 100
                   RETURNING *""",
                _uuid.UUID(recovery_id), _uuid.UUID(tenant_id), [event],
            )
            if record is not None:
                return _row(record)
            exists = await conn.fetchval(
                "SELECT 1 FROM narasi_continuity_recovery_artifacts WHERE id=$1 AND tenant_id=$2",
                _uuid.UUID(recovery_id), _uuid.UUID(tenant_id),
            )
            if exists is None:
                return None
            raise PersistenceError("AUDIT_FULL", "access audit is at its bound")
        try:
            if _conn is not None:
                async with _conn.transaction():
                    return await _do(_conn)
            async with self._pool.acquire() as conn, conn.transaction():
                return await _do(conn)
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    async def _recovery_due(self, tenant_id: str, *, cutoff, limit: int) -> list:
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
                rows = await conn.fetch(
                    """SELECT id FROM narasi_continuity_recovery_artifacts
                       WHERE tenant_id=$1 AND deleted_at IS NULL AND expires_at<=$2
                       ORDER BY expires_at, id LIMIT $3""",
                    _uuid.UUID(tenant_id), cutoff, limit,
                )
                return [str(row["id"]) for row in rows]
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc

    async def _recovery_live_object_locators(self, tenant_id: str) -> set:
        """The set of complete ``(object_bucket, object_key, object_version)`` locators for this
        tenant's non-deleted recovery rows -- tenant-scoped and RLS-safe (unlike a bare
        cross-tenant lookup, which RLS would just silently empty). ``sweep_orphans`` is itself
        tenant-scoped (Section 9), so this is the correct and sufficient live-metadata membership
        test for it: an object under this tenant's own key prefix is an orphan candidate exactly
        when its full locator is absent here, including when the tenant row itself (and therefore
        every cascade-deleted metadata row) no longer exists at all. The full locator -- not the
        key alone -- distinguishes a live current version from a stale prior version sharing the
        same key on a versioned store."""
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", tenant_id)
                rows = await conn.fetch(
                    """SELECT object_bucket, object_key, object_version
                       FROM narasi_continuity_recovery_artifacts WHERE tenant_id=$1 AND deleted_at IS NULL""",
                    _uuid.UUID(tenant_id),
                )
                return {(row["object_bucket"], row["object_key"], row["object_version"]) for row in rows}
        except Exception as exc:
            raise PersistenceError("PERSISTENCE_DB_ERROR", "an unexpected database error occurred") from exc


__all__ = ["ContinuityRepository", "PersistenceError"]
