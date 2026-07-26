"""B-02 -- exact, ordered, content-addressed mutation-hash evidence for the current Scenario A/B
``narrate_chapters`` pipeline.

Pure, local, deterministic: no provider, database, Redis, web framework, clock, randomness,
environment, or production-hash-helper dependency. This module proves an integrity fingerprint
over four real prose-mutation boundaries (post_map, post_polish, post_revise, final) -- it never
decides whether prose is good, authenticates a database row, authorizes a caller, signs an
artifact, or makes persistence atomic. ``authenticity`` is always ``"not_provided"``: SHA-256 is
not a MAC, and a party able to replace both bytes and every digest can re-seal a plain JSON
snapshot that stays internally self-consistent without ever becoming genuinely authenticated.

Every public entry point (``MutationHashRecorder.__init__``, ``.capture()``, ``.finalize()``,
``verify_final_binding()``) rejects a caller value's non-exact built-in type BEFORE calling any
method, comparison, hash, encode, iteration, or copy on it. A caller-supplied container is only
ever read via safe built-in operations (``in``, ``.get``/``[]`` on an *exact* ``dict``/``list``
whose own type was already confirmed exact) -- never ``copy.deepcopy``, ``repr``, or any operation
that could invoke an attacker's overridden dunder method. Only the specific fields this module
actually needs are read, once each, into a brand-new closed structure; every other field on a
caller object (a chapter record may carry unrelated display/debug keys) is never touched at all.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from typing import Any


MUTATION_HASH_SCHEMA_VERSION = "1"
HASH_ALGORITHM = "sha256"
STAGE_ORDER = ("post_map", "post_polish", "post_revise", "final")
VERSION_KEYS = (
    "mutation_hash_schema_version",
    "lifecycle_schema_version",
    "story_contract_schema_version",
    "story_contract_hash",
    "contract_prompt_version",
    "compiler_version",
    "slicer_version",
    "writer_context_version",
    "extractor_schema_version",
    "extractor_prompt_version",
    "extractor_epoch",
    "predicate_set_version",
    "diff_version",
)
FINAL_METADATA_KEYS = (
    "scenario", "strategy", "polished", "rag_used", "n_ok", "n_total",
    "outline_source", "mode", "target_language", "model", "manager_model",
)

_STAGE_KEYS = frozenset({
    "schema_version", "stage", "sequence", "coverage", "error_code",
    "candidate_hash", "chapters", "chapter_set_hash", "version_set_hash",
    "metadata_hash", "previous_stage_hash", "stage_hash",
})
_SNAPSHOT_KEYS = frozenset({
    "schema_version", "hash_algorithm", "authenticity", "valid", "coverage",
    "chapter_identity_mode", "chapter_count", "version_bindings", "version_set_hash",
    "stages", "final_candidate_hash", "final_metadata_hash", "ledger_hash",
})
_CHAPTER_KEYS = frozenset({"chapter_id", "legacy_index", "chapter_number", "content_hash"})
_VERSION_KEYS_SET = frozenset(VERSION_KEYS)
_FINAL_METADATA_KEYS_SET = frozenset(FINAL_METADATA_KEYS)
_STRUCTURAL_ERROR_CODES = frozenset({
    "CHAPTER_HEADING_COUNT_MISMATCH", "CHAPTER_HEADING_NUMBER_INVALID", "CHAPTER_HEADING_ORDER_MISMATCH",
})

_HEADING_RE = re.compile(r"(?m)^##[ \t]+[^\r\n]*(?:\r\n|\n|\r|$)")
_NUMBER_RE = re.compile(r"^##[ \t]+\D*?([0-9]+)(?![A-Za-z.])")
_CHAPTER_ID_RE = re.compile(r"^ch_[0-9a-f]{32}$")
_HASH64_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_HEADING_DIGITS = 18  # generous for any real chapter count; far below any int-conversion limit
_MAX_BOUND_INTEGER = (1 << 63) - 1  # inclusive signed-64-bit ceiling; keeps every accepted integer
                                    # comfortably inside json.dumps's int-to-string digit limit


class MutationHashError(Exception):
    """Raised for any invalid input or bounded protocol failure. Carries a stable ``code``; the
    message is always static and content-free -- never manuscript text, headings, metadata
    values, arbitrary ``repr``, or a caller exception's own text/arguments."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__((message or code)[:300])


def _fail(code: str, message: str = "") -> None:
    raise MutationHashError(code, message)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _structured_hash(value: Any) -> str:
    return _sha_text(_canonical_json(value))


def _safe_structured_hash(value: Any):
    try:
        return _structured_hash(value)
    except Exception:  # noqa: BLE001 - malformed/non-JSON-safe content must never raise here
        return None


