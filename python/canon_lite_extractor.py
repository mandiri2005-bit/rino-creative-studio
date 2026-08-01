"""Canon Lite L2a — bounded extraction lifecycle with an injected provider.

This module deliberately contains no production provider, network, database, metering,
credit, or pricing import. L2a proves the concurrency, retry, timeout, cancellation,
drain, and strict-conversion contract offline. L2B-METER must supply the real provider
and accounting-safe physical-attempt sink before the first production call.

The injected provider receives an ephemeral request containing exact chapter bytes and
the accepted canon. It returns only ``coverage`` and ``claims``. Server-owned identity,
hash, and version fields are attached after the call, so untrusted output cannot choose
which bytes or software contract it claims to describe.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional

import canon_lite as _cl
import canon_lite_l2 as _l2


EXTRACTOR_VERSION = _cl.L2_EXTRACTOR_VERSION
PROMPT_SHA256 = _cl._digest(
    "canon_lite_l2.extractor_prompt",
    {
        "version": "v1",
        "output_fields": ("coverage", "claims"),
        "evidence_unit": "utf8_byte_offset",
        "authority_rule": "accepted_canon_ids_only",
    },
)
MAX_CONCURRENCY = 8
MAX_LOGICAL_ATTEMPTS = 3
DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 120.0

PHYSICAL_ATTEMPTS_UNKNOWN = "PHYSICAL_ATTEMPTS_UNKNOWN"


def _schema_error(message: str) -> Exception:
    return _cl.CanonSchemaError(message)


@dataclass(frozen=True, slots=True)
class ExtractionRequestV1:
    """One ephemeral logical extraction request. Its repr never contains tenant prose."""

    chapter_index: int
    chapter_id: str
    content_sha256: str
    canon_sha256: str
    attempt: int
    chapter_bytes: bytes = field(repr=False)
    canon: _cl.CanonLiteV1 = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.chapter_index, bool) or not isinstance(
                self.chapter_index, int) or self.chapter_index < 0:
            raise _schema_error("chapter_index: expected a non-negative int")
        if self.chapter_id != _cl.UNKNOWN:
            _cl._req_id(self.chapter_id, "chapter_id")
        _cl._req_sha256(self.content_sha256, "content_sha256")
        _cl._req_sha256(self.canon_sha256, "canon_sha256")
        if isinstance(self.attempt, bool) or not isinstance(
                self.attempt, int) or not (1 <= self.attempt <= MAX_LOGICAL_ATTEMPTS):
            raise _schema_error("attempt: outside the logical-attempt ceiling")
        if not isinstance(self.chapter_bytes, bytes):
            raise _schema_error("chapter_bytes: expected bytes")
        if not isinstance(self.canon, _cl.CanonLiteV1):
            raise _schema_error("canon: expected CanonLiteV1")


ProviderCall = Callable[[ExtractionRequestV1], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class ExtractionRunV1:
    """Bounded lifecycle evidence. No provider error or tenant identifier is retained."""

    claims: tuple[_l2.ChapterClaimsV1, ...]
    logical_extractions: int
    logical_attempts: int
    max_concurrency_observed: int
    tasks_started: int
    tasks_drained: int
    live_tasks_after: int
    physical_attempts_state: str

    def __post_init__(self) -> None:
        if not isinstance(self.claims, tuple):
            raise _schema_error("claims: expected tuple")
        if tuple(c.chapter_index for c in self.claims) != tuple(range(len(self.claims))):
            raise _schema_error("claims: expected one canonical-order artifact per chapter")
        for name in (
            "logical_extractions", "logical_attempts", "max_concurrency_observed",
            "tasks_started", "tasks_drained", "live_tasks_after",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _schema_error(f"{name}: expected a non-negative int")
        if self.logical_attempts > self.logical_extractions * MAX_LOGICAL_ATTEMPTS:
            raise _schema_error("logical_attempts: exceeds absolute ceiling")
        if self.max_concurrency_observed > MAX_CONCURRENCY:
            raise _schema_error("max_concurrency_observed: exceeds module ceiling")
        if self.tasks_drained != self.tasks_started or self.live_tasks_after != 0:
            raise _schema_error("task lifecycle: every started task must be physically drained")
        if self.physical_attempts_state != PHYSICAL_ATTEMPTS_UNKNOWN:
            raise _schema_error("physical_attempts_state: expected explicit UNKNOWN state")

    @property
    def claims_by_index(self) -> dict[int, _l2.ChapterClaimsV1]:
        return {artifact.chapter_index: artifact for artifact in self.claims}


def _coverage(state: str) -> tuple[_l2.PredicateCoverageV1, ...]:
    return tuple(
        _l2.PredicateCoverageV1(predicate=predicate, state=state)
        for predicate in _l2.SEMANTIC_PREDICATES
    )


def _failure_artifact(
    snapshot: _l2.FinalChapterSnapshotV1,
    index: int,
    *,
    state: str,
    model_version: str,
) -> _l2.ChapterClaimsV1:
    block = snapshot.blocks[index]
    return _l2.ChapterClaimsV1(
        schema_version=_l2.CLAIMS_SCHEMA_VERSION,
        chapter_index=index,
        chapter_id=block.chapter_id,
        content_sha256=block.content_sha256,
        extractor_version=EXTRACTOR_VERSION,
        model_version=model_version,
        prompt_sha256=PROMPT_SHA256,
        predicate_set_version=_l2.PREDICATE_SET_VERSION,
        coverage=_coverage(state),
        claims=(),
    )


def _provider_payload(
    raw: Mapping[str, Any],
    *,
    snapshot: _l2.FinalChapterSnapshotV1,
    index: int,
    model_version: str,
) -> dict[str, Any]:
    _cl._reject_unknown_fields(raw, ("coverage", "claims"), "provider_output")
    block = snapshot.blocks[index]
    return {
        "schema_version": _l2.CLAIMS_SCHEMA_VERSION,
        "chapter_index": index,
        "chapter_id": block.chapter_id,
        "content_sha256": block.content_sha256,
        "extractor_version": EXTRACTOR_VERSION,
        "model_version": model_version,
        "prompt_sha256": PROMPT_SHA256,
        "predicate_set_version": _l2.PREDICATE_SET_VERSION,
        "coverage": raw["coverage"],
        "claims": raw["claims"],
    }


async def extract_all(
    snapshot: _l2.FinalChapterSnapshotV1,
    canon: Optional[_cl.CanonLiteV1],
    *,
    provider: ProviderCall,
    model_version: str,
    max_concurrency: int,
    max_attempts: int = MAX_LOGICAL_ATTEMPTS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ExtractionRunV1:
    """Extract every exact chapter version, bounded and fully drained.

    A canon with no semantic authority short-circuits to ``NO_CANON_AUTHORITY`` and makes
    zero provider calls. This is both the honest D-L2-5 result and a load-bearing cost
    guard: Wimba must not pay to extract claims that no accepted authority can evaluate.
    """
    if not isinstance(snapshot, _l2.FinalChapterSnapshotV1):
        raise _schema_error("snapshot: expected FinalChapterSnapshotV1")
    if canon is not None and not isinstance(canon, _cl.CanonLiteV1):
        raise _schema_error("canon: expected CanonLiteV1 or None")
    _cl._req_str(model_version, "model_version", max_len=_l2.MAX_LABEL_LEN)
    if not callable(provider):
        raise _schema_error("provider: expected an async callable")
    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) \
            or not (1 <= max_concurrency <= MAX_CONCURRENCY):
        raise _schema_error(f"max_concurrency: expected int in 1..{MAX_CONCURRENCY}")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) \
            or not (1 <= max_attempts <= MAX_LOGICAL_ATTEMPTS):
        raise _schema_error(
            f"max_attempts: expected int in 1..{MAX_LOGICAL_ATTEMPTS}")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) \
            or not math.isfinite(float(timeout_s)) \
            or not (0 < float(timeout_s) <= MAX_TIMEOUT_S):
        raise _schema_error(
            f"timeout_s: expected a finite number in (0, {MAX_TIMEOUT_S}]")

    if canon is None or not _l2.has_semantic_authority(canon):
        artifacts = tuple(
            _failure_artifact(
                snapshot, index, state=_l2.COVERAGE_NO_CANON_AUTHORITY,
                model_version="not_called")
            for index in range(snapshot.chapter_count)
        )
        return ExtractionRunV1(
            claims=artifacts, logical_extractions=0, logical_attempts=0,
            max_concurrency_observed=0, tasks_started=0, tasks_drained=0,
            live_tasks_after=0, physical_attempts_state=PHYSICAL_ATTEMPTS_UNKNOWN)

    semaphore = asyncio.Semaphore(max_concurrency)
    active = 0
    observed = 0
    attempts = 0
    counter_lock = asyncio.Lock()

    async def one(index: int) -> _l2.ChapterClaimsV1:
        nonlocal active, observed, attempts
        terminal_state = _l2.COVERAGE_PROVIDER_FAILURE
        block = snapshot.blocks[index]
        for attempt in range(1, max_attempts + 1):
            request = ExtractionRequestV1(
                chapter_index=index, chapter_id=block.chapter_id,
                content_sha256=block.content_sha256, canon_sha256=canon.canon_sha256,
                attempt=attempt, chapter_bytes=snapshot.block_bytes(index), canon=canon)
            try:
                async with semaphore:
                    async with counter_lock:
                        active += 1
                        observed = max(observed, active)
                        attempts += 1
                    try:
                        raw = await asyncio.wait_for(provider(request), timeout=float(timeout_s))
                    finally:
                        async with counter_lock:
                            active -= 1
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                terminal_state = _l2.COVERAGE_TIMEOUT
            except Exception:
                # Fixed bounded state only. The provider exception never enters a report,
                # log, fixture, or default result payload.
                terminal_state = _l2.COVERAGE_PROVIDER_FAILURE
            else:
                try:
                    payload = _provider_payload(
                        raw, snapshot=snapshot, index=index,
                        model_version=model_version)
                    return _l2.parse_chapter_claims(
                        payload, snapshot=snapshot, canon=canon)
                except Exception:
                    terminal_state = _l2.COVERAGE_INVALID_EXTRACTOR_OUTPUT
        return _failure_artifact(
            snapshot, index, state=terminal_state, model_version=model_version)

    tasks = [
        asyncio.create_task(one(index), name=f"canon-l2-extract-{index}")
        for index in range(snapshot.chapter_count)
    ]
    try:
        artifacts = tuple(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        # No drain timeout: returning while a provider task is physically alive would be
        # a false drain and violate C10. A cancellation-hostile provider may delay this
        # caller, but it cannot mutate evidence after terminalization.
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    drained = sum(task.done() for task in tasks)
    live = sum(not task.done() for task in tasks)
    return ExtractionRunV1(
        claims=artifacts,
        logical_extractions=snapshot.chapter_count,
        logical_attempts=attempts,
        max_concurrency_observed=observed,
        tasks_started=len(tasks),
        tasks_drained=drained,
        live_tasks_after=live,
        physical_attempts_state=PHYSICAL_ATTEMPTS_UNKNOWN,
    )
