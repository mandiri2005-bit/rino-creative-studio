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
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional

import canon_lite as _cl
import canon_lite_l2 as _l2
from canon_lite_qc_contract import (
    QC_PROVIDER_CODES,
    QC_RETRY_REASON_CODES,
    QC_RETRY_REASON_ADDRESS_INVALID,
    QC_RETRY_REASON_NONE,
    QC_RETRY_REASON_PROVIDER_UNPARSEABLE,
    QC_RETRY_REASON_SCHEMA_INVALID,
)

# 🔴 BOUNDED TELEMETRY, RATIFIED SCOPE. A failing extraction used to leave NO trace at
#    all: the provider exception was discarded by design, replaced with a coverage state,
#    and never logged. Three canaries were spent establishing that nine attempts had
#    failed — a fact the arithmetic `logical_attempts=9` for `units=3` carried and nothing
#    else did. Silence read as calm.
#
#    What may appear on this logger is fixed and closed: `unit_index`, `attempt_ordinal`,
#    and a member of the coverage, parser-reason, or provider-code vocabularies. Never the
#    prompt, response, quote, narration, tenant id, credential, or raw exception. The
#    extractor reads only an adapter-minted `.code` that it revalidates against the inert
#    contract; provider text cannot become a log value.
log = logging.getLogger("canon-lite-extractor")


EXTRACTOR_VERSION = _cl.L2_EXTRACTOR_VERSION

# DG-4: `prompt_sha256` is now an INJECTED parameter of extract_all, not a module
# constant. The old constant digested a four-key descriptor, so the actual prompt text
# could change completely without moving the hash — every claim artefact's provenance
# attested to a descriptor, not to what was sent. The ratified replacement hashes the
# derived static request contract, which lives in canon_lite_qc_provider.
#
# It is injected rather than imported because §4.5 forbids this module importing the QC
# adapter: the extractor ships to hosts where the provider module must never be imported
# at all (§6.0a), and importing it here would invert that. Injection also keeps L2a
# provider-agnostic, which is the whole point of this layer.
#
# There is deliberately NO default. A default is exactly how the wrong hash would ship
# silently into provenance that no downstream check could detect.
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
    atom_table_sha256: str
    attempt: int
    chapter_bytes: bytes = field(repr=False)
    chapter_atoms: tuple[_l2.ChapterAtomV1, ...] = field(repr=False)
    canon: _cl.CanonLiteV1 = field(repr=False)
    retry_reason: str = QC_RETRY_REASON_NONE
    meter_unit_index: Optional[int] = None
    meter_attempt_ordinal: Optional[int] = None

    def __post_init__(self) -> None:
        if isinstance(self.chapter_index, bool) or not isinstance(
                self.chapter_index, int) or self.chapter_index < 0:
            raise _schema_error("chapter_index: expected a non-negative int")
        if self.chapter_id != _cl.UNKNOWN:
            _cl._req_id(self.chapter_id, "chapter_id")
        _cl._req_sha256(self.content_sha256, "content_sha256")
        _cl._req_sha256(self.canon_sha256, "canon_sha256")
        _cl._req_sha256(self.atom_table_sha256, "atom_table_sha256")
        if isinstance(self.attempt, bool) or not isinstance(
                self.attempt, int) or not (1 <= self.attempt <= MAX_LOGICAL_ATTEMPTS):
            raise _schema_error("attempt: outside the logical-attempt ceiling")
        if type(self.retry_reason) is not str \
                or self.retry_reason not in QC_RETRY_REASON_CODES:
            raise _schema_error("retry_reason: outside the closed retry vocabulary")
        if not isinstance(self.chapter_bytes, bytes):
            raise _schema_error("chapter_bytes: expected bytes")
        if not isinstance(self.canon, _cl.CanonLiteV1):
            raise _schema_error("canon: expected CanonLiteV1")
        if not isinstance(self.chapter_atoms, tuple) or any(
                not isinstance(atom, _l2.ChapterAtomV1) for atom in self.chapter_atoms):
            raise _schema_error("chapter_atoms: expected tuple[ChapterAtomV1, ...]")
        atoms, atom_sha = _l2.build_chapter_atom_table(self.chapter_bytes)
        if self.chapter_atoms != atoms or self.atom_table_sha256 != atom_sha:
            raise _schema_error("chapter_atoms: do not bind chapter_bytes")
        meter_unit = self.chapter_index if self.meter_unit_index is None \
            else self.meter_unit_index
        meter_attempt = self.attempt if self.meter_attempt_ordinal is None \
            else self.meter_attempt_ordinal
        if type(meter_unit) is not int or not (0 <= meter_unit < 10_000):
            raise _schema_error("meter_unit_index: outside 0..9999")
        if type(meter_attempt) is not int or not (1 <= meter_attempt < 1_000):
            raise _schema_error("meter_attempt_ordinal: outside 1..999")
        object.__setattr__(self, "meter_unit_index", meter_unit)
        object.__setattr__(self, "meter_attempt_ordinal", meter_attempt)


