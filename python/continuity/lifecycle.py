"""Continuity B-07/B-08 -- immutable target-language and stable chapter-identity lifecycle.

Pure contract module: zero provider, network, Redis, database, billing, or filesystem calls. It
resolves `target_language` exactly once at outline admission and gives every logical chapter one
stable, language-neutral `chapter_id` that survives title edits, reorder, queueing, persistence,
retry, resume, stitch/recover, and a derived Review or One-Shot request -- see
LIFECYCLE-CONTRACT.md and LIFECYCLE-REWORK-CONTRACT.md. Every public function raises only
`LifecycleValidationError` (never a raw built-in exception) with a stable, nonblank, bounded
`.code` and never echoes manuscript content. Inputs are never mutated; every function returns a
fresh, deep-copied object -- callers can never observe or corrupt lifecycle state by mutating a
returned value.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
import uuid
from typing import Any

LIFECYCLE_SCHEMA_VERSION = "1"

_MAX_LANGUAGE_LEN = 80
_TAG_LIKE = re.compile(r"^[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*$")
_CHAPTER_ID_RE = re.compile(r"^ch_[0-9a-f]{32}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

_OUTLINE_ROOT_FIELDS = frozenset({
    "schema_version", "outline_id", "target_language", "status", "chapters", "lifecycle_hash",
})
_CHAPTER_IDENTITY_FIELDS = frozenset({"chapter_id", "chapter_index", "legacy_id"})
_ARTIFACT_KINDS = frozenset({
    "brief", "story_contract", "chapter_plan", "review_input", "oneshot_input",
})
_ARTIFACT_BINDING_FIELDS = frozenset({
    "schema_version", "outline_id", "lifecycle_hash", "target_language",
    "artifact_kind", "content_hash",
})
_JOB_LIFECYCLE_FIELDS = frozenset({
    "schema_version", "outline_id", "lifecycle_hash", "target_language", "chapters", "bindings",
})


class LifecycleValidationError(Exception):
    """Raised for every B-07/B-08 lifecycle violation. `.code` is a stable, nonblank, machine-
    readable string; the message is bounded (<=300 chars) and never contains manuscript prose,
    secrets, or unbounded user content -- only codes, IDs, and hashes."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__((message or code)[:300])


def _assert_json_safe_finite(value: Any) -> None:
    """Recursively rejects anything that would make json.dumps() misbehave or raise a raw
    exception: non-finite floats (NaN/Infinity), non-JSON-serializable types (sets, tuples,
    custom objects), and non-string dict keys. Every creative field a caller attaches to a
    chapter passes through this before it can ever reach a hash computation."""
    if value is None or type(value) is bool or type(value) is str or type(value) is int:
        return
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):  # NaN or +/-Infinity
            raise LifecycleValidationError("CREATIVE_FIELD_INVALID",
                                           "creative field must be a finite number")
        return
    if type(value) is list:
        for item in value:
            _assert_json_safe_finite(item)
        return
    if type(value) is dict:
        for key, val in value.items():
            if type(key) is not str:
                raise LifecycleValidationError("CREATIVE_FIELD_INVALID",
                                               "creative field object keys must be strings")
            _assert_json_safe_finite(val)
        return
    raise LifecycleValidationError("CREATIVE_FIELD_INVALID",
                                   "creative field must be a finite JSON-safe value")


def _canonical_hash(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k != "lifecycle_hash"}
    try:
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LifecycleValidationError("LIFECYCLE_PAYLOAD_INVALID",
                                       "lifecycle payload is not JSON-safe") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_uuid(value: Any) -> str:
    if type(value) is not str:
        raise LifecycleValidationError("OUTLINE_ID_INVALID", "outline_id must be an exact string")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise LifecycleValidationError("OUTLINE_ID_INVALID", "outline_id must be a valid UUID string") from exc


