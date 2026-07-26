"""Continuity Core CC-02 -- trusted, dormant, default-off shadow integration adapter.

This module wires the frozen CC-01 deterministic core into the real Scenario-A/B chapter
pipeline as a report-only observer. It owns an internal trusted-run factory registry (never
installed by an HTTP/job request payload), a ContextVar carrying the current run across the
real generation call chain, and the trusted chapter-observation / MAP-seal / final-bundle
lifecycle. It never builds a Bible V2 with an LLM, never calls a claims provider itself, never
persists anything, and never enables production traffic or enforcement -- see
INTEGRATION-CONTRACT.md. Zero provider/network/database import; the trusted Claims extractor is
always supplied by the caller's registered factory, never invoked here directly by name.

Process-owned provenance (v3 rework): factory-created runs and internally-created boundary
objects are tracked by object identity in module-private weak sets, never by an instance
attribute. An instance attribute can always be forged by direct construction or subclassing --
a `weakref.WeakSet` populated only inside `new_trusted_run`/`TrustedRun._boundary` cannot.
"""
from __future__ import annotations

import contextvars
import os
import uuid
import weakref
from typing import Any, Optional

from .compiler import compile_story_contract_v3, contract_hash as _contract_hash
from .gates import post_generation_gate
from .ledger import commit_chapter, invalidate_from_chapter, new_ledger
from .models import canonical_json, deep_freeze_copy, sha256_hex
from .schema import validate_bible_v2
from .slicer import compile_chapter_brief

_SHADOW_ENV_VAR = "NARASI_CONTINUITY_CORE_SHADOW"
_SHADOW_ON_VALUES = {"1", "true", "yes", "on"}
_RESERVED_REQUEST_KEYS = frozenset({
    "_continuity_run", "_continuity_factory", "continuity_contract",
    "continuity_bible_v2", "continuity_claims", "continuity_gate_result",
})
_SCHEMA_VERSIONS = {"integration": "2", "story_contract": "3", "continuity_ledger": "1"}

_current_run_var: "contextvars.ContextVar[Optional[TrustedRun]]" = contextvars.ContextVar(
    "narasi_continuity_current_run", default=None
)
_trusted_run_factory = None

# Process-owned provenance registries -- populated ONLY inside new_trusted_run/_boundary, never
# exposed, never settable via any instance attribute. Membership is by object identity (default
# object hashing), so a garbage-collected run's identity can never be replayed. THREE distinct
# capabilities, never merged into one set: a boundary object is installable (so the pipeline can
# still emit its bounded report) but must never be factory-eligible -- otherwise a boundary
# obtained from an earlier no-factory/error call could later be handed back by a (mis)configured
# factory and be accepted as a trusted result.
_FACTORY_CREATED_RUNS: "weakref.WeakSet" = weakref.WeakSet()
_BOUNDARY_RUNS: "weakref.WeakSet" = weakref.WeakSet()
_CLAIMED_RUNS: "weakref.WeakSet" = weakref.WeakSet()

# A fixed, content-free sentinel for a final chapter whose content is not an exact `str`. Never a
# real hash -- a real sha256 hex digest is always exactly 64 lowercase hex characters, so this can
# never coincidentally equal one -- and never derived from the value itself, so raw content
# (however large or structured) never reaches the report.
_FINAL_CONTENT_TYPE_MISMATCH = "_type_mismatch"


class ContinuityIntegrationError(Exception):
    """Raised for CC-02 integration-boundary violations: malformed identity, an out-of-range
    chapter index, a non-boolean ``resumed``, a malformed/out-of-order MAP chapter set, an
    attempt to mutate the sealed/one-shot lifecycle out of order, or a stale/cross-job final-
    bundle preparation attempt. Never raised for a missing/erroring/invalid/mismatched/reused
    trusted factory -- those resolve to a bounded incomplete report."""