ProviderCall = Callable[[ExtractionRequestV1], Awaitable[Mapping[str, Any]]]


class ExtractionWaveToken:
    """One-wave-per-job guard. A job creates one token and hands it to extract_all.

    The max_inflight bound is `replicas × jobs-per-worker × extractor_concurrency`, which
    holds only if a job runs ONE extraction wave at a time. A second concurrent wave under
    the same job would double the real in-flight count while the derived ceiling stayed
    put — the meter would then be bounded by a number that no longer describes reality.
    Claiming is one-shot and not reusable, so a second wave raises rather than silently
    over-subscribing.
    """

    __slots__ = ("_claimed",)

    def __init__(self) -> None:
        self._claimed = False

    @property
    def claimed(self) -> bool:
        return self._claimed

    def claim(self) -> None:
        if self._claimed:
            raise _schema_error("extractor_wave_already_claimed")
        self._claimed = True


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
    prompt_sha256: str,
    canon_sha256: str,
) -> _l2.ChapterClaimsV1:
    block = snapshot.blocks[index]
    _atoms, atom_sha = _l2.build_chapter_atom_table(snapshot.block_bytes(index))
    return _l2.ChapterClaimsV1(
        schema_version=_l2.CLAIMS_SCHEMA_VERSION,
        chapter_index=index,
        chapter_id=block.chapter_id,
        content_sha256=block.content_sha256,
        # Failure artefacts name the canon too, not only success payloads — a failed
        # extraction must still say which canon it failed against.
        canon_sha256=canon_sha256,
        atom_table_sha256=atom_sha,
        extractor_version=EXTRACTOR_VERSION,
        model_version=model_version,
        prompt_sha256=prompt_sha256,
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
    prompt_sha256: str,
    canon_sha256: str,
) -> dict[str, Any]:
    _cl._reject_unknown_fields(raw, ("coverage", "claims"), "provider_output")
    block = snapshot.blocks[index]
    _atoms, atom_sha = _l2.build_chapter_atom_table(snapshot.block_bytes(index))
    return {
        "schema_version": _l2.CLAIMS_SCHEMA_VERSION,
        "chapter_index": index,
        "chapter_id": block.chapter_id,
        "content_sha256": block.content_sha256,
        "canon_sha256": canon_sha256,
        "atom_table_sha256": atom_sha,
        "extractor_version": EXTRACTOR_VERSION,
        "model_version": model_version,
        "prompt_sha256": prompt_sha256,
        "predicate_set_version": _l2.PREDICATE_SET_VERSION,
        "coverage": raw["coverage"],
        "claims": raw["claims"],
    }


def _extraction_reason_code(exc: BaseException) -> str:
    """Bounded, closed reason for ONE failed attempt's log line — never the exception
    itself (canon_lite_l2's §10.3, and this module's own "fixed and closed... never a raw
    exception" telemetry scope).

    A specific `.reason_code` — set only at the handful of claims-loop sites in
    `parse_chapter_claims` that can name a precise cause — wins. Any other schema/bounds
    refusal (a structural check earlier in `parse_chapter_claims`, or `_provider_payload`'s
    own `_reject_unknown_fields`) is `SCHEMA_INVALID`. Anything else — a bug neither of
    those anticipated — is `OTHER`. Every branch returns an `_l2.EXTRACT_REASON_CODES`
    member, so the log line's vocabulary stays closed no matter what future code raises.
    """
    code = getattr(exc, "reason_code", None)
    if code in _l2.EXTRACT_REASON_CODES:
        return code
    if isinstance(exc, (_cl.CanonSchemaError, _cl.CanonBoundsError)):
        return _l2.EXTRACT_REASON_SCHEMA_INVALID
    return _l2.EXTRACT_REASON_OTHER


def _provider_error_code(exc: BaseException) -> Optional[str]:
    """Return only a code minted by the closed QC provider vocabulary.

    The provider exception class itself cannot be imported here: importing the adapter on
    a non-metering host violates its lazy-import boundary. The inert contract module owns
    the vocabulary shared by both sides; an arbitrary exception carrying any other value
    is refused and remains the generic PROVIDER_FAILURE coverage state.
    """
    code = getattr(exc, "code", None)
    return code if type(code) is str and code in QC_PROVIDER_CODES else None