def canonicalize_target_language(value: Any) -> str:
    """Resolves the one true `target_language` authority. Known BCP-47-like tags (letters/digits
    joined by a single `_`/`-`) are NFKC-normalized, trimmed, `_`->`-`, and lowercased. Anything
    else (a custom label such as "Basa Jawa Krama") is NFKC-normalized, outer-trimmed, internal-
    whitespace-collapsed, and case-preserved. No default language is ever synthesized -- missing/
    blank input is TARGET_LANGUAGE_REQUIRED, never a silent fallback."""
    if value is None:
        raise LifecycleValidationError("TARGET_LANGUAGE_REQUIRED", "target_language is required")
    if type(value) is not str:
        raise LifecycleValidationError("TARGET_LANGUAGE_INVALID", "target_language must be an exact string")
    stripped = value.strip()
    if stripped == "":
        raise LifecycleValidationError("TARGET_LANGUAGE_REQUIRED", "target_language is required")
    if any(unicodedata.category(ch) == "Cc" for ch in stripped):
        raise LifecycleValidationError("TARGET_LANGUAGE_INVALID", "target_language must not contain control characters")
    if len(stripped) > _MAX_LANGUAGE_LEN:
        raise LifecycleValidationError("TARGET_LANGUAGE_INVALID", "target_language exceeds the maximum length")
    normalized = unicodedata.normalize("NFKC", stripped)
    if _TAG_LIKE.fullmatch(normalized):
        return normalized.replace("_", "-").lower()
    return re.sub(r"\s+", " ", normalized).strip()


def _deterministic_chapter_id(outline_id: str, index: int, avoid: "list[Any]") -> str:
    """Deterministic for (outline_id, original slot): the same input always converges on the same
    id. A small, deterministic nonce search (always starting at 0) additionally guarantees the
    32-hex-character suffix never contains a chapter's own legacy/display id as a substring --
    display id, title, and ordinal are never identity authority."""
    avoid_lower = [str(a).lower() for a in avoid if a is not None and str(a) != ""]
    nonce = 0
    while True:
        digest = hashlib.sha256(f"{outline_id}:{index}:{nonce}".encode("utf-8")).hexdigest()[:32]
        if not any(a in digest for a in avoid_lower):
            return "ch_" + digest
        nonce += 1


def admit_outline_lifecycle(outline_id: Any, target_language: Any, chapters: Any) -> dict:
    """The only path that mints `outline_id`/`chapter_id`/`lifecycle_hash`. Creates every
    `chapter_id` before Brief, Bible, or generation; preserves every other creative chapter field
    verbatim (after proving it is a finite, JSON-safe value); never mutates the caller's
    `chapters` input."""
    canonical_outline_id = _canonical_uuid(outline_id)
    canonical_language = canonicalize_target_language(target_language)
    if type(chapters) is not list:
        raise LifecycleValidationError("CHAPTER_SET_INVALID", "chapters must be an exact list")
    built_chapters = []
    for index, chapter in enumerate(chapters):
        if type(chapter) is not dict:
            raise LifecycleValidationError("CHAPTER_SET_INVALID", "each chapter must be an object")
        legacy_id = chapter.get("id")
        cid = _deterministic_chapter_id(canonical_outline_id, index, [legacy_id])
        new_chapter = {"chapter_id": cid, "chapter_index": index, "legacy_id": legacy_id}
        for key, val in chapter.items():
            if key not in ("id", *_CHAPTER_IDENTITY_FIELDS):
                _assert_json_safe_finite(val)
                new_chapter[key] = copy.deepcopy(val)
        built_chapters.append(new_chapter)
    outline_obj = {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "outline_id": canonical_outline_id,
        "target_language": canonical_language,
        "status": "active",
        "chapters": built_chapters,
    }
    outline_obj["lifecycle_hash"] = _canonical_hash(outline_obj)
    return outline_obj