def _utf8_safe(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_hash64(value: Any) -> bool:
    return type(value) is str and _HASH64_RE.fullmatch(value) is not None


def _is_bounded_nonblank_str(value: Any, max_bytes: int) -> bool:
    if type(value) is not str or not value.strip() or not _utf8_safe(value):
        return False
    return len(value.encode("utf-8")) <= max_bytes


def _exact_string_keyed_dict_or_none(value: Any):
    """Exact-type-first: confirms `value` is an exact ``dict`` AND every key inside it is an
    exact ``str``, without ever hashing/comparing/invoking a hook on an untrusted key -- and
    without ever returning or re-reading the caller's own dict object afterward (V5 Section 2).

    A prior revision of this function called ``dict(value)`` BEFORE checking any key's type, on
    the theory that copying an already-built dict is a hash-free bulk operation. That is false: if
    ``value`` already holds two keys whose hashes collide (a real ``str`` key plus a foreign object
    engineered to hash the same), inserting the second of that pair into the brand-new dict being
    built by ``dict(value)`` requires CPython to distinguish "same key, update value" from "genuine
    collision, probe further" -- which calls the foreign key's ``__eq__``. So a hostile key already
    present in ``value`` could have its hook invoked by the "safe" copy itself, before any type
    check ever ran.

    A second prior revision walked ``for key, item in value.items():`` directly -- a Python-level
    loop over the LIVE dict. That is also unsafe (V6 Section 2): another thread, a signal handler,
    or a re-entrant caller callback can resize ``value`` between two loop iterations, and CPython
    raises a raw ``RuntimeError: dictionary changed size during iteration`` the next time the loop
    resumes -- violating the "never raises anything but MutationHashError" contract just as surely
    as the earlier ``dict(value)`` bug did, just at a different call site.

    The correct order is: capture ``value.items()`` into a plain ``list`` in ONE atomic built-in
    bulk operation -- ``list(value.items())`` stores the already-existing (key, value) pair
    references sequentially and builds no destination hash table, so it cannot invoke a hostile
    key's ``__hash__``/``__eq__`` (unlike ``dict(value)``, which builds a table and can trigger
    collision-resolution equality checks) -- and it cannot observe a later caller resize, because
    by the time any Python-level loop runs, the pairs already live in this independent list, wholly
    decoupled from ``value``'s own live hash table. Only AFTER this one atomic capture does a
    Python-level loop run, over the captured list (never over ``value`` again), inserting each
    entry into a brand-new closed dict ONE AT A TIME, but only ever after that entry's key has just
    been confirmed exact ``str``. Inserting a confirmed ``str`` key can only ever invoke ``str``'s
    own non-overridable hashing/equality, never a foreign object's, because by construction no
    non-``str`` key is ever inserted into the new dict. The moment a non-``str`` key is seen, this
    returns ``None`` immediately -- that key is never looked up, hashed, compared, or inserted
    anywhere. Every subsequent read in this module operates ONLY on the returned captured dict --
    the caller's original dict is never touched again, so a caller replacing a key (with a foreign,
    hash-colliding object) or resizing the dict after this point, concurrently or otherwise, can
    never influence anything downstream. Returns the captured dict if every key is exact ``str``,
    else ``None``."""
    if type(value) is not dict:
        return None
    captured_items = list(value.items())
    captured: dict = {}
    for key, item in captured_items:
        if type(key) is not str:
            return None
        captured[key] = item
    return captured


def _safe_eq(untrusted: Any, expected: Any, expected_type: type) -> bool:
    """`expected` is `None` or an exact value of `expected_type` this module computed/trusts
    itself. Returns True iff `untrusted` safely equals `expected` -- confirms `untrusted`'s exact
    type (or uses `is None`, which never invokes anything) before any `==` comparison, so a
    hostile `untrusted` value's `__eq__`/`__ne__`/`__hash__` is never invoked even when the
    comparison would otherwise fail closed."""
    if expected is None:
        return untrusted is None
    return type(untrusted) is expected_type and untrusted == expected


def _closed_chapter_records(chapter_records: Any):
    """Exact-type-first, single-shallow-capture, closed rebuild. Never touches any field on a
    chapter record other than ``no``/``chapter_id`` -- an unrelated key's value (however hostile)
    is never read, copied, or invoked. ``list(chapter_records)`` captures every element reference
    into a brand-new list via one atomic built-in bulk operation before any length check or
    iteration (V5 Section 2) -- the caller's original list is never length-checked-then-iterated
    live, so growth/mutation of the original list afterward cannot affect this validation. Returns
    ``(identity_mode, closed_records)``."""
    if type(chapter_records) is not list:
        _fail("CHAPTER_RECORDS_INVALID", "chapter_records must be an exact list")
    captured_records = list(chapter_records)
    if len(captured_records) == 0:
        _fail("CHAPTER_RECORDS_INVALID", "chapter_records must be an exact nonempty list")
    n = len(captured_records)

    closed: list = []
    modes: list = []
    for record in captured_records:
        closed_record = _exact_string_keyed_dict_or_none(record)
        if closed_record is None:
            _fail("CHAPTER_RECORD_INVALID", "each chapter record must be an exact dict with exact string keys")
        if "no" not in closed_record:
            _fail("CHAPTER_NUMBER_INVALID", "chapter record 'no' must be present")
        no = closed_record["no"]
        if type(no) is not int:
            _fail("CHAPTER_NUMBER_INVALID", "chapter record 'no' must be an exact int")
        if "chapter_id" not in closed_record:
            _fail("CHAPTER_ID_INVALID", "chapter record must contain chapter_id (null for legacy)")
        chapter_id = closed_record["chapter_id"]
        if chapter_id is None:
            modes.append("legacy_index")
        elif type(chapter_id) is str and _CHAPTER_ID_RE.fullmatch(chapter_id) and _utf8_safe(chapter_id):
            modes.append("stable_v1")
        else:
            _fail("CHAPTER_ID_INVALID", "chapter_id must be null or an exact ch_<32 hex> string")
        closed.append({"no": no, "chapter_id": chapter_id})

    if [item["no"] for item in closed] != list(range(n)):
        _fail("CHAPTER_ORDER_INVALID", "chapter record 'no' values must be exactly 0..N-1 in order")

    identity_mode = modes[0]
    if any(mode != identity_mode for mode in modes):
        _fail("CHAPTER_IDENTITY_MIXED", "chapter identity mode must be uniformly stable_v1 or legacy_index")
    if identity_mode == "stable_v1":
        seen: set = set()
        for item in closed:
            chapter_id = item["chapter_id"]
            if chapter_id in seen:
                _fail("CHAPTER_ID_DUPLICATE", "chapter_id values must be unique")
            seen.add(chapter_id)
    return identity_mode, closed


def _closed_version_bindings(versions: Any):
    """Exact-type-first, single-shallow-capture, closed rebuild of the version-bindings map."""
    captured_versions = _exact_string_keyed_dict_or_none(versions)
    if captured_versions is None or set(captured_versions) != _VERSION_KEYS_SET:
        _fail("VERSION_BINDINGS_INVALID", "versions must be an exact dict with exactly the required keys")
    closed: dict = {}
    schema_version = captured_versions["mutation_hash_schema_version"]
    if type(schema_version) is not str or schema_version != MUTATION_HASH_SCHEMA_VERSION:
        _fail("VERSION_BINDINGS_INVALID", "mutation_hash_schema_version must be the exact fixed schema version")
    closed["mutation_hash_schema_version"] = schema_version
    story_contract_hash = captured_versions["story_contract_hash"]
    if story_contract_hash is not None and not _is_hash64(story_contract_hash):
        _fail("VERSION_BINDINGS_INVALID", "story_contract_hash must be null or an exact lowercase SHA-256 hex string")
    closed["story_contract_hash"] = story_contract_hash
    extractor_epoch = captured_versions["extractor_epoch"]
    if extractor_epoch is not None and (
        type(extractor_epoch) is not int
        or extractor_epoch < 0
        or extractor_epoch > _MAX_BOUND_INTEGER
    ):
        _fail("VERSION_BINDINGS_INVALID", "extractor_epoch must be null or an exact int in [0, 2**63-1]")
    closed["extractor_epoch"] = extractor_epoch
    for key in VERSION_KEYS:
        if key in ("mutation_hash_schema_version", "story_contract_hash", "extractor_epoch"):
            continue
        value = captured_versions[key]
        if value is not None and not _is_bounded_nonblank_str(value, 64):
            _fail("VERSION_BINDINGS_INVALID", f"{key} must be null or an exact nonblank string at most 64 UTF-8 bytes")
        closed[key] = value
    return closed


def _closed_final_metadata(metadata: Any):
    """Exact-type-first, single-shallow-capture, closed rebuild of the final metadata map."""
    captured_metadata = _exact_string_keyed_dict_or_none(metadata)
    if captured_metadata is None or set(captured_metadata) != _FINAL_METADATA_KEYS_SET:
        _fail("FINAL_METADATA_INVALID", "metadata must be an exact dict with exactly the required keys")
    closed: dict = {}
    for key in ("polished", "rag_used"):
        value = captured_metadata[key]
        if type(value) is not bool:
            _fail("FINAL_METADATA_INVALID", f"{key} must be an exact bool")
        closed[key] = value
    n_ok, n_total = captured_metadata["n_ok"], captured_metadata["n_total"]
    if type(n_ok) is not int or n_ok < 0 or n_ok > _MAX_BOUND_INTEGER:
        _fail("FINAL_METADATA_INVALID", "n_ok must be an exact int in [0, 2**63-1]")
    if type(n_total) is not int or n_total < 0 or n_total > _MAX_BOUND_INTEGER:
        _fail("FINAL_METADATA_INVALID", "n_total must be an exact int in [0, 2**63-1]")
    if n_ok > n_total:
        _fail("FINAL_METADATA_INVALID", "n_ok must not exceed n_total")
    closed["n_ok"], closed["n_total"] = n_ok, n_total
    target_language = captured_metadata["target_language"]
    if type(target_language) is not str or not target_language.strip() or not _utf8_safe(target_language):
        _fail("FINAL_METADATA_INVALID", "target_language must be an exact nonblank string")
    closed["target_language"] = target_language
    for key in ("scenario", "strategy", "outline_source", "mode", "model", "manager_model"):
        value = captured_metadata[key]
        if value is not None and (type(value) is not str or not _utf8_safe(value) or len(value.encode("utf-8")) > 256):
            _fail("FINAL_METADATA_INVALID", f"{key} must be null or an exact string at most 256 UTF-8 bytes")
        closed[key] = value
    return closed


def _segment(candidate: str, chapter_records: list):
    """Splits ``candidate`` at exact line-start ``## `` headings and binds each segment to its
    chapter record's identity. Returns ``(chapters, None)`` on a complete, well-formed candidate,
    or ``(None, error_code)`` for a bounded structural segmentation failure -- never guesses by
    list position or a stale ``result['chapters']``, and never exposes an unbounded integer
    conversion for a hostile/oversized numeric heading token."""
    matches = list(_HEADING_RE.finditer(candidate))
    if len(matches) != len(chapter_records):
        return None, "CHAPTER_HEADING_COUNT_MISMATCH"
    chapters = []
    for index, (match, record) in enumerate(zip(matches, chapter_records)):
        heading_text = match.group(0)
        number_match = _NUMBER_RE.match(heading_text)
        if number_match is None:
            return None, "CHAPTER_HEADING_NUMBER_INVALID"
        digits = number_match.group(1)
        if len(digits) > _MAX_HEADING_DIGITS:
            return None, "CHAPTER_HEADING_NUMBER_INVALID"
        tail_pos = number_match.end()
        if tail_pos < len(heading_text) and heading_text[tail_pos].isdecimal():
            return None, "CHAPTER_HEADING_NUMBER_INVALID"
        number = int(digits)
        if digits != str(number):
            return None, "CHAPTER_HEADING_NUMBER_INVALID"
        if number != index + 1:
            return None, "CHAPTER_HEADING_ORDER_MISMATCH"
        if heading_text.endswith("\r\n"):
            heading = heading_text[:-2]
        elif heading_text.endswith(("\r", "\n")):
            heading = heading_text[:-1]
        else:
            heading = heading_text
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(candidate)
        chapter_id = record["chapter_id"]
        envelope = {
            "body": candidate[match.end():body_end],
            "chapter_id": chapter_id,
            "chapter_number": index + 1,
            "heading": heading,
            "legacy_index": index if chapter_id is None else None,
        }
        chapters.append({
            "chapter_id": chapter_id,
            "legacy_index": index if chapter_id is None else None,
            "chapter_number": index + 1,
            "content_hash": _structured_hash(envelope),
        })
    return chapters, None


class MutationHashRecorder:
    """Records the four append-only prose-mutation stages for one job's ``narrate_chapters``
    candidate. Every returned mapping/list is a detached recursive copy of data this module built
    itself from validated closed primitives -- mutating an input or a returned value can never
    alter recorder-owned state. Thread-safe: two concurrent attempts to append the SAME next stage
    yield exactly one append and one bounded rejection."""

    def __init__(self, *, chapter_records: Any, versions: Any) -> None:
        identity_mode, closed_records = _closed_chapter_records(chapter_records)
        closed_versions = _closed_version_bindings(versions)
        self._chapter_records = closed_records
        self._chapter_count = len(closed_records)
        self._identity_mode = identity_mode
        self._version_bindings = closed_versions
        self._version_set_hash = _structured_hash(closed_versions)
        self._stages: list[dict] = []
        self._closed = False
        self._lock = threading.Lock()

    def capture(self, *, stage: Any, candidate_text: Any) -> dict:
        if type(stage) is not str or stage not in STAGE_ORDER:
            _fail("STAGE_INVALID", "stage must be an exact recognized stage name")
        if type(candidate_text) is not str or not _utf8_safe(candidate_text):
            _fail("CANDIDATE_TYPE_INVALID", "candidate_text must be an exact strict-UTF-8 string")
        with self._lock:
            if self._closed:
                _fail("RECORDER_CLOSED", "recorder is already closed")
            if stage == "final":
                _fail("STAGE_INVALID", "the final stage may only be written via finalize()")
            expected_index = len(self._stages)
            if expected_index >= len(STAGE_ORDER) or stage != STAGE_ORDER[expected_index]:
                _fail("STAGE_ORDER_INVALID", "stages must be captured exactly once, in exact order")
            entry = self._build_stage(stage, expected_index, candidate_text, metadata=None)
            self._stages.append(entry)
            return copy.deepcopy(entry)

    def finalize(self, *, candidate_text: Any, metadata: Any) -> dict:
        if type(candidate_text) is not str or not _utf8_safe(candidate_text):
            _fail("CANDIDATE_TYPE_INVALID", "candidate_text must be an exact strict-UTF-8 string")
        closed_metadata = _closed_final_metadata(metadata)
        with self._lock:
            if self._closed:
                _fail("RECORDER_CLOSED", "recorder is already closed")
            expected_index = len(self._stages)
            if expected_index >= len(STAGE_ORDER) or STAGE_ORDER[expected_index] != "final":
                _fail("STAGE_ORDER_INVALID", "stages must be captured exactly once, in exact order")
            entry = self._build_stage("final", expected_index, candidate_text, metadata=closed_metadata)
            self._stages.append(entry)
            self._closed = True
            return self._build_snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            return self._build_snapshot()

    def _build_stage(self, stage: str, sequence: int, candidate_text: str, *, metadata) -> dict:
        candidate_hash = _sha_text(candidate_text)
        chapters, error_code = _segment(candidate_text, self._chapter_records)
        previous_hash = self._stages[-1]["stage_hash"] if self._stages else None
        entry = {
            "schema_version": MUTATION_HASH_SCHEMA_VERSION,
            "stage": stage,
            "sequence": sequence,
            "coverage": "complete" if chapters is not None else "incomplete",
            "error_code": error_code,
            "candidate_hash": candidate_hash,
            "chapters": chapters if chapters is not None else [],
            "chapter_set_hash": _structured_hash(chapters) if chapters is not None else None,
            "version_set_hash": self._version_set_hash,
            "metadata_hash": _structured_hash(metadata) if metadata is not None else None,
            "previous_stage_hash": previous_hash,
        }
        entry["stage_hash"] = _structured_hash(entry)
        return entry

    def _build_snapshot(self) -> dict:
        stages = copy.deepcopy(self._stages)
        valid = len(stages) == len(STAGE_ORDER) and all(s["coverage"] == "complete" for s in stages)
        final_stage = stages[-1] if stages and stages[-1]["stage"] == "final" else None
        snapshot = {
            "schema_version": MUTATION_HASH_SCHEMA_VERSION,
            "hash_algorithm": HASH_ALGORITHM,
            "authenticity": "not_provided",
            "valid": valid,
            "coverage": "complete" if valid else "incomplete",
            "chapter_identity_mode": self._identity_mode,
            "chapter_count": self._chapter_count,
            "version_bindings": copy.deepcopy(self._version_bindings),
            "version_set_hash": self._version_set_hash,
            "stages": stages,
            "final_candidate_hash": final_stage["candidate_hash"] if final_stage is not None else None,
            "final_metadata_hash": final_stage["metadata_hash"] if final_stage is not None else None,
        }
        snapshot["ledger_hash"] = _structured_hash(snapshot)
        return snapshot


def _closed_stage_or_none(stage: Any, *, expected_stage: str, expected_sequence: int,
                           expected_version_set_hash, previous_hash, identity_mode: str,
                           chapter_count: int):
    """Exact-type-first structural AND semantic validation of one stage dict against everything the
    closed schema requires (Section 4) plus V4 Section 2: exact keys/types, stage-order/sequence
    position, complete-vs-incomplete shape, per-stage version-hash repeat, metadata-hash placement,
    the stage's own hash-chain link, and -- for a complete stage -- the full ordered chapter list
    (exact count, contiguous one-based chapter_number by position, and stable_v1/legacy_index
    identity rules with uniqueness). Builds and returns a brand-new detached closed stage dict from
    already-validated plain values if every invariant holds, else ``None`` -- never raises, never
    returns a caller-owned reference."""
    captured_stage = _exact_string_keyed_dict_or_none(stage)
    if captured_stage is None or set(captured_stage) != _STAGE_KEYS:
        return None
    if not (type(captured_stage.get("schema_version")) is str and captured_stage.get("schema_version") == MUTATION_HASH_SCHEMA_VERSION):
        return None
    if not (type(captured_stage.get("stage")) is str and captured_stage.get("stage") == expected_stage):
        return None
    if not (type(captured_stage.get("sequence")) is int and captured_stage.get("sequence") == expected_sequence):
        return None
    coverage = captured_stage.get("coverage")
    if type(coverage) is not str or coverage not in ("complete", "incomplete"):
        return None
    error_code = captured_stage.get("error_code")
    if coverage == "complete":
        if error_code is not None:
            return None
    else:
        if type(error_code) is not str or error_code not in _STRUCTURAL_ERROR_CODES:
            return None
    candidate_hash = captured_stage.get("candidate_hash")
    if not _is_hash64(candidate_hash):
        return None
    raw_chapters = captured_stage.get("chapters")
    chapter_set_hash = captured_stage.get("chapter_set_hash")
    closed_chapters: list = []
    closed_chapter_set_hash = None
    if coverage == "complete":
        # `type(raw_chapters) is not list` alone rejects a `list` SUBCLASS before any comparison
        # ever touches it. `list(raw_chapters)` then shallow-captures every element reference in
        # one atomic bulk operation BEFORE the length check/iteration below, so growth of the
        # caller's original list afterward cannot change what this validation actually sees
        # (V5 Section 2 / V5-R03).
        if type(raw_chapters) is not list:
            return None
        chapters = list(raw_chapters)
        if len(chapters) != chapter_count:
            return None
        seen_ids: set = set()
        for index, chapter in enumerate(chapters):
            captured_chapter = _exact_string_keyed_dict_or_none(chapter)
            if captured_chapter is None or set(captured_chapter) != _CHAPTER_KEYS:
                return None
            chapter_id = captured_chapter.get("chapter_id")
            if chapter_id is not None and type(chapter_id) is not str:
                return None
            legacy_index = captured_chapter.get("legacy_index")
            if legacy_index is not None and type(legacy_index) is not int:
                return None
            chapter_number = captured_chapter.get("chapter_number")
            if type(chapter_number) is not int or chapter_number != index + 1:
                return None
            content_hash = captured_chapter.get("content_hash")
            if not _is_hash64(content_hash):
                return None
            if identity_mode == "stable_v1":
                if legacy_index is not None:
                    return None
                if (
                    type(chapter_id) is not str
                    or not _CHAPTER_ID_RE.fullmatch(chapter_id)
                    or not _utf8_safe(chapter_id)
                ):
                    return None
                if chapter_id in seen_ids:
                    return None
                seen_ids.add(chapter_id)
            else:
                if chapter_id is not None:
                    return None
                if legacy_index != index:
                    return None
            closed_chapters.append({
                "chapter_id": chapter_id,
                "legacy_index": legacy_index,
                "chapter_number": chapter_number,
                "content_hash": content_hash,
            })
        expected_chapter_set_hash = _safe_structured_hash(closed_chapters)
        if expected_chapter_set_hash is None or not _safe_eq(chapter_set_hash, expected_chapter_set_hash, str):
            return None
        closed_chapter_set_hash = expected_chapter_set_hash
    else:
        # Mirrors the `complete` branch above (V5 Section 2 / V5-R03): `type(raw_chapters) is not
        # list` rejects a `list` subclass before any comparison touches it, then `list(raw_chapters)`
        # atomically captures every element reference in one bulk operation BEFORE the length check
        # -- a prior revision called `len(raw_chapters) != 0` directly on the live caller-owned
        # reference here, with no intervening capture, so a caller resize landing between the type
        # check and the length check could make an incomplete stage with a non-empty `chapters` list
        # (a schema violation) evaluate as empty and be wrongly accepted.
        if type(raw_chapters) is not list:
            return None
        chapters = list(raw_chapters)
        if len(chapters) != 0:
            return None
        if chapter_set_hash is not None:
            return None
    if not _safe_eq(captured_stage.get("version_set_hash"), expected_version_set_hash, str):
        return None
    metadata_hash = captured_stage.get("metadata_hash")
    if expected_stage == "final":
        if not _is_hash64(metadata_hash):
            return None
    else:
        if metadata_hash is not None:
            return None
    if not _safe_eq(captured_stage.get("previous_stage_hash"), previous_hash, str):
        return None
    stage_hash = captured_stage.get("stage_hash")
    closed_without_hash = {
        "schema_version": MUTATION_HASH_SCHEMA_VERSION,
        "stage": expected_stage,
        "sequence": expected_sequence,
        "coverage": coverage,
        "error_code": error_code,
        "candidate_hash": candidate_hash,
        "chapters": closed_chapters,
        "chapter_set_hash": closed_chapter_set_hash,
        "version_set_hash": expected_version_set_hash,
        "metadata_hash": metadata_hash,
        "previous_stage_hash": previous_hash,
    }
    # V5 Section 4 / V5-R04: the expected stage_hash is recomputed from the CLOSED structure this
    # function is about to return, never from a fresh/live read of the caller's stage dict -- so a
    # caller mutation between an earlier local capture and this point can never make the returned
    # stage's stored hash describe different bytes than the returned stage itself.
    expected_stage_hash = _safe_structured_hash(closed_without_hash)
    if expected_stage_hash is None or not _safe_eq(stage_hash, expected_stage_hash, str):
        return None
    closed_without_hash["stage_hash"] = stage_hash
    return closed_without_hash


def _closed_snapshot_shape_ok(snapshot: Any):
    """Exact-type-first, fully closed structural/semantic validation of a snapshot per V2 Section 4
    and V4 Sections 2-3. Builds and returns a brand-new, fully detached closed view (fresh dict,
    fresh stage/chapter dicts and lists) containing only already-validated plain built-in values --
    NEVER the caller's own snapshot/stage/chapter object references -- so a caller mutation of its
    own snapshot after this function returns can never alter, corrupt, or crash any later read of
    the accepted result. Also relates top-level ``chapter_count``/``chapter_identity_mode`` to every
    complete stage's own chapter records (passed into `_closed_stage_or_none` as the expected
    values) and requires the chapter identity vector to be identical across every complete stage.
    Returns ``None`` if any invariant fails. Never raises."""
    captured_snapshot = _exact_string_keyed_dict_or_none(snapshot)
    if captured_snapshot is None or set(captured_snapshot) != _SNAPSHOT_KEYS:
        return None
    if not (type(captured_snapshot.get("schema_version")) is str and captured_snapshot.get("schema_version") == MUTATION_HASH_SCHEMA_VERSION):
        return None
    if not (type(captured_snapshot.get("hash_algorithm")) is str and captured_snapshot.get("hash_algorithm") == HASH_ALGORITHM):
        return None
    if not (type(captured_snapshot.get("authenticity")) is str and captured_snapshot.get("authenticity") == "not_provided"):
        return None
    if type(captured_snapshot.get("valid")) is not bool:
        return None
    coverage = captured_snapshot.get("coverage")
    if type(coverage) is not str or coverage not in ("complete", "incomplete"):
        return None
    identity_mode = captured_snapshot.get("chapter_identity_mode")
    if type(identity_mode) is not str or identity_mode not in ("stable_v1", "legacy_index"):
        return None
    chapter_count = captured_snapshot.get("chapter_count")
    if type(chapter_count) is not int or chapter_count <= 0:
        return None
    version_bindings = captured_snapshot.get("version_bindings")
    try:
        closed_version_bindings = _closed_version_bindings(version_bindings)
    except MutationHashError:
        return None
    expected_version_set_hash = _safe_structured_hash(closed_version_bindings)
    if expected_version_set_hash is None or not _safe_eq(captured_snapshot.get("version_set_hash"), expected_version_set_hash, str):
        return None

    raw_stages = captured_snapshot.get("stages")
    # `list(raw_stages)` shallow-captures every stage-element reference in one atomic bulk
    # operation BEFORE the length check/iteration, so growth of the caller's original list
    # afterward (e.g. appending a 5th entry mid-validation) cannot change what this validation
    # actually sees (V5 Section 2 / V5-R03) -- the loop below can never run past the length this
    # captured list had at this exact instant.
    if type(raw_stages) is not list:
        return None
    stages = list(raw_stages)
    if len(stages) > len(STAGE_ORDER):
        return None
    previous = None
    closed_stages: list = []
    for i, stage in enumerate(stages):
        validated_stage = _closed_stage_or_none(
            stage, expected_stage=STAGE_ORDER[i], expected_sequence=i,
            expected_version_set_hash=expected_version_set_hash, previous_hash=previous,
            identity_mode=identity_mode, chapter_count=chapter_count)
        if validated_stage is None:
            return None
        closed_stages.append(validated_stage)
        previous = validated_stage["stage_hash"]

    # Cross-stage identity closure (V4-R01): the identity vector (chapter_id, legacy_index) per
    # chapter position must be identical across every complete stage, even though content_hash may
    # legitimately change stage to stage because prose changes.
    identity_vectors = [
        tuple((c["chapter_id"], c["legacy_index"]) for c in s["chapters"])
        for s in closed_stages if s["coverage"] == "complete"
    ]
    if identity_vectors and any(vector != identity_vectors[0] for vector in identity_vectors[1:]):
        return None

    all_complete = len(closed_stages) == len(STAGE_ORDER) and all(s["coverage"] == "complete" for s in closed_stages)
    if captured_snapshot.get("valid") != all_complete:
        return None
    expected_coverage = "complete" if all_complete else "incomplete"
    if coverage != expected_coverage:
        return None

    final_stage = closed_stages[-1] if closed_stages and closed_stages[-1]["stage"] == "final" else None
    final_candidate_hash = captured_snapshot.get("final_candidate_hash")
    final_metadata_hash = captured_snapshot.get("final_metadata_hash")
    if final_stage is not None:
        # `final_stage[...]` is our own detached rebuild (already confirmed exact-hash64); the
        # untrusted `final_candidate_hash`/`final_metadata_hash` side is type-confirmed by
        # `_safe_eq` before any comparison touches it.
        if not _safe_eq(final_candidate_hash, final_stage["candidate_hash"], str):
            return None
        if not _safe_eq(final_metadata_hash, final_stage["metadata_hash"], str):
            return None
    else:
        if final_candidate_hash is not None or final_metadata_hash is not None:
            return None

    ledger_hash = captured_snapshot.get("ledger_hash")
    closed_without_hash = {
        "schema_version": MUTATION_HASH_SCHEMA_VERSION,
        "hash_algorithm": HASH_ALGORITHM,
        "authenticity": "not_provided",
        "valid": all_complete,
        "coverage": expected_coverage,
        "chapter_identity_mode": identity_mode,
        "chapter_count": chapter_count,
        "version_bindings": closed_version_bindings,
        "version_set_hash": expected_version_set_hash,
        "stages": closed_stages,
        "final_candidate_hash": final_candidate_hash,
        "final_metadata_hash": final_metadata_hash,
    }
    # V5 Section 4 / V5-R04: the expected ledger_hash is recomputed from the CLOSED structure this
    # function is about to return, never from a fresh/live read of the caller's snapshot dict --
    # so a caller mutation between an earlier local capture and this point can never make the
    # returned snapshot's stored hash describe different bytes than the returned snapshot itself.
    expected_ledger_hash = _safe_structured_hash(closed_without_hash)
    if expected_ledger_hash is None or not _safe_eq(ledger_hash, expected_ledger_hash, str):
        return None

    return {**closed_without_hash, "ledger_hash": expected_ledger_hash}


def verify_final_binding(snapshot: Any, *, candidate_text: Any, metadata: Any, versions: Any,
                          chapter_records: Any) -> dict:
    """Independently parses and validates a closed exact snapshot without raising, then -- only
    once every internal invariant in ``_closed_snapshot_shape_ok`` holds -- re-derives the final
    candidate, final metadata, version binding, and chapter identity from its trusted caller's own
    inputs (never from the snapshot itself). When the trusted candidate is confirmed non-stale for
    THIS snapshot (its hash already matches the final stage's own candidate_hash), independently
    re-segments that candidate against the trusted chapter records and requires the final stage's
    own chapter identities/numbers/content hashes/chapter-set hash to match that re-derivation
    exactly -- a forged final-stage record fails integrity even when fully re-sealed; a genuinely
    stale candidate is reported as staleness, never mistaken for internal forgery. Never raises and
    never repairs; malformed input converts to a bounded verdict. ``authenticity`` is always
    ``"not_provided"``: a fully re-sealed, internally self-consistent plain-JSON forgery must never
    be reported as authenticated.

    V7 Section 2: ``chapter_records`` is closed via ``_closed_chapter_records`` exactly ONCE, right
    here, for the whole call. Both the trusted-candidate re-segmentation check and the identity/
    count check below consume this SAME ``identity_mode``/``closed_records_for_identity`` pair -- neither one
    calls ``_closed_chapter_records`` again. A prior revision called it twice (once per check); a
    caller growing/shrinking/reordering/replacing an element in the window between those two calls
    made one check see a different view than the other, producing a false ``CHAPTER_IDENTITY_STALE``
    for evidence that was fully consistent with either view alone. Closing once and reusing the
    result makes that window impossible: every downstream check in this pass now necessarily agrees
    on what "the chapter records" were, and a caller mutation after this point can never be observed
    by any part of this function."""
    try:
        identity_mode, closed_records_for_identity = _closed_chapter_records(chapter_records)
    except MutationHashError:
        identity_mode, closed_records_for_identity = None, None

    validated = _closed_snapshot_shape_ok(snapshot)
    integrity_valid = validated is not None

    def _sourced(name: str):
        # V4 Section 3 / V5 Section 3: once `validated` exists it is a brand-new detached view
        # built entirely from already-confirmed plain values -- every later read comes from THIS
        # object, never from the caller-owned `snapshot` again, so a caller mutation of `snapshot`
        # after `_closed_snapshot_shape_ok` returns can never change, corrupt, or crash this
        # function. When validation has already failed, `snapshot`'s own keys never passed
        # validation either, so this NEVER falls back to reading `snapshot` again (a replaced/
        # foreign-keyed root field would otherwise invoke a hostile hook on a plain `.get()`) --
        # it returns a safe fixed default instead.
        if validated is not None:
            return validated.get(name)
        return None

    errors: list[str] = []

    coverage = "incomplete"
    final_stage = None
    if integrity_valid:
        coverage = validated["coverage"]
        stages = validated["stages"]
        if stages and stages[-1]["stage"] == "final":
            final_stage = stages[-1]

        if final_stage is not None:
            try:
                candidate_hash_now = _sha_text(candidate_text) if type(candidate_text) is str and _utf8_safe(candidate_text) else None
            except Exception:  # noqa: BLE001
                candidate_hash_now = None
            if candidate_hash_now is not None and candidate_hash_now == final_stage["candidate_hash"]:
                # V7 Section 2: reuse the single upfront closure -- never a second independent
                # `_closed_chapter_records(chapter_records)` read here.
                if closed_records_for_identity is None:
                    expected_chapters, expected_seg_error = None, None
                else:
                    expected_chapters, expected_seg_error = _segment(candidate_text, closed_records_for_identity)
                if final_stage["coverage"] == "complete":
                    if expected_chapters is None or final_stage["chapters"] != expected_chapters:
                        integrity_valid = False
                    elif _safe_structured_hash(expected_chapters) != final_stage["chapter_set_hash"]:
                        integrity_valid = False
                else:
                    # V6 Section 4: a final stage that legitimately reports coverage="incomplete"
                    # must independently re-derive to the SAME "no chapters" outcome AND the SAME
                    # fixed structural error code -- a genuinely incomplete chain is not corruption,
                    # but a re-sealed stage whose stored error disagrees with fresh segmentation
                    # (of the very candidate the stage claims to describe) still is.
                    if expected_chapters is not None or expected_seg_error != final_stage["error_code"]:
                        integrity_valid = False

    if not integrity_valid:
        errors.append("SNAPSHOT_INTEGRITY_INVALID")

    final_binding_valid = integrity_valid

    try:
        expected_candidate_hash = _sha_text(candidate_text) if type(candidate_text) is str and _utf8_safe(candidate_text) else None
    except Exception:  # noqa: BLE001
        expected_candidate_hash = None
    if expected_candidate_hash is None or not _safe_eq(_sourced("final_candidate_hash"), expected_candidate_hash, str):
        final_binding_valid = False
        errors.append("FINAL_CANDIDATE_STALE")

    try:
        closed_metadata = _closed_final_metadata(metadata)
        expected_metadata_hash = _structured_hash(closed_metadata)
    except MutationHashError:
        expected_metadata_hash = None
    if expected_metadata_hash is None or not _safe_eq(_sourced("final_metadata_hash"), expected_metadata_hash, str):
        final_binding_valid = False
        errors.append("FINAL_METADATA_STALE")

    try:
        expected_version_set_hash = _structured_hash(_closed_version_bindings(versions))
    except MutationHashError:
        expected_version_set_hash = None
    if expected_version_set_hash is None or not _safe_eq(_sourced("version_set_hash"), expected_version_set_hash, str):
        final_binding_valid = False
        errors.append("VERSION_BINDING_STALE")

    # V7 Section 2: `identity_mode`/`closed_records_for_identity` were already closed exactly once
    # at the top of this call -- reused here rather than re-derived, so this check and the
    # re-segmentation check above always agree on what "the chapter records" were for this pass.
    expected_identity = None
    if closed_records_for_identity is not None:
        expected_identity = [
            (record["chapter_id"], (index if record["chapter_id"] is None else None))
            for index, record in enumerate(closed_records_for_identity)
        ]
    actual_identity = None
    if final_stage is not None and type(final_stage.get("chapters")) is list:
        try:
            actual_identity = [(c["chapter_id"], c["legacy_index"]) for c in final_stage["chapters"]]
        except Exception:  # noqa: BLE001
            actual_identity = None
    expected_count = len(closed_records_for_identity) if closed_records_for_identity is not None else None
    if (
        identity_mode is None
        or not _safe_eq(_sourced("chapter_identity_mode"), identity_mode, str)
        or not _safe_eq(_sourced("chapter_count"), expected_count, int)
        or expected_identity != actual_identity
    ):
        final_binding_valid = False
        errors.append("CHAPTER_IDENTITY_STALE")

    seen: set = set()
    ordered_errors = []
    for code in errors:
        if code not in seen:
            seen.add(code)
            ordered_errors.append(code)

    return {
        "integrity_valid": integrity_valid,
        "final_binding_valid": final_binding_valid,
        "coverage": coverage,
        "authenticity": "not_provided",
        "errors": ordered_errors,
    }


__all__ = [
    "MUTATION_HASH_SCHEMA_VERSION", "HASH_ALGORITHM", "STAGE_ORDER", "VERSION_KEYS",
    "FINAL_METADATA_KEYS", "MutationHashError", "MutationHashRecorder", "verify_final_binding",
]