def _retry_reason(error_code: str) -> str:
    """Map one closed failure code to bounded prompt feedback for the next attempt."""
    if error_code in (
            _l2.EXTRACT_REASON_ATOM_INDEX_OUT_OF_RANGE,
            _l2.EXTRACT_REASON_ATOM_SPAN_INVERTED,
            _l2.EXTRACT_REASON_ATOM_TABLE_MISMATCH,
    ):
        return QC_RETRY_REASON_ADDRESS_INVALID
    if error_code == "qc_provider_unparseable":
        return QC_RETRY_REASON_PROVIDER_UNPARSEABLE
    if error_code in (
            _l2.EXTRACT_REASON_SCHEMA_INVALID,
            _l2.EXTRACT_REASON_CANON_REF_INVALID,
            _l2.EXTRACT_REASON_OTHER,
            "qc_provider_schema_violation",
    ):
        return QC_RETRY_REASON_SCHEMA_INVALID
    return QC_RETRY_REASON_NONE


def _preflight_request(
    request: ExtractionRequestV1,
    *,
    snapshot: _l2.FinalChapterSnapshotV1,
    index: int,
    canon: _cl.CanonLiteV1,
) -> None:
    """Step 4 of the frozen order: content, identity and request-canon parity.

    Runs with `snapshot` and `block` in scope, and therefore BEFORE MeteredProvider —
    before sink.begin(), before any meter row exists, and before any HTTP request. This
    is the ONLY layer that can promise zero meter rows: MeteredProvider calls
    sink.begin() before invoking the adapter, so an adapter-level rejection already has a
    row open. The adapter keeps the same canon checks as defence in depth, not as the
    primary guard.

    These raise through the module's existing path (_schema_error -> CanonSchemaError).
    No new exception class is introduced, and QcProviderError is never raised here —
    importing it would invert the dependency direction.
    """
    block = snapshot.blocks[index]
    if _cl.sha256_hex(request.chapter_bytes) != request.content_sha256:
        raise _schema_error("extractor_request_content_hash_mismatch")
    atoms, atom_sha = _l2.build_chapter_atom_table(request.chapter_bytes)
    if request.chapter_atoms != atoms or request.atom_table_sha256 != atom_sha:
        raise _schema_error("extractor_request_atom_table_mismatch")
    if request.chapter_index != index or request.chapter_id != block.chapter_id:
        raise _schema_error("extractor_request_identity_mismatch")
    # Request-canon parity — three conjuncts, all required. Content and identity bind the
    # CHAPTER; without these the request's CANON is unchecked at the only layer that can
    # promise zero meter rows.
    #   1. the request names the same canon it was given;
    #   2. request.canon is itself self-consistent — NOT redundant with the step-2 check,
    #      which verified the `canon` ARGUMENT: request.canon is a separate reference that
    #      construction could have replaced, re-bound or mutated, and assuming they are
    #      the same object is exactly the assumption a parity check exists to refuse;
    #   3. the projection actually serialized carries the hash the request advertises, so
    #      the bytes sent and the provenance recorded cannot describe different canons.
    if request.canon_sha256 != canon.canon_sha256:
        raise _schema_error("extractor_request_canon_mismatch")
    if not request.canon.verify_sha256():
        raise _schema_error("extractor_request_canon_mismatch")
    if request.canon.to_canonical_obj(include_hash=True).get("canon_sha256") \
            != request.canon_sha256:
        raise _schema_error("extractor_request_canon_mismatch")