def validate_outline_lifecycle(payload: Any) -> dict:
    """Structural checks (unknown/missing root fields, exact chapter identity types, duplicate
    IDs, index-vs-position sequence), then canonical-form checks (language, UUID spelling,
    status), run BEFORE the hash comparison -- resealing a corrupted or non-canonical structure's
    hash can never make it valid. Returns a fresh deep copy; the caller's object is never
    returned or mutated."""
    if type(payload) is not dict:
        raise LifecycleValidationError("LIFECYCLE_PAYLOAD_INVALID", "outline lifecycle must be an object")
    extra = set(payload) - _OUTLINE_ROOT_FIELDS
    if extra:
        raise LifecycleValidationError("LIFECYCLE_FIELD_UNKNOWN", f"unknown lifecycle field(s): {sorted(extra)}")
    missing = _OUTLINE_ROOT_FIELDS - set(payload)
    if missing:
        raise LifecycleValidationError("LIFECYCLE_FIELD_MISSING", f"missing lifecycle field(s): {sorted(missing)}")
    if type(payload.get("schema_version")) is not str or payload.get("schema_version") != LIFECYCLE_SCHEMA_VERSION:
        raise LifecycleValidationError("LIFECYCLE_SCHEMA_VERSION_INVALID", "unsupported lifecycle schema_version")
    outline_id = payload.get("outline_id")
    if type(outline_id) is not str:
        raise LifecycleValidationError("OUTLINE_ID_INVALID", "outline_id must be an exact string")
    if type(payload.get("status")) is not str:
        raise LifecycleValidationError("LIFECYCLE_STATUS_INVALID", "status must be an exact string")
    if payload.get("status") != "active":
        raise LifecycleValidationError("LIFECYCLE_STATUS_INVALID", "status must be exactly 'active'")
    target_language = payload.get("target_language")
    canonical_language = canonicalize_target_language(target_language)
    if canonical_language != target_language:
        raise LifecycleValidationError("TARGET_LANGUAGE_INVALID", "target_language is not in its own canonical form")
    if outline_id != _canonical_uuid(outline_id):
        raise LifecycleValidationError("OUTLINE_ID_INVALID", "outline_id is not in its own canonical form")
    chapters = payload.get("chapters")
    if type(chapters) is not list:
        raise LifecycleValidationError("CHAPTER_SET_INVALID", "chapters must be an exact list")
    seen_ids: set = set()
    for position, chapter in enumerate(chapters):
        if type(chapter) is not dict:
            raise LifecycleValidationError("CHAPTER_SET_INVALID", "each chapter must be an object")
        cid = chapter.get("chapter_id")
        if type(cid) is not str or not _CHAPTER_ID_RE.fullmatch(cid):
            raise LifecycleValidationError("CHAPTER_ID_INVALID", "chapter_id must match ch_[0-9a-f]{32}")
        if cid in seen_ids:
            raise LifecycleValidationError("CHAPTER_ID_DUPLICATE", "duplicate chapter_id in outline")
        seen_ids.add(cid)
        index = chapter.get("chapter_index")
        if type(index) is not int:
            raise LifecycleValidationError("CHAPTER_INDEX_INVALID", "chapter_index must be an exact integer")
        if index != position:
            raise LifecycleValidationError("CHAPTER_INDEX_SEQUENCE", "chapter_index must match its position exactly")
        legacy_id = chapter.get("legacy_id")
        if legacy_id is not None and type(legacy_id) is not str:
            raise LifecycleValidationError("CHAPTER_SET_INVALID", "legacy_id must be an exact string or null")
    if type(payload.get("lifecycle_hash")) is not str or payload.get("lifecycle_hash") != _canonical_hash(payload):
        raise LifecycleValidationError("LIFECYCLE_HASH_MISMATCH", "lifecycle_hash does not match its canonical content")
    return copy.deepcopy(payload)


def reindex_outline_lifecycle(payload: Any, ordered_chapter_ids: Any) -> dict:
    """Reorders an already-admitted outline's chapters to an exact permutation of its own
    chapter_id set, recomputing only chapter_index and lifecycle_hash. Existing IDs -- and every
    other field -- survive unchanged; a partial/duplicate/foreign/type-confused ordering rejects
    before any mutation. The upstream payload is fully re-validated FIRST -- a resealed-but-
    corrupt or hash-mismatched outline can never be laundered into a fresh-looking hash by
    reindexing it."""
    payload = validate_outline_lifecycle(payload)
    chapters = payload.get("chapters")
    if type(chapters) is not list:
        raise LifecycleValidationError("CHAPTER_SET_INVALID", "chapters must be an exact list")
    by_id = {}
    for chapter in chapters:
        if type(chapter) is not dict:
            raise LifecycleValidationError("CHAPTER_SET_INVALID", "each chapter must be an object")
        by_id[chapter.get("chapter_id")] = chapter

    if type(ordered_chapter_ids) is not list or len(ordered_chapter_ids) != len(chapters):
        raise LifecycleValidationError("CHAPTER_SET_MISMATCH", "reorder must be an exact permutation of the chapter set")
    seen: set = set()
    for cid in ordered_chapter_ids:
        if type(cid) is not str or cid in seen or cid not in by_id:
            raise LifecycleValidationError("CHAPTER_SET_MISMATCH", "reorder must be an exact permutation of the chapter set")
        seen.add(cid)

    new_chapters = []
    for new_index, cid in enumerate(ordered_chapter_ids):
        updated = copy.deepcopy(by_id[cid])
        updated["chapter_index"] = new_index
        new_chapters.append(updated)

    new_payload = copy.deepcopy(payload)
    new_payload["chapters"] = new_chapters
    new_payload["lifecycle_hash"] = _canonical_hash(new_payload)
    return new_payload