def shadow_mode() -> bool:
    """Only the exact tokens 1/true/yes/on (case-insensitive) enable shadow. Anything else --
    unset, blank, malformed, unknown, or the literal word 'enforce' -- resolves to off. There is
    no enforce mode in CC-02."""
    value = os.environ.get(_SHADOW_ENV_VAR)
    if value is None:
        return False
    return value.strip().lower() in _SHADOW_ON_VALUES


def register_trusted_run_factory(factory) -> None:
    """Registers the ONLY callable ``begin_shadow_run`` will ever invoke. Must be called by
    process/startup code; never reachable from an HTTP/job request body."""
    global _trusted_run_factory
    _trusted_run_factory = factory


def clear_trusted_run_factory() -> None:
    global _trusted_run_factory
    _trusted_run_factory = None


def _is_factory_created_run(run: Any) -> bool:
    """Exact type (never a subclass) AND registered by new_trusted_run specifically -- an internal
    boundary object (unavailable/error/invalid/mismatch/reused) is deliberately EXCLUDED here even
    though it is installable, so a boundary returned by a factory can never be accepted as trusted.
    An instance attribute set after the fact (however plausibly named) can never satisfy this."""
    return type(run) is TrustedRun and run in _FACTORY_CREATED_RUNS


def _is_installable_run(run: Any) -> bool:
    """Exact type AND registered as EITHER a factory-created run or an internal boundary object --
    both may be installed on the ContextVar so the pipeline can observe/report either outcome."""
    return type(run) is TrustedRun and (run in _FACTORY_CREATED_RUNS or run in _BOUNDARY_RUNS)


def install_current_run(run: "TrustedRun"):
    if not _is_installable_run(run):
        raise ContinuityIntegrationError(
            "only a process-registered TrustedRun instance may be installed as the current run"
        )
    return _current_run_var.set(run)


def current_run() -> "Optional[TrustedRun]":
    return _current_run_var.get()


def reset_current_run(token) -> None:
    _current_run_var.reset(token)


def _require_nonblank_str(value: Any) -> bool:
    return type(value) is str and value.strip() != ""


def _validate_identity(identity: Any) -> dict:
    if not isinstance(identity, dict):
        raise ContinuityIntegrationError("identity must be an object")
    for key in ("tenant_id", "job_id"):
        if not _require_nonblank_str(identity.get(key)):
            raise ContinuityIntegrationError(f"identity.{key} must be a nonblank string")
    job_uuid = identity.get("job_uuid")
    if not _require_nonblank_str(job_uuid):
        raise ContinuityIntegrationError("identity.job_uuid must be a nonblank string")
    try:
        parsed = uuid.UUID(job_uuid)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ContinuityIntegrationError("identity.job_uuid must be a valid UUID string") from exc
    if str(parsed) != job_uuid:
        raise ContinuityIntegrationError("identity.job_uuid must be the canonical lowercase UUID string")
    return {"tenant_id": identity["tenant_id"], "job_id": identity["job_id"], "job_uuid": job_uuid}