async def extract_all(
    snapshot: _l2.FinalChapterSnapshotV1,
    canon: Optional[_cl.CanonLiteV1],
    *,
    provider: ProviderCall,
    model_version: str,
    prompt_sha256: str,
    max_concurrency: int,
    max_attempts: int = MAX_LOGICAL_ATTEMPTS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    wave_token: Optional["ExtractionWaveToken"] = None,
) -> ExtractionRunV1:
    """Extract every exact chapter version, bounded and fully drained.

    A canon with no semantic authority short-circuits to ``NO_CANON_AUTHORITY`` and makes
    zero provider calls. This is both the honest D-L2-5 result and a load-bearing cost
    guard: Wimba must not pay to extract claims that no accepted authority can evaluate.
    """
    if not isinstance(snapshot, _l2.FinalChapterSnapshotV1):
        raise _schema_error("snapshot: expected FinalChapterSnapshotV1")
    # ---- FROZEN ORDER, step 1: canon TYPE -----------------------------------
    if canon is not None and not isinstance(canon, _cl.CanonLiteV1):
        raise _schema_error("canon: expected CanonLiteV1 or None")
    # ---- step 2: canon SELF-CONSISTENCY, before semantic authority ----------
    # An invalid canon fails LOUDLY; it is never downgraded to a no-authority result.
    # This cannot live after request construction: the NO_CANON_AUTHORITY branch returns
    # before any ExtractionRequestV1 exists, so a check placed later would never run on
    # that path and an invalid canon would be silently turned into a no-authority
    # artefact.
    if canon is not None and not canon.verify_sha256():
        raise _schema_error("extractor_canon_hash_invalid")
    _cl._req_str(model_version, "model_version", max_len=_l2.MAX_LABEL_LEN)
    _cl._req_sha256(prompt_sha256, "prompt_sha256")
    if wave_token is not None and not isinstance(wave_token, ExtractionWaveToken):
        raise _schema_error("wave_token: expected ExtractionWaveToken or None")
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

    # One extraction WAVE per job, or the max_inflight formula undercounts: the bound is
    # replicas × jobs-per-worker × extractor_concurrency, which assumes a job holds at
    # most one wave open at a time. A second concurrent wave would double the real
    # in-flight count while the ceiling stayed put.
    if wave_token is not None:
        wave_token.claim()

    # ---- step 3: the None / no-authority short-circuit ----------------------
    # Now running on a canon already known to be self-consistent. The two cases differ in
    # PROVENANCE, not outcome: canon=None has genuinely no canon to name, while a valid
    # canon lacking semantic authority exists and is nameable — only its AUTHORITY is
    # absent, so it carries its real hash rather than UNKNOWN.
    if canon is None or not _l2.has_semantic_authority(canon):
        short_circuit_canon_sha = _cl.UNKNOWN if canon is None else canon.canon_sha256
        artifacts = tuple(
            _failure_artifact(
                snapshot, index, state=_l2.COVERAGE_NO_CANON_AUTHORITY,
                model_version="not_called", prompt_sha256=prompt_sha256,
                canon_sha256=short_circuit_canon_sha)
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
        error_code = _l2.COVERAGE_PROVIDER_FAILURE
        retry_reason = QC_RETRY_REASON_NONE
        block = snapshot.blocks[index]
        chapter_bytes = snapshot.block_bytes(index)
        chapter_atoms, atom_table_sha = _l2.build_chapter_atom_table(chapter_bytes)
        for attempt in range(1, max_attempts + 1):
            # ---- step 4: construct, then parity-check BEFORE MeteredProvider ----
            request = ExtractionRequestV1(
                chapter_index=index, chapter_id=block.chapter_id,
                content_sha256=block.content_sha256, canon_sha256=canon.canon_sha256,
                atom_table_sha256=atom_table_sha, attempt=attempt,
                chapter_bytes=chapter_bytes, chapter_atoms=chapter_atoms, canon=canon,
                retry_reason=retry_reason, meter_unit_index=index,
                meter_attempt_ordinal=attempt)
            _preflight_request(request, snapshot=snapshot, index=index, canon=canon)
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
                error_code = _l2.COVERAGE_TIMEOUT
            except Exception as exc:
                # The durable coverage state remains the fixed PROVIDER_FAILURE value, but
                # an adapter-minted CLOSED code is preserved for the bounded log and next
                # retry. The exception message/body is never inspected or retained.
                terminal_state = _l2.COVERAGE_PROVIDER_FAILURE
                error_code = _provider_error_code(exc) or _l2.COVERAGE_PROVIDER_FAILURE
            else:
                try:
                    payload = _provider_payload(
                        raw, snapshot=snapshot, index=index,
                        model_version=model_version, prompt_sha256=prompt_sha256,
                        canon_sha256=canon.canon_sha256)
                    return _l2.parse_chapter_claims(
                        payload, snapshot=snapshot, canon=canon)
                except Exception as exc:
                    terminal_state = _l2.COVERAGE_INVALID_EXTRACTOR_OUTPUT
                    error_code = _extraction_reason_code(exc)
            retry_reason = _retry_reason(error_code)
            # Reached only when this attempt did NOT return: the success path above
            # returns from inside the `else`. One line per failed attempt, three bounded
            # fields, nothing else. `error_code` narrows INVALID_EXTRACTOR_OUTPUT to WHICH
            # parser-side rule rejected the attempt and preserves an adapter-minted closed
            # provider code; unknown exceptions remain the generic PROVIDER_FAILURE.
            log.warning("canon lite qc extract: attempt failed "
                        "(unit_index=%d attempt_ordinal=%d error_code=%s)",
                        index, attempt, error_code)
        return _failure_artifact(
            snapshot, index, state=terminal_state, model_version=model_version,
            prompt_sha256=prompt_sha256, canon_sha256=canon.canon_sha256)

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