def bind_lifecycle_artifact(payload: Any, artifact_kind: Any, content_hash: Any) -> dict:
    """Every derived artifact (Brief, Story Contract, chapter plan, Review/One-Shot input) binds
    to the exact outline/lifecycle/language it was produced from plus its own content hash. The
    upstream outline is fully re-validated FIRST -- a resealed/forged outline (even one whose
    lifecycle_hash field was overwritten to match doctored content) can never mint a binding."""
    payload = validate_outline_lifecycle(payload)
    if type(artifact_kind) is not str or artifact_kind not in _ARTIFACT_KINDS:
        raise LifecycleValidationError("ARTIFACT_KIND_INVALID", "unknown artifact_kind")
    if type(content_hash) is not str or not _HEX64_RE.fullmatch(content_hash):
        raise LifecycleValidationError("ARTIFACT_CONTENT_HASH_INVALID", "content_hash must be a lowercase sha256 hex string")
    return {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "outline_id": payload.get("outline_id"),
        "lifecycle_hash": payload.get("lifecycle_hash"),
        "target_language": payload.get("target_language"),
        "artifact_kind": artifact_kind,
        "content_hash": content_hash,
    }


def validate_lifecycle_artifact(payload: Any, binding: Any, artifact_kind: Any, content_hash: Any) -> dict:
    """Rejects an unknown/missing binding field, wrong schema_version, stale content, stale
    lifecycle, wrong kind, wrong outline, or wrong language -- every field of the binding is
    checked against the CURRENT payload/kind/hash, not just echoed. The upstream outline is
    fully re-validated FIRST (never just type/dict-checked) so a forged-but-well-shaped payload
    can never pass a binding check merely because its `outline_id`/`lifecycle_hash` fields were
    copied verbatim. Returns a fresh deep copy."""
    payload = validate_outline_lifecycle(payload)
    _validate_binding_shape(binding, artifact_kind, outline_id=payload.get("outline_id"),
                            lifecycle_hash=payload.get("lifecycle_hash"),
                            target_language=payload.get("target_language"))
    if binding.get("content_hash") != content_hash:
        raise LifecycleValidationError("ARTIFACT_CONTENT_MISMATCH", "artifact binding content_hash mismatch")
    return copy.deepcopy(binding)


def _exact_equal(a: Any, b: Any) -> bool:
    """Recursive, exact-type equality at every depth. Python's `==` treats `False == 0` and
    `0.0 == 0` (bool is an int subclass; int/float compare numerically) -- this rejects a
    creative-field drift disguised as a same-looking value of a different exact type no
    matter how deeply nested inside a list or dict (B-07/B-08 Rework 3, Contract A)."""
    if type(a) is not type(b):
        return False
    if type(a) is float and a != a and b != b:  # NaN != NaN; treat as equal here
        return True
    if type(a) is dict:
        if set(a) != set(b):
            return False
        return all(_exact_equal(a[k], b[k]) for k in a)
    if type(a) is list:
        if len(a) != len(b):
            return False
        return all(_exact_equal(x, y) for x, y in zip(a, b))
    return a == b


def _chapters_equal(a: dict, b: dict) -> bool:
    """Exact structural equality for two chapter dicts, recursive and exact-type at every
    depth -- a chapter_index/words/nested-field drift disguised as a same-looking value of a
    different exact type still counts as a mismatch, no matter how deeply nested."""
    return _exact_equal(a, b)