def _scrub_reserved(value: Any) -> Any:
    """Recursively deep-copies ``value`` while removing every reserved key from mappings
    nested under mappings or lists at ANY depth -- not just the request root. Building a
    brand-new container at every level also guarantees the factory can never mutate the
    caller's original request object back."""
    if isinstance(value, dict):
        return {k: _scrub_reserved(v) for k, v in value.items() if k not in _RESERVED_REQUEST_KEYS}
    if isinstance(value, list):
        return [_scrub_reserved(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub_reserved(v) for v in value)
    return value


def _is_exact_int(value: Any) -> bool:
    """Exact built-in ``int`` -- never ``bool`` (``isinstance(True, int)`` is True in Python) and
    never an ``int`` subclass (a subclass could override arithmetic/equality). ``type(x) is int``
    excludes both in one check, so every integer-typed field here uses this helper instead of a
    bare ``isinstance``."""
    return type(value) is int


_FINAL_ID_INVALID = "_invalid_id"
_FINAL_NUMBER_INVALID = "_type_mismatch"


def _bounded_final_id(value: Any, known_ids: "set"):
    """Normalizes a final chapter's ``id`` field for exact comparison against the sealed string
    chapter_id. Only an exact ``str`` that is one of THIS run's own sealed/contract chapter IDs is
    safe to echo back into a report -- those are bounded, contract-derived identifiers, never
    manuscript prose. A wrong type (including a ``str`` subclass, which could carry a forged
    ``__eq__``) or any string not in the sealed set -- including arbitrary untrusted text -- becomes
    a fixed marker instead, so nothing invalid can ever leak into a violation."""
    if type(value) is str and value in known_ids:
        return value
    return _FINAL_ID_INVALID


def _final_chapter_number(value: Any):
    """Normalizes a final chapter's ``no`` field for exact comparison against the sealed
    integer chapter_number. A non-exact-int value (bool/float/str/None/int subclass) becomes a
    fixed bounded marker -- never the raw value -- so it can neither spuriously ``==`` a real int
    (Python's ``True == 1``/``1.0 == 1`` would otherwise let a type-confused value slip past a bare
    equality check) nor leak an untrusted value into any report that embeds this result."""
    if _is_exact_int(value):
        return value + 1
    return _FINAL_NUMBER_INVALID


def new_trusted_run(*, identity: dict, bible_v2: dict, claims_extractor) -> "TrustedRun":
    """The only way to construct a genuine, factory-acceptable trusted run. Validates identity,
    compiles the Story Contract v3 itself from the supplied Bible V2, and creates a fresh ledger
    -- the caller's ``claims_extractor`` is invoked later, once per chapter, never given the
    whole book. Registers the instance in the process-owned run registry so a direct
    ``TrustedRun(...)`` forgery (even with plausible marker attributes set afterward) can never
    pass ``begin_shadow_run``'s provenance check."""
    bound_identity = _validate_identity(identity)
    contract = compile_story_contract_v3(validate_bible_v2(bible_v2))
    ledger = new_ledger(contract)
    run = TrustedRun(identity=bound_identity, contract=contract, ledger=ledger, claims_extractor=claims_extractor)
    _FACTORY_CREATED_RUNS.add(run)
    return run


async def begin_shadow_run(*, identity: dict, request: dict) -> "Optional[TrustedRun]":
    """Flag off -> None, zero identity validation, zero factory lookup, zero call. Flag on ->
    validates/canonicalizes identity FIRST (raises on malformed identity, before any boundary
    report), then calls only the registered factory with a recursively scrubbed deep copy of
    ``request`` (every reserved key removed at every nesting level; the caller's own mapping,
    including nested structures, is never mutated). No factory, a factory exception, a factory
    return value that is not an exact process-owned trusted-run instance, a run whose own
    identity does not match, or a run already claimed by an earlier start each become a bounded,
    incomplete report -- never a caller-data fallback, never raw request/exception prose, never
    a raised exception."""
    if not shadow_mode():
        return None
    bound_identity = _validate_identity(identity)
    stripped_request = _scrub_reserved(request or {})
    factory = _trusted_run_factory
    if factory is None:
        return TrustedRun._boundary(identity=bound_identity, reason="trusted_factory_unavailable")
    try:
        # A defensive COPY -- bound_identity itself remains the sole authoritative value compared
        # below. A factory that clears/rewrites the dict it receives (accidentally or otherwise)
        # must never redefine the identity this call is authenticating against.
        made = factory(identity=dict(bound_identity), request=stripped_request)
    except Exception:  # noqa: BLE001 - a factory's own error prose must never leak
        return TrustedRun._boundary(identity=bound_identity, reason="trusted_factory_error")
    if not _is_factory_created_run(made):
        return TrustedRun._boundary(identity=bound_identity, reason="trusted_factory_invalid")
    if made.identity != bound_identity:
        return TrustedRun._boundary(identity=bound_identity, reason="trusted_factory_identity_mismatch")
    if made in _CLAIMED_RUNS:
        return TrustedRun._boundary(identity=bound_identity, reason="trusted_factory_reused")
    _CLAIMED_RUNS.add(made)
    return made


class TrustedRun:
    """One trusted continuity run bound to exactly one job attempt. Never accepts an external
    gate result, claims mapping, ledger, brief, contract, or receipt -- every one of those is
    produced internally from the Bible V2 and ledger this run itself owns. Coverage/validity are
    derived fresh from the current ledger and chapter reports on every access -- never a one-way
    latch -- so a retry-triggered invalidation or a later successful re-observation is always
    reflected truthfully. The MAP-seal / final-bundle lifecycle is monotonic and one-shot.
    Factory-created/claimed provenance lives OUTSIDE this instance (module-private weak sets);
    no instance attribute governs it, so no instance attribute can forge it."""

    def __init__(self, *, identity: dict, contract: Optional[dict] = None, ledger: Optional[dict] = None,
                 claims_extractor=None, _valid: bool = True, _reason: Optional[str] = None):
        self._identity = dict(identity) if isinstance(identity, dict) else {}
        self._attempt_id = uuid.uuid4()
        self._contract = contract
        self._ledger = ledger
        self._claims_extractor = claims_extractor
        self._valid = _valid
        self._reason = _reason
        self._final_prepared = False
        self._chapter_by_index: dict = (
            {c["chapter_number"] - 1: c for c in contract["chapter_contracts"]} if contract else {}
        )
        self._chapter_number_by_id: dict = (
            {c["chapter_id"]: c["chapter_number"] for c in contract["chapter_contracts"]} if contract else {}
        )
        self._pending: dict = {}
        self._chapter_reports_by_id: dict = {}
        self._map_seal: Optional[dict] = None

    @classmethod
    def _boundary(cls, *, identity: dict, reason: str) -> "TrustedRun":
        obj = cls(identity=identity if isinstance(identity, dict) else {}, _valid=False, _reason=reason)
        _BOUNDARY_RUNS.add(obj)
        return obj

    @property
    def identity(self) -> dict:
        """A defensive copy on every access -- mutating the returned mapping can never rebind
        this run's real identity."""
        return dict(self._identity)

    @property
    def attempt_id(self) -> uuid.UUID:
        """Read-only -- there is no setter, so ``run.attempt_id = ...`` raises AttributeError."""
        return self._attempt_id

    def _coverage_complete(self) -> bool:
        if self._contract is None or self._ledger is None:
            return False
        chapter_ids = set(self._chapter_number_by_id)
        if not chapter_ids:
            return False
        if set(self._ledger.get("commits") or {}) != chapter_ids:
            return False
        for cid in chapter_ids:
            row = self._chapter_reports_by_id.get(cid)
            if row is None or not row.get("ok"):
                return False
        return True

    def report(self) -> dict:
        valid = self._valid and self._coverage_complete()
        payload: dict = {
            "valid": valid,
            "coverage": "complete" if valid else "incomplete",
            "identity": self.identity,
            "attempt_id": str(self.attempt_id),
            "schema_versions": dict(_SCHEMA_VERSIONS),
        }
        if self._reason is not None:
            payload["reason"] = self._reason
        if self._contract is not None:
            payload["contract_hash"] = _contract_hash(self._contract)
        if self._ledger is not None:
            payload["ledger_hash"] = sha256_hex(canonical_json(self._ledger))
        ordered_ids = sorted(
            self._chapter_reports_by_id, key=lambda cid: self._chapter_number_by_id.get(cid, 0)
        )
        payload["chapters"] = [dict(self._chapter_reports_by_id[cid]) for cid in ordered_ids]
        return payload

    def ledger_snapshot(self) -> dict:
        return deep_freeze_copy(self._ledger) if self._ledger is not None else {}

    async def observe_chapter(self, *, chapter_index: int, chapter_text: str, resumed: bool = False) -> None:
        if self._map_seal is not None:
            raise ContinuityIntegrationError("observation is closed after the MAP seal for this run")
        if not _is_exact_int(chapter_index):
            raise ContinuityIntegrationError("chapter_index must be an integer, never a boolean or float")
        if resumed is not True and resumed is not False:
            raise ContinuityIntegrationError("resumed must be the exact boolean True or False")
        chapter = self._chapter_by_index.get(chapter_index)
        if chapter is None:
            raise ContinuityIntegrationError(f"chapter_index {chapter_index!r} is out of range")
        chapter_id = chapter["chapter_id"]
        chapter_number = chapter["chapter_number"]
        brief = compile_chapter_brief(self._contract, self._ledger, chapter_id)
        try:
            if self._claims_extractor is None:
                raise ContinuityIntegrationError("no trusted claims extractor is bound to this run")
            claims = await self._claims_extractor(chapter_text=chapter_text, brief=brief)
            if not isinstance(claims, dict):
                raise ContinuityIntegrationError("trusted extractor must return a mapping")
            # Recursive deep snapshot BEFORE writing content_hash or storing pending state --
            # the extractor may retain and mutate its returned mapping after we resume; a
            # shallow dict(...) copy still aliases nested entities/events, which would let a
            # post-return mutation change the Claims we later gate/commit.
            claims = deep_freeze_copy(claims)
            claims["content_hash"] = sha256_hex(chapter_text)
            self._pending[chapter_id] = {
                "chapter_number": chapter_number, "chapter_text": chapter_text,
                "claims": claims, "resumed": resumed, "ok": True,
            }
        except Exception:  # noqa: BLE001 - malformed/throwing extraction must never escape into generation
            self._pending[chapter_id] = {
                "chapter_number": chapter_number, "chapter_text": chapter_text,
                "claims": None, "resumed": resumed, "ok": False,
            }

    async def commit_observations(self) -> dict:
        pending = self._pending
        self._pending = {}
        for chapter_id in sorted(pending, key=lambda cid: pending[cid]["chapter_number"]):
            obs = pending[chapter_id]
            if not obs["ok"]:
                self._chapter_reports_by_id[chapter_id] = {
                    "chapter_id": chapter_id, "resumed": obs["resumed"], "ok": False,
                    "error": "EXTRACTOR_FAILED",
                }
                continue
            claims = obs["claims"]
            content_hash = claims.get("content_hash")
            existing = (self._ledger.get("commits") or {}).get(chapter_id)
            if existing is not None and existing.get("content_hash") != content_hash:
                before_commits = set(self._ledger.get("commits") or {})
                self._ledger = invalidate_from_chapter(self._ledger, chapter_id)
                after_commits = set(self._ledger.get("commits") or {})
                for removed_id in before_commits - after_commits:
                    if removed_id == chapter_id:
                        continue  # about to be recommitted fresh below, not "invalidated"
                    prior = self._chapter_reports_by_id.get(removed_id) or {}
                    self._chapter_reports_by_id[removed_id] = {
                        "chapter_id": removed_id, "resumed": prior.get("resumed", False),
                        "ok": False, "error": "INVALIDATED_BY_RETRY",
                    }
            brief = compile_chapter_brief(self._contract, self._ledger, chapter_id)
            try:
                gate = post_generation_gate(self._contract, self._ledger, brief, obs["chapter_text"], claims)
            except Exception:  # noqa: BLE001 - a malformed trusted-extractor payload must stay bounded
                gate = None
            if gate is not None and gate.get("decision") == "pass":
                try:
                    self._ledger = commit_chapter(self._ledger, claims, gate)
                    self._chapter_reports_by_id[chapter_id] = {
                        "chapter_id": chapter_id, "resumed": obs["resumed"], "ok": True, "error": None,
                    }
                    continue
                except Exception:  # noqa: BLE001
                    pass
            self._chapter_reports_by_id[chapter_id] = {
                "chapter_id": chapter_id, "resumed": obs["resumed"], "ok": False, "error": "GATE_NOT_CLEAN",
            }
        return self.report()

    def seal_map(self, *, chapter_records: list, assembled_book: str) -> None:
        """Validates exact types, exact complete order, and ledger-content binding BEFORE any
        mutation -- a malformed/out-of-order/stale-vs-ledger MAP chapter set never seals, and
        never leaks a raw TypeError from sorting or coercing a bad field."""
        if self._map_seal is not None:
            raise ContinuityIntegrationError("a MAP seal already exists for this run and cannot be replaced")
        if type(assembled_book) is not str:
            raise ContinuityIntegrationError("assembled_book must be an exact string")
        if type(chapter_records) is not list:
            raise ContinuityIntegrationError("chapter_records must be an exact list")

        contract_chapters = self._contract["chapter_contracts"] if self._contract is not None else []
        expected_order = sorted(
            ((c["chapter_id"], c["chapter_number"]) for c in contract_chapters),
            key=lambda pair: pair[1],
        )

        chapter_set: list = []
        for record in chapter_records:
            if type(record) is not dict:
                raise ContinuityIntegrationError("each chapter record must be an object")
            rid = record.get("id")
            rno = record.get("no")
            rcontent = record.get("content")
            if type(rid) is not str or rid == "":
                raise ContinuityIntegrationError("chapter record id must be a nonblank exact string")
            if not _is_exact_int(rno):
                raise ContinuityIntegrationError("chapter record no must be an integer, never a boolean or float")
            if type(rcontent) is not str:
                raise ContinuityIntegrationError(
                    "chapter record content must be an exact string -- a str subclass (including one "
                    "with an overridden encode()) is rejected before any hash is ever computed"
                )
            chapter_set.append({
                "chapter_id": rid, "chapter_number": rno + 1, "content_hash": sha256_hex(rcontent),
            })

        incoming_order = [(c["chapter_id"], c["chapter_number"]) for c in chapter_set]
        if incoming_order != expected_order:
            raise ContinuityIntegrationError(
                "chapter record set/order does not exactly match the Story Contract's complete "
                "chapter sequence (wrong/missing/extra/reordered/duplicate chapter)"
            )

        commits = (self._ledger.get("commits") or {}) if self._ledger is not None else {}
        for entry in chapter_set:
            existing = commits.get(entry["chapter_id"])
            if existing is not None and existing.get("content_hash") != entry["content_hash"]:
                raise ContinuityIntegrationError(
                    f"chapter {entry['chapter_id']!r} content does not match its existing clean "
                    "ledger commit -- a post-observation content change cannot be sealed"
                )

        chapter_set_hash = sha256_hex(canonical_json(chapter_set))
        contract_hash_value = _contract_hash(self._contract) if self._contract is not None else None
        ledger_hash = sha256_hex(canonical_json(self._ledger)) if self._ledger is not None else None
        pre_polish_book_hash = sha256_hex(assembled_book)
        seal_payload = {
            "attempt_id": str(self.attempt_id),
            "chapter_set_hash": chapter_set_hash,
            "contract_hash": contract_hash_value,
            "identity": self.identity,
            "ledger_hash": ledger_hash,
            "pre_polish_book_hash": pre_polish_book_hash,
        }
        map_seal_hash = sha256_hex(canonical_json(seal_payload))
        self._map_seal = {
            "chapter_set": chapter_set,  # full canonical (chapter_id, chapter_number, content_hash) triples
            "chapter_set_hash": chapter_set_hash,
            "book_hash": pre_polish_book_hash,
            "ledger_hash": ledger_hash,
            "map_seal_hash": map_seal_hash,
        }

    def prepare_final(self, *, result: dict, tenant_id: str, job_id: str, job_uuid: str) -> "FinalBundle":
        if self._final_prepared:
            raise ContinuityIntegrationError("a final bundle has already been prepared for this run")
        current = self.identity
        if (tenant_id != current.get("tenant_id") or job_id != current.get("job_id")
                or job_uuid != current.get("job_uuid")):
            raise ContinuityIntegrationError(
                "CROSS_JOB_IDENTITY_MISMATCH: prepare_final identity does not match this run"
            )
        self._final_prepared = True

        result_copy = deep_freeze_copy(result if isinstance(result, dict) else {})
        violations: list = []

        # Only THIS run's own sealed/contract chapter IDs are safe, bounded identifiers -- never
        # manuscript prose. Anything else a final chapter's `id` claims to be, however plausible,
        # is untrusted and must never be echoed into a report (Fix D).
        known_chapter_ids = (
            {c["chapter_id"] for c in self._map_seal["chapter_set"]} if self._map_seal is not None else set()
        )

        final_chapters = result_copy.get("chapters") if isinstance(result_copy.get("chapters"), list) else []
        chapter_hashes: list = []
        final_triples: list = []
        for c in final_chapters:
            if isinstance(c, dict):
                content = c.get("content")
                # Exact type FIRST, never isinstance/truthiness/str(...) coercion -- a sealed
                # string "3"/"True"/"{'a': 1}"/"[1, 2]" must not collide with a mutated final
                # int 3 / bool True / dict / list that merely stringifies to the same bytes, and a
                # hostile str subclass with an overridden encode() must never reach sha256_hex.
                h = sha256_hex(content) if type(content) is str else _FINAL_CONTENT_TYPE_MISMATCH
                final_triples.append((
                    _bounded_final_id(c.get("id"), known_chapter_ids),
                    _final_chapter_number(c.get("no")),
                    h,
                ))
            else:
                h = _FINAL_CONTENT_TYPE_MISMATCH
                final_triples.append((_FINAL_ID_INVALID, _FINAL_NUMBER_INVALID, h))
            chapter_hashes.append(h)

        final_book = result_copy.get("book")
        if final_book is None:
            final_book = result_copy.get("output")
        # Exact type before hashing -- a hostile str subclass with an overridden encode() (visibly
        # changed text, sealed bytes) must be treated as stale, never hashed, never call .encode().
        final_text_hash = sha256_hex(final_book) if type(final_book) is str else None

        if self._map_seal is None:
            violations.append({
                "code": "MAP_SEAL_MISSING", "expected": "a sealed MAP snapshot", "actual": None,
            })
        else:
            sealed_triples = [
                (c["chapter_id"], c["chapter_number"], c["content_hash"]) for c in self._map_seal["chapter_set"]
            ]
            if final_triples != sealed_triples:
                violations.append({
                    "code": "FINAL_CHAPTER_CHANGED_AFTER_VALIDATION",
                    "expected": sealed_triples, "actual": final_triples,
                })
            if final_text_hash != self._map_seal["book_hash"]:
                violations.append({
                    "code": "FINAL_TEXT_CHANGED_AFTER_VALIDATION",
                    "expected": self._map_seal["book_hash"], "actual": final_text_hash,
                })

        coverage_ok = self._coverage_complete() and self._map_seal is not None
        if not coverage_ok:
            violations.append({
                "code": "CONTINUITY_COVERAGE_INCOMPLETE", "expected": "complete", "actual": "incomplete",
            })

        report: dict = {
            "valid": not violations,
            "coverage": "complete" if coverage_ok else "incomplete",
            "final_text_hash": final_text_hash,
            "chapter_hashes": chapter_hashes,
            "violations": violations,
            "identity": dict(current),
            "attempt_id": str(self.attempt_id),
            "schema_versions": dict(_SCHEMA_VERSIONS),
        }
        if self._contract is not None:
            report["contract_hash"] = _contract_hash(self._contract)
        if self._map_seal is not None:
            report["ledger_hash"] = self._map_seal["ledger_hash"]
            report["chapter_set_hash"] = self._map_seal["chapter_set_hash"]
            report["map_seal_hash"] = self._map_seal["map_seal_hash"]
        elif self._ledger is not None:
            report["ledger_hash"] = sha256_hex(canonical_json(self._ledger))

        result_copy["continuity_shadow_report"] = report
        return FinalBundle(report=report, result=result_copy)


class FinalBundle:
    def __init__(self, *, report: dict, result: dict):
        self.report = report
        self.result = result