_ARTIFACT_KIND_KEY = "artifact_kind"


def _validate_binding_shape(binding: Any, kind: Any, *, outline_id: Any, lifecycle_hash: Any,
                            target_language: Any) -> None:
    """Shared binding-shape closure used by both `validate_lifecycle_artifact` (against a
    caller-supplied content_hash) and `validate_job_lifecycle_request` (against a durable
    job's own authority): closed fields, exact schema_version, cross-checked
    outline_id/lifecycle_hash/target_language/artifact_kind, and a well-formed content_hash
    STRING shape (freshness against real content, when there is real content to compare, is
    the caller's job)."""
    if type(kind) is not str or kind not in _ARTIFACT_KINDS:
        raise LifecycleValidationError("ARTIFACT_KIND_INVALID", "unknown artifact_kind in bindings")
    if type(binding) is not dict:
        raise LifecycleValidationError("ARTIFACT_BINDING_INVALID", "binding must be an object")
    extra = set(binding) - _ARTIFACT_BINDING_FIELDS
    if extra:
        raise LifecycleValidationError("ARTIFACT_BINDING_INVALID", f"unknown artifact binding field(s): {sorted(extra)}")
    missing = _ARTIFACT_BINDING_FIELDS - set(binding)
    if missing:
        raise LifecycleValidationError("ARTIFACT_BINDING_INVALID", f"missing artifact binding field(s): {sorted(missing)}")
    if type(binding.get("schema_version")) is not str or binding.get("schema_version") != LIFECYCLE_SCHEMA_VERSION:
        raise LifecycleValidationError("ARTIFACT_BINDING_INVALID", "unsupported artifact binding schema_version")
    if binding.get("outline_id") != outline_id:
        raise LifecycleValidationError("ARTIFACT_OUTLINE_MISMATCH", "artifact binding outline_id mismatch")
    if binding.get("lifecycle_hash") != lifecycle_hash:
        raise LifecycleValidationError("ARTIFACT_LIFECYCLE_MISMATCH", "artifact binding lifecycle_hash mismatch")
    if binding.get("target_language") != target_language:
        raise LifecycleValidationError("ARTIFACT_LANGUAGE_MISMATCH", "artifact binding target_language mismatch")
    if binding.get(_ARTIFACT_KIND_KEY) != kind:
        raise LifecycleValidationError("ARTIFACT_KIND_MISMATCH", "artifact binding artifact_kind mismatch")
    if type(binding.get("content_hash")) is not str or not _HEX64_RE.fullmatch(binding.get("content_hash")):
        raise LifecycleValidationError("ARTIFACT_CONTENT_HASH_INVALID", "content_hash must be a lowercase sha256 hex string")


def build_job_lifecycle(payload: Any, chapters: Any, bindings: Any = None) -> dict:
    """The durable, minimal job snapshot written into `jobs.input_payload.narasi_lifecycle` in the
    SAME insert as `_meter`. Re-validates the COMPLETE upstream outline lifecycle first (a
    resealed-but-corrupt or hash-mismatched payload can never produce a snapshot). `chapters` must
    be the exact admitted set, in the exact admitted order, with every prompt-driving field
    (title, words, description, legacy_id, and any other admitted creative field) byte-identical
    to what was admitted -- missing/duplicate/forged/extra/reordered/drifted chapters reject
    before any cost/hold/queue work happens. `bindings` (optional) is a {artifact_kind: binding}
    map of ALREADY-VALIDATED `validate_lifecycle_artifact()` results (e.g. a supplied Brief) --
    each is re-verified against THIS exact outline before being written into the snapshot, so a
    stale/forged/wrong-outline binding can never ride along into durable storage."""
    validated = validate_outline_lifecycle(payload)
    canonical_chapters = validated.get("chapters")
    if type(chapters) is not list or len(chapters) != len(canonical_chapters):
        raise LifecycleValidationError("CHAPTER_SET_MISMATCH", "chapter set does not match the outline lifecycle")
    canonical_by_id = {c.get("chapter_id"): c for c in canonical_chapters}
    minimal_chapters = []
    for position, chapter in enumerate(chapters):
        if type(chapter) is not dict:
            raise LifecycleValidationError("CHAPTER_SET_MISMATCH", "chapter set does not match the outline lifecycle")
        canonical = canonical_by_id.get(chapter.get("chapter_id"))
        if canonical is None or canonical.get("chapter_index") != position:
            raise LifecycleValidationError("CHAPTER_SET_MISMATCH", "chapter set does not match the outline lifecycle")
        if not _chapters_equal(chapter, canonical):
            raise LifecycleValidationError(
                "CHAPTER_SET_MISMATCH", "chapter content does not match the admitted outline lifecycle")
        minimal_chapters.append({
            "chapter_id": canonical.get("chapter_id"),
            "chapter_index": canonical.get("chapter_index"),
            "legacy_id": canonical.get("legacy_id"),
        })
    validated_bindings: dict = {}
    if bindings is not None:
        if type(bindings) is not dict:
            raise LifecycleValidationError("ARTIFACT_BINDING_INVALID", "bindings must be an object")
        for kind, binding in bindings.items():
            if type(kind) is not str or kind not in _ARTIFACT_KINDS:
                raise LifecycleValidationError("ARTIFACT_KIND_INVALID", "unknown artifact_kind in bindings")
            if type(binding) is not dict or type(binding.get("content_hash")) is not str:
                raise LifecycleValidationError("ARTIFACT_BINDING_INVALID", "malformed binding in bindings")
            validated_bindings[kind] = validate_lifecycle_artifact(
                validated, binding, kind, binding.get("content_hash"))
    return {
        "schema_version": validated.get("schema_version"),
        "outline_id": validated.get("outline_id"),
        "lifecycle_hash": validated.get("lifecycle_hash"),
        "target_language": validated.get("target_language"),
        "chapters": minimal_chapters,
        "bindings": validated_bindings,
    }


def validate_durable_job_snapshot(job_lifecycle: Any) -> dict:
    """Fully validates a durable job snapshot's OWN structure, independent of any specific
    request: closed-schema, exact-version, canonical UUID/language/hash-format, every binding
    FULLY validated (unknown kind, non-object, missing/extra field, wrong kind/outline/
    language, or a non-string content_hash all reject), and a closed, canonical, unique
    chapter identity tuple at every position (B-07/B-08 Rework 3, Contract A). A
    malformed/tampered durable row is never trusted merely because it superficially resembles
    a valid one -- every public consumer of a durable job snapshot (worker re-read, Google
    persist, retry, derived lineage) routes through this one function. Returns a fresh deep
    copy; never mutates its input."""
    if type(job_lifecycle) is not dict:
        raise LifecycleValidationError("LIFECYCLE_PAYLOAD_INVALID", "job lifecycle must be an object")
    extra = set(job_lifecycle) - _JOB_LIFECYCLE_FIELDS
    if extra:
        raise LifecycleValidationError("LIFECYCLE_FIELD_UNKNOWN", f"unknown job lifecycle field(s): {sorted(extra)}")
    missing = _JOB_LIFECYCLE_FIELDS - set(job_lifecycle)
    if missing:
        raise LifecycleValidationError("LIFECYCLE_FIELD_MISSING", f"missing job lifecycle field(s): {sorted(missing)}")
    if type(job_lifecycle.get("schema_version")) is not str or job_lifecycle.get("schema_version") != LIFECYCLE_SCHEMA_VERSION:
        raise LifecycleValidationError("LIFECYCLE_SCHEMA_VERSION_INVALID", "unsupported job lifecycle schema_version")
    _durable_outline_id = job_lifecycle.get("outline_id")
    if _durable_outline_id != _canonical_uuid(_durable_outline_id):
        raise LifecycleValidationError("OUTLINE_ID_INVALID", "job lifecycle outline_id is not a canonical UUID")
    _durable_hash = job_lifecycle.get("lifecycle_hash")
    if type(_durable_hash) is not str or not _HEX64_RE.fullmatch(_durable_hash):
        raise LifecycleValidationError("LIFECYCLE_HASH_MISMATCH", "job lifecycle lifecycle_hash must be a lowercase sha256 hex string")
    _durable_language = job_lifecycle.get("target_language")
    if _durable_language != canonicalize_target_language(_durable_language):
        raise LifecycleValidationError("TARGET_LANGUAGE_INVALID", "job lifecycle target_language is not in its own canonical form")
    # B-07/B-08 Rework 3 (Contract A): every binding is FULLY validated against this job's own
    # outline_id/lifecycle_hash/target_language -- a supplied bindings map that is merely "a
    # dict" (unknown kind, non-object, missing/extra field, wrong kind/outline/language, or a
    # non-string content_hash) is a forged/incomplete snapshot, not a legitimate one.
    _durable_bindings = job_lifecycle.get("bindings")
    if type(_durable_bindings) is not dict:
        raise LifecycleValidationError("ARTIFACT_BINDING_INVALID", "job lifecycle bindings must be an object")
    for _binding_kind, _binding in _durable_bindings.items():
        _validate_binding_shape(_binding, _binding_kind, outline_id=_durable_outline_id,
                                lifecycle_hash=_durable_hash, target_language=_durable_language)
    _durable_chapters = job_lifecycle.get("chapters")
    if type(_durable_chapters) is not list:
        raise LifecycleValidationError("CHAPTER_SET_INVALID", "job lifecycle chapters must be an exact list")
    _durable_seen_ids: set = set()
    for _durable_position, _durable_chapter in enumerate(_durable_chapters):
        if type(_durable_chapter) is not dict:
            raise LifecycleValidationError("CHAPTER_SET_INVALID", "each job lifecycle chapter must be an object")
        # B-07/B-08 Rework 3 (Contract A): the durable job chapter tuple is CLOSED -- exactly
        # the minimal identity fields, nothing else (a "smuggled" creative field like `title`
        # does not belong in a job snapshot's reduced identity tuple).
        if set(_durable_chapter) != _CHAPTER_IDENTITY_FIELDS:
            raise LifecycleValidationError("CHAPTER_SET_INVALID",
                                           "job lifecycle chapter must contain exactly the minimal identity fields")
        _durable_cid = _durable_chapter.get("chapter_id")
        if type(_durable_cid) is not str or not _CHAPTER_ID_RE.fullmatch(_durable_cid):
            raise LifecycleValidationError("CHAPTER_ID_INVALID", "job lifecycle chapter_id must match ch_[0-9a-f]{32}")
        if _durable_cid in _durable_seen_ids:
            raise LifecycleValidationError("CHAPTER_ID_DUPLICATE", "duplicate chapter_id in job lifecycle")
        _durable_seen_ids.add(_durable_cid)
        _durable_idx = _durable_chapter.get("chapter_index")
        if type(_durable_idx) is not int or _durable_idx != _durable_position:
            raise LifecycleValidationError("CHAPTER_INDEX_INVALID", "job lifecycle chapter_index must match its position exactly")
        _durable_legacy = _durable_chapter.get("legacy_id")
        if _durable_legacy is not None and type(_durable_legacy) is not str:
            raise LifecycleValidationError("CHAPTER_SET_INVALID", "job lifecycle legacy_id must be an exact string or null")
    return copy.deepcopy(job_lifecycle)


def validate_job_lifecycle_request(job_lifecycle: Any, request: Any) -> dict:
    """Compares an incoming request (queue payload, worker re-read, or in-process call) against
    the durable job snapshot -- the ONLY authority. The durable snapshot itself is fully
    validated FIRST via `validate_durable_job_snapshot` (never merely compared-by-`!=`, which
    lets a bool-for-int durable field slip through Python's numeric equality) -- a
    malformed/tampered durable row is never trusted merely because it superficially resembles
    a valid one. A missing request language is TARGET_LANGUAGE_REQUIRED, never a silent
    default; a request-supplied `outline_lifecycle` is itself fully re-validated (not just
    hash-spot-checked) and must match this exact durable lifecycle_hash; a mismatched
    language, chapter set, chapter_index, or legacy_id (even with the same chapter_id, even
    under exact-type confusion like bool-for-int or a str/int subclass) rejects -- including
    the request's legacy_id against the DURABLE legacy_id, not merely its own type. Never
    mutates its inputs."""
    _validated_job = validate_durable_job_snapshot(job_lifecycle)
    _durable_outline_id = _validated_job.get("outline_id")
    _durable_hash = _validated_job.get("lifecycle_hash")
    _durable_language = _validated_job.get("target_language")
    _durable_chapters = _validated_job.get("chapters")

    if type(request) is not dict:
        raise LifecycleValidationError("LIFECYCLE_PAYLOAD_INVALID", "request must be an object")

    supplied_outline = request.get("outline_lifecycle")
    if supplied_outline is not None:
        _validated_supplied_outline = validate_outline_lifecycle(supplied_outline)
        if _validated_supplied_outline.get("lifecycle_hash") != _durable_hash:
            raise LifecycleValidationError("LIFECYCLE_HASH_MISMATCH",
                                           "request outline_lifecycle does not match the durable job lifecycle")

    canonical_language = canonicalize_target_language(request.get("target_language"))
    if canonical_language != _durable_language:
        raise LifecycleValidationError("TARGET_LANGUAGE_MISMATCH", "request target_language does not match the durable job lifecycle")

    request_chapters = request.get("chapters")
    if (type(request_chapters) is not list
            or len(request_chapters) != len(_durable_chapters)):
        raise LifecycleValidationError("CHAPTER_SET_MISMATCH", "request chapter set does not match the durable job lifecycle")
    for req_chapter, canon_chapter in zip(request_chapters, _durable_chapters):
        if type(req_chapter) is not dict:
            raise LifecycleValidationError("CHAPTER_SET_MISMATCH", "request chapter set does not match the durable job lifecycle")
        req_cid = req_chapter.get("chapter_id")
        if type(req_cid) is not str or req_cid != canon_chapter.get("chapter_id"):
            raise LifecycleValidationError("CHAPTER_SET_MISMATCH", "request chapter set does not match the durable job lifecycle")
        req_idx = req_chapter.get("chapter_index")
        if type(req_idx) is not int or req_idx != canon_chapter.get("chapter_index"):
            raise LifecycleValidationError("CHAPTER_INDEX_MISMATCH", "request chapter_index does not match the durable job lifecycle")
        req_legacy = req_chapter.get("legacy_id")
        if req_legacy is not None and type(req_legacy) is not str:
            raise LifecycleValidationError("CHAPTER_SET_MISMATCH", "request legacy_id must be an exact string or null")
        if req_legacy != canon_chapter.get("legacy_id"):
            raise LifecycleValidationError("CHAPTER_SET_MISMATCH", "request legacy_id does not match the durable job lifecycle")
    return {
        "schema_version": _validated_job.get("schema_version"),
        "outline_id": _durable_outline_id,
        "lifecycle_hash": _durable_hash,
        "target_language": canonical_language,
        "chapters": copy.deepcopy(_durable_chapters),
        "bindings": copy.deepcopy(_validated_job.get("bindings")),
    }


def classify_language_change(payload: Any, new_target_language: Any) -> dict:
    """Returns 'unchanged' only for exact canonical equality; any real change returns
    'invalidate' and lists exactly the four dependent artifacts. Never mutates or rewrites the
    existing lifecycle -- invalidation is the caller's responsibility to act on. The upstream
    outline is fully re-validated FIRST -- a forged/corrupt payload can never even reach the
    'unchanged' classification just because its target_language field happens to match."""
    payload = validate_outline_lifecycle(payload)
    canonical_new = canonicalize_target_language(new_target_language)
    old = payload.get("target_language")
    if canonical_new == old:
        return {"action": "unchanged"}
    return {
        "action": "invalidate",
        "reason": "target_language_changed",
        "old_target_language": old,
        "new_target_language": canonical_new,
        "invalidates": ["outline", "brief", "story_contract", "chapter_plan"],
    }


__all__ = [
    "LIFECYCLE_SCHEMA_VERSION",
    "LifecycleValidationError",
    "canonicalize_target_language",
    "admit_outline_lifecycle",
    "validate_outline_lifecycle",
    "reindex_outline_lifecycle",
    "bind_lifecycle_artifact",
    "validate_lifecycle_artifact",
    "build_job_lifecycle",
    "validate_durable_job_snapshot",
    "validate_job_lifecycle_request",
    "classify_language_change",
]
