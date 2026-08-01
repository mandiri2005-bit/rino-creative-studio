"""Canon Lite — L1 strict foundation (schemas, validation, hashing, renderer).

Scope of THIS file (package L1 of CANON-LITE-ARCHITECTURE-FINAL.md §13):

  * `CanonLiteV1` and `JobConfigSnapshotV1` — two immutable, exact-schema artifacts.
  * Strict validation: closed field sets, closed enums, explicit bounds. An unknown
    field is an ERROR, never ignored. An unknown FACT is stated explicitly
    (`UNKNOWN` / `UNKNOWN_ORDER`), never silently dropped and never invented.
  * Canonical serialization + SHA-256 over a domain-separated preimage.
  * A deterministic renderer (built and hashed in L1; injection is L3).
  * A shared-context freeze that is tamper-EVIDENT by default and tamper-PROOF on
    request (`hard=True`), for C2/I08.

Explicitly NOT in this file, by design:

  * No provider call of any kind. The L1 canon is a DETERMINISTIC projection of the
    accepted outline plus the resolved job configuration. §11 caps Canon Lite at one
    steady-state construction call; L1 spends ZERO, so the cap cannot be breached and
    no second serial planner is stacked (§11, and the I09 constraint).
  * No prompt injection, no repair, no persistence, no metering, no payload change.
  * No semantic extraction. Entities, anchors, one-time events and reveals are
    populated ONLY from explicitly structured job inputs. The prose Story Bible is
    ADVISORY: it contributes a provenance hash and nothing else. §6.1 — "The existing
    prose Bible may be advisory input, but it is not authority over code-owned
    outline identity, order, or language." §7.1 alias-authority rule — an alias is
    authoritative only when explicitly present in an accepted input.

Privacy (C12, §10): every value that leaves this module for a log or a metric is a
bounded label, a count, or a hash. `render_canon()` output and canonical names are
prompt material, never telemetry — use `telemetry_digest()` for anything observable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, fields as _dc_fields
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "SCHEMA_VERSION",
    "UNKNOWN",
    "UNKNOWN_ORDER",
    "MODE_OFF",
    "MODE_SHADOW",
    "MODE_ASSIST",
    "MODE_ENFORCE",
    "CanonLiteError",
    "CanonSchemaError",
    "CanonBoundsError",
    "CanonFrozenError",
    "CanonChapterV1",
    "CanonEntityV1",
    "CanonAnchorV1",
    "CanonEventV1",
    "CanonRevealV1",
    "CanonFlashbackExceptionV1",
    "JobConfigSnapshotV1",
    "CanonLiteV1",
    "SharedContextFreeze",
    "resolve_mode",
    "canonical_bytes",
    "sha256_hex",
    "build_job_config_snapshot",
    "build_canon_lite_v1",
    "parse_canon_lite_v1",
    "render_canon",
    "telemetry_digest",
]

# ===========================================================================
# Constants — versions, sentinels, bounds, closed enums
# ===========================================================================

SCHEMA_VERSION = "canon_lite_v1"
JOB_CONFIG_SCHEMA_VERSION = "job_config_snapshot_v1"

#: Explicit "this fact is not known" marker for STRING fields. §6 requires unknown to
#: be represented explicitly; an empty string or a missing key is NOT acceptable.
UNKNOWN = "__unknown__"

#: Explicit "this placement is not known" marker for 1-based ORDER fields.
UNKNOWN_ORDER = -1

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ASSIST = "assist"
MODE_ENFORCE = "enforce"
_MODES = (MODE_OFF, MODE_SHADOW, MODE_ASSIST, MODE_ENFORCE)

MODE_ENV_VAR = "NARASI_CANON_LITE_MODE"

FACT_SOURCE_POLICIES = ("fiction_generated", "user_supplied", "source_grounded", "unknown")
ALIAS_SOURCES = ("job_input", "canon_source", "none")
ANCHOR_KINDS = ("time", "quantity")
FLASHBACK_REASON_CODES = ("declared_flashback", "declared_retelling", "unknown")

# Bounds. Every collection and every string is bounded; an over-long input is an
# ERROR, never a silent truncation (a truncated canon that still hashes is a forgery
# surface).
MAX_CHAPTERS = 400
MAX_ENTITIES = 400
MAX_ALIASES_PER_ENTITY = 32
MAX_ANCHORS = 400
MAX_EVENTS = 400
MAX_REVEALS = 400
MAX_FLASHBACK_EXCEPTIONS = 400
MAX_ID_LEN = 64
MAX_NAME_LEN = 200
MAX_TITLE_LEN = 300
MAX_LITERAL_LEN = 200
MAX_LANGUAGE_LEN = 32

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,%d}$" % (MAX_ID_LEN - 1))
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CanonLiteError(Exception):
    """Base class for every Canon Lite failure."""


class CanonSchemaError(CanonLiteError):
    """An exact-schema violation: unknown field, wrong type, or closed-enum miss."""


class CanonBoundsError(CanonLiteError):
    """A declared bound was exceeded."""


class CanonFrozenError(CanonLiteError):
    """A hard-frozen shared context was mutated (C2/I08)."""


# ===========================================================================
# Canonical serialization + hashing
# ===========================================================================

def canonical_bytes(obj: Any) -> bytes:
    """Serialize `obj` to the ONE canonical byte string used for hashing.

    Deterministic across processes and interpreter runs: sorted keys, no insignificant
    whitespace, no NaN/Infinity, UTF-8. `ensure_ascii=False` keeps non-Latin canonical
    names as their own code points so the digest is stable under any locale.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(domain: str, obj: Any) -> str:
    """Domain-separated SHA-256 over the canonical bytes of `obj`.

    The domain tag prevents a `CanonLiteV1` preimage from ever colliding with a
    `JobConfigSnapshotV1` preimage that happens to canonicalize identically.
    """
    return sha256_hex(domain.encode("utf-8") + b"\x00" + canonical_bytes(obj))


def _norm_text(value: str) -> str:
    """NFC-normalize so visually identical names hash identically."""
    return unicodedata.normalize("NFC", value)


# ===========================================================================
# Field validators
# ===========================================================================

def _req_str(value: Any, field: str, *, max_len: int, allow_unknown: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise CanonSchemaError(f"{field}: expected str, got {type(value).__name__}")
    if value == UNKNOWN:
        if not allow_unknown:
            raise CanonSchemaError(f"{field}: UNKNOWN is not permitted for this field")
        return value
    text = _norm_text(value)
    if not text.strip():
        # An empty string is the silent-unknown that §6 forbids. Callers must say
        # UNKNOWN out loud.
        raise CanonSchemaError(f"{field}: empty; use UNKNOWN to state an unknown fact")
    if len(text) > max_len:
        raise CanonBoundsError(f"{field}: length {len(text)} exceeds {max_len}")
    return text


def _req_id(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise CanonSchemaError(f"{field}: expected str id, got {type(value).__name__}")
    if not _ID_RE.match(value):
        raise CanonSchemaError(
            f"{field}: {value!r} is not a canonical id "
            f"(lowercase [a-z0-9][a-z0-9_.-]* up to {MAX_ID_LEN} chars)")
    return value


def _req_sha256(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str) or not _SHA256_RE.match(value):
        raise CanonSchemaError(f"{field}: expected a lowercase 64-hex sha256")
    return value


def _req_enum(value: Any, field: str, allowed: Sequence[str]) -> str:
    if isinstance(value, bool) or not isinstance(value, str) or value not in allowed:
        raise CanonSchemaError(f"{field}: {value!r} not in {tuple(allowed)}")
    return value


def _req_order(value: Any, field: str, *, count: int, allow_unknown: bool = True) -> int:
    # bool is an int subclass; a True that means "chapter 1" is a bug, not a value.
    if isinstance(value, bool) or not isinstance(value, int):
        raise CanonSchemaError(f"{field}: expected int, got {type(value).__name__}")
    if value == UNKNOWN_ORDER:
        if not allow_unknown:
            raise CanonSchemaError(f"{field}: UNKNOWN_ORDER is not permitted here")
        return value
    if not (1 <= value <= count):
        raise CanonBoundsError(
            f"{field}: {value} outside 1..{count} (use UNKNOWN_ORDER for unknown)")
    return value


def _reject_unknown_fields(mapping: Mapping[str, Any], allowed: Sequence[str], where: str) -> None:
    if not isinstance(mapping, Mapping):
        raise CanonSchemaError(f"{where}: expected a mapping, got {type(mapping).__name__}")
    extra = sorted(set(mapping) - set(allowed), key=repr)
    if extra:
        raise CanonSchemaError(f"{where}: unknown field(s) {extra}")
    missing = sorted(set(allowed) - set(mapping))
    if missing:
        raise CanonSchemaError(f"{where}: missing field(s) {missing}")


# ===========================================================================
# §6.1 component artifacts
# ===========================================================================

def _bind_norm(obj: Any, field: str) -> None:
    """NFC-normalize a text field IN PLACE at construction.

    Normalizing inside a validator is not enough: these artifacts are frozen, so a
    validator that normalizes and returns a value nobody assigns leaves the original
    bytes in the object and in its hash. Binding it here is what makes
    "equal names hash equally" true rather than merely claimed.
    """
    value = getattr(obj, field, None)
    if isinstance(value, str) and value != UNKNOWN:
        normalized = unicodedata.normalize("NFC", value)
        if normalized != value:
            object.__setattr__(obj, field, normalized)
    elif isinstance(value, tuple):
        normalized_t = tuple(
            unicodedata.normalize("NFC", v) if isinstance(v, str) else v for v in value)
        if normalized_t != value:
            object.__setattr__(obj, field, normalized_t)


@dataclass(frozen=True, slots=True)
class CanonChapterV1:
    """One chapter's code-owned identity, taken from the ACCEPTED OUTLINE only."""
    chapter_id: str
    order: int              # 1-based; never UNKNOWN_ORDER — identity is code-owned
    expected_title: str     # UNKNOWN when the outline supplied no title

    def __post_init__(self) -> None:
        _bind_norm(self, "expected_title")

    def to_canonical_obj(self) -> dict[str, Any]:
        return {"chapter_id": self.chapter_id, "order": self.order,
                "expected_title": self.expected_title}


@dataclass(frozen=True, slots=True)
class CanonEntityV1:
    entity_id: str
    canonical_name: str
    aliases: tuple[str, ...]
    alias_source: str       # ALIAS_SOURCES — "none" when `aliases` is empty

    def __post_init__(self) -> None:
        _bind_norm(self, "canonical_name")
        _bind_norm(self, "aliases")

    def to_canonical_obj(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "canonical_name": self.canonical_name,
                "aliases": list(self.aliases), "alias_source": self.alias_source}


@dataclass(frozen=True, slots=True)
class CanonAnchorV1:
    anchor_id: str
    kind: str               # ANCHOR_KINDS
    literal: str            # the exact declared literal

    def __post_init__(self) -> None:
        _bind_norm(self, "literal")

    def to_canonical_obj(self) -> dict[str, Any]:
        return {"anchor_id": self.anchor_id, "kind": self.kind, "literal": self.literal}


@dataclass(frozen=True, slots=True)
class CanonEventV1:
    event_id: str
    occurs_chapter_order: int   # 1..N or UNKNOWN_ORDER

    def to_canonical_obj(self) -> dict[str, Any]:
        return {"event_id": self.event_id,
                "occurs_chapter_order": self.occurs_chapter_order}


@dataclass(frozen=True, slots=True)
class CanonRevealV1:
    reveal_id: str
    planned_chapter_order: int  # 1..N or UNKNOWN_ORDER

    def to_canonical_obj(self) -> dict[str, Any]:
        return {"reveal_id": self.reveal_id,
                "planned_chapter_order": self.planned_chapter_order}


@dataclass(frozen=True, slots=True)
class CanonFlashbackExceptionV1:
    exception_id: str
    chapter_order: int          # 1..N or UNKNOWN_ORDER
    reason_code: str            # FLASHBACK_REASON_CODES — a label, never prose

    def to_canonical_obj(self) -> dict[str, Any]:
        return {"exception_id": self.exception_id, "chapter_order": self.chapter_order,
                "reason_code": self.reason_code}


# ===========================================================================
# Immutable job-configuration snapshot
# ===========================================================================

@dataclass(frozen=True, slots=True)
class JobConfigSnapshotV1:
    """The resolved job configuration, bound ONCE before canon construction.

    §6.1: "Random choice occurs once before canon construction, is bound into the job
    snapshot, and is never re-rolled by retry or reconciliation."

    On the `_run_narration_job → router → narrate_chapters` path, `genre`, `subgenre`
    and `twist_variant_id` are NOT resolvable: `select_beatmap()` runs only in the
    separate outline endpoint and its `twist_id` is never read back on this path. They
    are therefore bound as UNKNOWN. That is a true statement about this path — it is
    not a placeholder to be filled in with a guess later.
    """
    schema_version: str
    target_language: str
    narration_style: str        # or UNKNOWN
    genre: str                  # or UNKNOWN
    subgenre: str               # or UNKNOWN
    twist_variant_id: str       # or UNKNOWN
    outline_sha256: str
    chapter_count: int
    config_sha256: str

    def __post_init__(self) -> None:
        # A frozen dataclass is not automatically a valid artifact. Validate and bind
        # normalization at the construction boundary so callers cannot instantiate a
        # plausible-looking snapshot with an arbitrary hash and have the canon trust it.
        for name in ("target_language", "narration_style", "genre", "subgenre",
                     "twist_variant_id"):
            _bind_norm(self, name)
        _req_enum(self.schema_version, "schema_version", (JOB_CONFIG_SCHEMA_VERSION,))
        _req_str(self.target_language, "target_language",
                 max_len=MAX_LANGUAGE_LEN, allow_unknown=True)
        _req_str(self.narration_style, "narration_style",
                 max_len=MAX_NAME_LEN, allow_unknown=True)
        _req_str(self.genre, "genre", max_len=MAX_NAME_LEN, allow_unknown=True)
        _req_str(self.subgenre, "subgenre", max_len=MAX_NAME_LEN, allow_unknown=True)
        _req_str(self.twist_variant_id, "twist_variant_id",
                 max_len=MAX_ID_LEN, allow_unknown=True)
        _req_sha256(self.outline_sha256, "outline_sha256")
        if isinstance(self.chapter_count, bool) or not isinstance(self.chapter_count, int):
            raise CanonSchemaError(
                f"chapter_count: expected int, got {type(self.chapter_count).__name__}")
        if not (1 <= self.chapter_count <= MAX_CHAPTERS):
            raise CanonBoundsError(
                f"chapter_count: {self.chapter_count} outside 1..{MAX_CHAPTERS}")
        _req_sha256(self.config_sha256, "config_sha256")
        if not self.verify_sha256():
            raise CanonSchemaError("config_sha256: does not bind this job configuration")

    def _hashed_obj(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "target_language": self.target_language,
            "narration_style": self.narration_style,
            "genre": self.genre,
            "subgenre": self.subgenre,
            "twist_variant_id": self.twist_variant_id,
            "outline_sha256": self.outline_sha256,
            "chapter_count": self.chapter_count,
        }

    def compute_sha256(self) -> str:
        return _digest("canon_lite.job_config.v1", self._hashed_obj())

    def verify_sha256(self) -> bool:
        return self.config_sha256 == self.compute_sha256()

    def to_canonical_obj(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "target_language": self.target_language,
            "narration_style": self.narration_style,
            "genre": self.genre,
            "subgenre": self.subgenre,
            "twist_variant_id": self.twist_variant_id,
            "outline_sha256": self.outline_sha256,
            "chapter_count": self.chapter_count,
            "config_sha256": self.config_sha256,
        }


_JOB_CONFIG_HASHED_FIELDS = (
    "schema_version", "target_language", "narration_style", "genre", "subgenre",
    "twist_variant_id", "outline_sha256", "chapter_count",
)


def _canonical_outline_chapters(
    outline_chapters: Sequence[Mapping[str, Any]],
) -> tuple[CanonChapterV1, ...]:
    """Validate and canonicalize the accepted outline's identity exactly once.

    The old builder hashed the full title but silently truncated the canon title, and
    it hashed a raw outline id even when the canon replaced that id. One accepted
    outline could therefore have two different "identity" projections. This helper is
    the single projection used by both the job snapshot and the canon.
    """
    source = list(outline_chapters or [])
    if not source:
        raise CanonSchemaError("outline_chapters: empty; canon requires an accepted outline")
    if len(source) > MAX_CHAPTERS:
        raise CanonBoundsError(f"chapter_count: {len(source)} exceeds {MAX_CHAPTERS}")

    rows: list[CanonChapterV1] = []
    for i, ch in enumerate(source):
        if not isinstance(ch, Mapping):
            raise CanonSchemaError(
                f"outline_chapters[{i}]: expected a mapping, got {type(ch).__name__}")
        raw_id_value = ch.get("id")
        if raw_id_value is None or raw_id_value == "":
            raw_id = str(i + 1)
        elif isinstance(raw_id_value, bool) or not isinstance(raw_id_value, (int, str)):
            raise CanonSchemaError(
                f"outline_chapters[{i}].id: expected int or str, "
                f"got {type(raw_id_value).__name__}")
        else:
            raw_id = str(raw_id_value).strip().lower()
        chapter_id = raw_id if _ID_RE.match(raw_id) else f"ch{i + 1}"
        raw_title = ch.get("title")
        if raw_title is None or raw_title == "":
            title = UNKNOWN
        else:
            if isinstance(raw_title, bool) or not isinstance(raw_title, str):
                raise CanonSchemaError(
                    f"outline_chapters[{i}].title: expected str, "
                    f"got {type(raw_title).__name__}")
            stripped = raw_title.strip()
            title = (_req_str(stripped, f"outline_chapters[{i}].title",
                              max_len=MAX_TITLE_LEN)
                     if stripped else UNKNOWN)
        rows.append(CanonChapterV1(
            chapter_id=chapter_id, order=i + 1, expected_title=title))

    # Duplicate/colliding input ids make every id order-derived. Applying the fallback
    # to the complete set keeps the identity projection deterministic.
    if len({row.chapter_id for row in rows}) != len(rows):
        rows = [
            CanonChapterV1(
                chapter_id=f"ch{row.order}", order=row.order,
                expected_title=row.expected_title)
            for row in rows
        ]
    return tuple(rows)


def outline_digest(outline_chapters: Sequence[Mapping[str, Any]]) -> str:
    """Hash the ACCEPTED OUTLINE's identity-bearing shape only.

    Deliberately narrow: chapter order, id, and title. Summaries are prose and are not
    part of chapter IDENTITY — including them would make the outline hash churn on
    every re-word and would drag prose into a value we compare and report.
    """
    rows = [
        {"order": ch.order, "id": ch.chapter_id, "title": ch.expected_title}
        for ch in _canonical_outline_chapters(outline_chapters)
    ]
    return _digest("canon_lite.outline.v1", rows)


def build_job_config_snapshot(
    *,
    outline_chapters: Sequence[Mapping[str, Any]],
    target_language: str,
    narration_style: Optional[str],
    genre: str = UNKNOWN,
    subgenre: str = UNKNOWN,
    twist_variant_id: str = UNKNOWN,
) -> JobConfigSnapshotV1:
    """Bind the resolved configuration once. Pure; no I/O, no clock, no randomness."""
    chapters = list(outline_chapters or [])
    canonical_outline = _canonical_outline_chapters(chapters)

    style_val = (
        UNKNOWN
        if narration_style is None
        or (isinstance(narration_style, str) and not narration_style.strip())
        else narration_style
    )
    payload = {
        "schema_version": JOB_CONFIG_SCHEMA_VERSION,
        "target_language": _req_str(target_language, "target_language",
                                    max_len=MAX_LANGUAGE_LEN, allow_unknown=True),
        "narration_style": _req_str(style_val, "narration_style",
                                    max_len=MAX_NAME_LEN, allow_unknown=True),
        "genre": _req_str(genre, "genre", max_len=MAX_NAME_LEN, allow_unknown=True),
        "subgenre": _req_str(subgenre, "subgenre", max_len=MAX_NAME_LEN, allow_unknown=True),
        "twist_variant_id": _req_str(twist_variant_id, "twist_variant_id",
                                     max_len=MAX_ID_LEN, allow_unknown=True),
        "outline_sha256": _digest(
            "canon_lite.outline.v1",
            [{"order": ch.order, "id": ch.chapter_id, "title": ch.expected_title}
             for ch in canonical_outline]),
        "chapter_count": len(canonical_outline),
    }
    config_sha256 = _digest("canon_lite.job_config.v1",
                            {k: payload[k] for k in _JOB_CONFIG_HASHED_FIELDS})
    return JobConfigSnapshotV1(config_sha256=config_sha256, **payload)


# ===========================================================================
# CanonLiteV1
# ===========================================================================

_CANON_FIELDS = (
    "schema_version", "outline_sha256", "generation_config_sha256", "target_language",
    "chapters", "entities", "anchors", "one_time_events", "reveals",
    "flashback_exceptions", "fact_source_policy", "advisory_bible_sha256", "canon_sha256",
)


@dataclass(frozen=True, slots=True)
class CanonLiteV1:
    """The immutable pre-MAP canon (§6.1). Read-only; supplied identically to workers."""
    schema_version: str
    outline_sha256: str
    generation_config_sha256: str
    target_language: str
    chapters: tuple[CanonChapterV1, ...]
    entities: tuple[CanonEntityV1, ...]
    anchors: tuple[CanonAnchorV1, ...]
    one_time_events: tuple[CanonEventV1, ...]
    reveals: tuple[CanonRevealV1, ...]
    flashback_exceptions: tuple[CanonFlashbackExceptionV1, ...]
    fact_source_policy: str
    #: Provenance of the ADVISORY prose Story Bible, or UNKNOWN when none was pinned.
    #: A hash only — the bible's text never enters this artifact, because it is not
    #: authority (§6.1) and because prose must not travel in a comparable value (§10).
    advisory_bible_sha256: str
    canon_sha256: str

    def __post_init__(self) -> None:
        # Public construction must be just as strict as parsing. Without this boundary,
        # a caller can instantiate an invalid frozen dataclass and pass it directly to
        # the renderer or future assist/enforce code without ever touching the parser.
        _bind_norm(self, "target_language")
        _validate_canon_instance(self)

    # -- canonical form ---------------------------------------------------
    def to_canonical_obj(self, *, include_hash: bool = True) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "schema_version": self.schema_version,
            "outline_sha256": self.outline_sha256,
            "generation_config_sha256": self.generation_config_sha256,
            "target_language": self.target_language,
            "chapters": [c.to_canonical_obj() for c in self.chapters],
            "entities": [e.to_canonical_obj() for e in self.entities],
            "anchors": [a.to_canonical_obj() for a in self.anchors],
            "one_time_events": [e.to_canonical_obj() for e in self.one_time_events],
            "reveals": [r.to_canonical_obj() for r in self.reveals],
            "flashback_exceptions": [f.to_canonical_obj() for f in self.flashback_exceptions],
            "fact_source_policy": self.fact_source_policy,
            "advisory_bible_sha256": self.advisory_bible_sha256,
        }
        if include_hash:
            obj["canon_sha256"] = self.canon_sha256
        return obj

    def compute_sha256(self) -> str:
        """Recompute the canon hash from the artifact's own content."""
        return _digest("canon_lite.canon.v1", self.to_canonical_obj(include_hash=False))

    def verify_sha256(self) -> bool:
        """True when `canon_sha256` still binds this exact content."""
        return self.canon_sha256 == self.compute_sha256()

    @property
    def chapter_count(self) -> int:
        return len(self.chapters)


def _validate_chapters(rows: Sequence[CanonChapterV1]) -> tuple[CanonChapterV1, ...]:
    if not rows:
        raise CanonSchemaError("chapters: empty; canon requires an accepted outline")
    if len(rows) > MAX_CHAPTERS:
        raise CanonBoundsError(f"chapters: {len(rows)} exceeds {MAX_CHAPTERS}")
    seen_ids: set[str] = set()
    for i, ch in enumerate(rows):
        if not isinstance(ch, CanonChapterV1):
            raise CanonSchemaError(
                f"chapters[{i}]: expected CanonChapterV1, got {type(ch).__name__}")
        _req_id(ch.chapter_id, f"chapters[{i}].chapter_id")
        if ch.chapter_id in seen_ids:
            raise CanonSchemaError(f"chapters[{i}].chapter_id: duplicate {ch.chapter_id!r}")
        seen_ids.add(ch.chapter_id)
        if isinstance(ch.order, bool) or not isinstance(ch.order, int) or ch.order != i + 1:
            # Exact expected ORDER, not merely a sorted set: §6.1 binds order, and a
            # gap or a swap is precisely the defect predicate #1 must catch.
            raise CanonSchemaError(
                f"chapters[{i}].order: expected exactly {i + 1}, got {ch.order!r}")
        _req_str(ch.expected_title, f"chapters[{i}].expected_title",
                 max_len=MAX_TITLE_LEN, allow_unknown=True)
    return tuple(rows)


def _validate_entities(rows: Sequence[CanonEntityV1]) -> tuple[CanonEntityV1, ...]:
    if len(rows) > MAX_ENTITIES:
        raise CanonBoundsError(f"entities: {len(rows)} exceeds {MAX_ENTITIES}")
    seen: set[str] = set()
    for i, e in enumerate(rows):
        if not isinstance(e, CanonEntityV1):
            raise CanonSchemaError(
                f"entities[{i}]: expected CanonEntityV1, got {type(e).__name__}")
        _req_id(e.entity_id, f"entities[{i}].entity_id")
        if e.entity_id in seen:
            raise CanonSchemaError(f"entities[{i}].entity_id: duplicate {e.entity_id!r}")
        seen.add(e.entity_id)
        _req_str(e.canonical_name, f"entities[{i}].canonical_name", max_len=MAX_NAME_LEN)
        if not isinstance(e.aliases, tuple):
            raise CanonSchemaError(f"entities[{i}].aliases: expected tuple")
        if len(e.aliases) > MAX_ALIASES_PER_ENTITY:
            raise CanonBoundsError(
                f"entities[{i}].aliases: {len(e.aliases)} exceeds {MAX_ALIASES_PER_ENTITY}")
        for j, alias in enumerate(e.aliases):
            _req_str(alias, f"entities[{i}].aliases[{j}]", max_len=MAX_NAME_LEN)
        _req_enum(e.alias_source, f"entities[{i}].alias_source", ALIAS_SOURCES)
        # §7.1 alias-authority rule, enforced structurally: aliases may not exist
        # without a declared authoritative source, and a declared source may not be
        # claimed for an empty alias set.
        if e.aliases and e.alias_source == "none":
            raise CanonSchemaError(
                f"entities[{i}]: {len(e.aliases)} alias(es) with alias_source='none' — "
                "an alias is authoritative only from an accepted input or canon source")
        if not e.aliases and e.alias_source != "none":
            raise CanonSchemaError(
                f"entities[{i}]: alias_source={e.alias_source!r} with no aliases")
    return tuple(rows)


def _validate_simple(rows, *, kind: str, max_n: int, id_attr: str,
                     order_attrs: Sequence[str], count: int,
                     row_type: type,
                     enum_attrs: Mapping[str, Sequence[str]] = (),
                     text_attrs: Mapping[str, int] = ()) -> tuple:
    if len(rows) > max_n:
        raise CanonBoundsError(f"{kind}: {len(rows)} exceeds {max_n}")
    seen: set[str] = set()
    for i, row in enumerate(rows):
        if not isinstance(row, row_type):
            raise CanonSchemaError(
                f"{kind}[{i}]: expected {row_type.__name__}, "
                f"got {type(row).__name__}")
        rid = getattr(row, id_attr)
        _req_id(rid, f"{kind}[{i}].{id_attr}")
        if rid in seen:
            raise CanonSchemaError(f"{kind}[{i}].{id_attr}: duplicate {rid!r}")
        seen.add(rid)
        for attr in order_attrs:
            _req_order(getattr(row, attr), f"{kind}[{i}].{attr}", count=count)
        for attr, allowed in dict(enum_attrs).items():
            _req_enum(getattr(row, attr), f"{kind}[{i}].{attr}", allowed)
        for attr, max_len in dict(text_attrs).items():
            _req_str(getattr(row, attr), f"{kind}[{i}].{attr}", max_len=max_len)
    return tuple(rows)


def _validate_canon_instance(canon: CanonLiteV1) -> None:
    """Validate a fully constructed artifact, including its self-binding hash."""
    _req_enum(canon.schema_version, "schema_version", (SCHEMA_VERSION,))
    _req_sha256(canon.outline_sha256, "outline_sha256")
    _req_sha256(canon.generation_config_sha256, "generation_config_sha256")
    _req_str(canon.target_language, "target_language",
             max_len=MAX_LANGUAGE_LEN, allow_unknown=True)
    _req_enum(canon.fact_source_policy, "fact_source_policy", FACT_SOURCE_POLICIES)
    if canon.advisory_bible_sha256 != UNKNOWN:
        _req_sha256(canon.advisory_bible_sha256, "advisory_bible_sha256")

    for name in ("chapters", "entities", "anchors", "one_time_events", "reveals",
                 "flashback_exceptions"):
        if not isinstance(getattr(canon, name), tuple):
            raise CanonSchemaError(f"{name}: expected tuple")

    count = len(canon.chapters)
    _validate_chapters(canon.chapters)
    _validate_entities(canon.entities)
    _validate_simple(
        canon.anchors, kind="anchors", max_n=MAX_ANCHORS, id_attr="anchor_id",
        order_attrs=(), count=count, row_type=CanonAnchorV1,
        enum_attrs={"kind": ANCHOR_KINDS}, text_attrs={"literal": MAX_LITERAL_LEN})
    _validate_simple(
        canon.one_time_events, kind="one_time_events", max_n=MAX_EVENTS,
        id_attr="event_id", order_attrs=("occurs_chapter_order",), count=count,
        row_type=CanonEventV1)
    _validate_simple(
        canon.reveals, kind="reveals", max_n=MAX_REVEALS, id_attr="reveal_id",
        order_attrs=("planned_chapter_order",), count=count, row_type=CanonRevealV1)
    _validate_simple(
        canon.flashback_exceptions, kind="flashback_exceptions",
        max_n=MAX_FLASHBACK_EXCEPTIONS, id_attr="exception_id",
        order_attrs=("chapter_order",), count=count,
        row_type=CanonFlashbackExceptionV1,
        enum_attrs={"reason_code": FLASHBACK_REASON_CODES})
    _req_sha256(canon.canon_sha256, "canon_sha256")
    if not canon.verify_sha256():
        raise CanonSchemaError("canon_sha256: does not bind this content")


def _finalize(canon_kwargs: dict[str, Any]) -> CanonLiteV1:
    """Validate every field, then bind the hash. The ONLY constructor path."""
    count = len(canon_kwargs["chapters"])
    _req_enum(canon_kwargs["schema_version"], "schema_version", (SCHEMA_VERSION,))
    _req_sha256(canon_kwargs["outline_sha256"], "outline_sha256")
    _req_sha256(canon_kwargs["generation_config_sha256"], "generation_config_sha256")
    _req_str(canon_kwargs["target_language"], "target_language",
             max_len=MAX_LANGUAGE_LEN, allow_unknown=True)
    _req_enum(canon_kwargs["fact_source_policy"], "fact_source_policy", FACT_SOURCE_POLICIES)
    if canon_kwargs["advisory_bible_sha256"] != UNKNOWN:
        _req_sha256(canon_kwargs["advisory_bible_sha256"], "advisory_bible_sha256")

    canon_kwargs["chapters"] = _validate_chapters(canon_kwargs["chapters"])
    canon_kwargs["entities"] = _validate_entities(canon_kwargs["entities"])
    canon_kwargs["anchors"] = _validate_simple(
        canon_kwargs["anchors"], kind="anchors", max_n=MAX_ANCHORS, id_attr="anchor_id",
        order_attrs=(), count=count, row_type=CanonAnchorV1,
        enum_attrs={"kind": ANCHOR_KINDS},
        text_attrs={"literal": MAX_LITERAL_LEN})
    canon_kwargs["one_time_events"] = _validate_simple(
        canon_kwargs["one_time_events"], kind="one_time_events", max_n=MAX_EVENTS,
        id_attr="event_id", order_attrs=("occurs_chapter_order",), count=count,
        row_type=CanonEventV1)
    canon_kwargs["reveals"] = _validate_simple(
        canon_kwargs["reveals"], kind="reveals", max_n=MAX_REVEALS, id_attr="reveal_id",
        order_attrs=("planned_chapter_order",), count=count, row_type=CanonRevealV1)
    canon_kwargs["flashback_exceptions"] = _validate_simple(
        canon_kwargs["flashback_exceptions"], kind="flashback_exceptions",
        max_n=MAX_FLASHBACK_EXCEPTIONS, id_attr="exception_id",
        order_attrs=("chapter_order",), count=count,
        row_type=CanonFlashbackExceptionV1,
        enum_attrs={"reason_code": FLASHBACK_REASON_CODES})

    hash_input = {
        "schema_version": canon_kwargs["schema_version"],
        "outline_sha256": canon_kwargs["outline_sha256"],
        "generation_config_sha256": canon_kwargs["generation_config_sha256"],
        "target_language": canon_kwargs["target_language"],
        "chapters": [c.to_canonical_obj() for c in canon_kwargs["chapters"]],
        "entities": [e.to_canonical_obj() for e in canon_kwargs["entities"]],
        "anchors": [a.to_canonical_obj() for a in canon_kwargs["anchors"]],
        "one_time_events": [
            e.to_canonical_obj() for e in canon_kwargs["one_time_events"]],
        "reveals": [r.to_canonical_obj() for r in canon_kwargs["reveals"]],
        "flashback_exceptions": [
            f.to_canonical_obj() for f in canon_kwargs["flashback_exceptions"]],
        "fact_source_policy": canon_kwargs["fact_source_policy"],
        "advisory_bible_sha256": canon_kwargs["advisory_bible_sha256"],
    }
    return CanonLiteV1(
        canon_sha256=_digest("canon_lite.canon.v1", hash_input), **canon_kwargs)


def build_canon_lite_v1(
    *,
    outline_chapters: Sequence[Mapping[str, Any]],
    job_config: JobConfigSnapshotV1,
    entities: Sequence[CanonEntityV1] = (),
    anchors: Sequence[CanonAnchorV1] = (),
    one_time_events: Sequence[CanonEventV1] = (),
    reveals: Sequence[CanonRevealV1] = (),
    flashback_exceptions: Sequence[CanonFlashbackExceptionV1] = (),
    fact_source_policy: str = "unknown",
    advisory_bible_text: Optional[str] = None,
) -> CanonLiteV1:
    """Build the canon deterministically. Pure: no I/O, no clock, no randomness, no LLM.

    Chapter identity, count and order come from the ACCEPTED OUTLINE (§6.1) — never
    from a model's newly invented structure, and never from `advisory_bible_text`,
    which contributes only its hash.

    Raises `CanonSchemaError` / `CanonBoundsError` on any violation. It never returns a
    partially valid canon: C7 forbids turning invalid canon into a clean-looking result,
    so there is no lenient mode and no "best effort" return.
    """
    if not isinstance(job_config, JobConfigSnapshotV1):
        raise CanonSchemaError(
            f"job_config: expected JobConfigSnapshotV1, got {type(job_config).__name__}")
    if not job_config.verify_sha256():
        raise CanonSchemaError("config_sha256: job configuration was mutated after binding")

    canon_chapters = _canonical_outline_chapters(outline_chapters)
    outline_sha = _digest(
        "canon_lite.outline.v1",
        [{"order": ch.order, "id": ch.chapter_id, "title": ch.expected_title}
         for ch in canon_chapters])
    if outline_sha != job_config.outline_sha256:
        # The snapshot is the authority on what was accepted. A drift here means the
        # outline changed after the config was bound — exactly what the snapshot exists
        # to catch.
        raise CanonSchemaError(
            "outline_sha256: outline does not match the bound job-config snapshot")
    if job_config.chapter_count != len(canon_chapters):
        raise CanonSchemaError(
            "chapter_count: outline does not match the bound job-config snapshot")

    bible_sha = UNKNOWN
    if advisory_bible_text is not None:
        if not isinstance(advisory_bible_text, str):
            raise CanonSchemaError(
                "advisory_bible_text: expected str or None, got "
                f"{type(advisory_bible_text).__name__}")
        if advisory_bible_text.strip():
            bible_sha = sha256_hex(
                _norm_text(advisory_bible_text).encode("utf-8"))

    return _finalize({
        "schema_version": SCHEMA_VERSION,
        "outline_sha256": outline_sha,
        "generation_config_sha256": job_config.config_sha256,
        "target_language": job_config.target_language,
        "chapters": canon_chapters,
        "entities": list(entities),
        "anchors": list(anchors),
        "one_time_events": list(one_time_events),
        "reveals": list(reveals),
        "flashback_exceptions": list(flashback_exceptions),
        "fact_source_policy": _req_enum(fact_source_policy, "fact_source_policy",
                                        FACT_SOURCE_POLICIES),
        "advisory_bible_sha256": bible_sha,
    })


# ===========================================================================
# Strict parsing — the untrusted-input door
# ===========================================================================

_COMPONENT_SPECS = {
    "chapters": (CanonChapterV1, ("chapter_id", "order", "expected_title")),
    "entities": (CanonEntityV1, ("entity_id", "canonical_name", "aliases", "alias_source")),
    "anchors": (CanonAnchorV1, ("anchor_id", "kind", "literal")),
    "one_time_events": (CanonEventV1, ("event_id", "occurs_chapter_order")),
    "reveals": (CanonRevealV1, ("reveal_id", "planned_chapter_order")),
    "flashback_exceptions": (CanonFlashbackExceptionV1,
                             ("exception_id", "chapter_order", "reason_code")),
}


def parse_canon_lite_v1(payload: Mapping[str, Any]) -> CanonLiteV1:
    """Parse an untrusted mapping into a validated `CanonLiteV1`.

    Fails closed on ANY unknown field at any level, on any missing field, and on a
    `canon_sha256` that does not bind the content. There is no permissive path: a canon
    that cannot be validated must never be usable, because C7 forbids converting
    invalid canon into a clean result.
    """
    _reject_unknown_fields(payload, _CANON_FIELDS, "canon")

    built: dict[str, Any] = {}
    for key, (cls, allowed) in _COMPONENT_SPECS.items():
        rows = payload[key]
        if not isinstance(rows, (list, tuple)):
            raise CanonSchemaError(f"{key}: expected a list")
        out = []
        for i, row in enumerate(rows):
            _reject_unknown_fields(row, allowed, f"{key}[{i}]")
            kwargs = dict(row)
            if cls is CanonEntityV1:
                aliases = kwargs.get("aliases")
                if not isinstance(aliases, (list, tuple)):
                    raise CanonSchemaError(f"{key}[{i}].aliases: expected a list")
                kwargs["aliases"] = tuple(aliases)
            out.append(cls(**kwargs))
        built[key] = out

    canon_kwargs = {
        "schema_version": payload["schema_version"],
        "outline_sha256": payload["outline_sha256"],
        "generation_config_sha256": payload["generation_config_sha256"],
        "target_language": payload["target_language"],
        "fact_source_policy": payload["fact_source_policy"],
        "advisory_bible_sha256": payload["advisory_bible_sha256"],
        **built,
    }
    canon = _finalize(canon_kwargs)
    declared = payload["canon_sha256"]
    if not isinstance(declared, str) or declared != canon.canon_sha256:
        raise CanonSchemaError("canon_sha256: does not bind this content")
    return canon


# ===========================================================================
# Deterministic renderer
# ===========================================================================

def render_canon(canon: CanonLiteV1) -> str:
    """Render the canon as deterministic, byte-stable text.

    PROMPT MATERIAL ONLY. Built and hashed in L1; injection into chapter prompts is
    L3 (§13). It carries canonical names and declared literals, so it must never reach
    a log, a metric, a fixture, or an API payload (C12) — use `telemetry_digest()`.

    Two canons with the same `canon_sha256` render byte-identically, and a change to
    any bound field changes the rendering. That is what makes it safe to hash the
    rendering as the injected-prefix identity in L3.
    """
    if not isinstance(canon, CanonLiteV1):
        raise CanonSchemaError("render_canon: expected a CanonLiteV1")
    _validate_canon_instance(canon)
    lines: list[str] = [
        f"CANON {canon.schema_version} {canon.canon_sha256}",
        f"language={canonical_bytes(canon.target_language).decode('utf-8')}",
        f"fact_source_policy={canonical_bytes(canon.fact_source_policy).decode('utf-8')}",
        f"chapters={canon.chapter_count}",
        "",
        "[CHAPTERS]",
    ]
    for c in canon.chapters:
        lines.append(canonical_bytes(c.to_canonical_obj()).decode("utf-8"))
    lines += ["", "[ENTITIES]"]
    for e in canon.entities:
        lines.append(canonical_bytes(e.to_canonical_obj()).decode("utf-8"))
    lines += ["", "[ANCHORS]"]
    for a in canon.anchors:
        lines.append(canonical_bytes(a.to_canonical_obj()).decode("utf-8"))
    lines += ["", "[ONE_TIME_EVENTS]"]
    for ev in canon.one_time_events:
        lines.append(canonical_bytes(ev.to_canonical_obj()).decode("utf-8"))
    lines += ["", "[REVEALS]"]
    for r in canon.reveals:
        lines.append(canonical_bytes(r.to_canonical_obj()).decode("utf-8"))
    lines += ["", "[FLASHBACK_EXCEPTIONS]"]
    for f in canon.flashback_exceptions:
        lines.append(canonical_bytes(f.to_canonical_obj()).decode("utf-8"))
    return "\n".join(lines) + "\n"


def telemetry_digest(canon: Optional[CanonLiteV1], *, canon_status: str) -> dict[str, Any]:
    """The ONLY observable projection of a canon: hashes, counts, bounded labels.

    Never contains a canonical name, an alias, a title, a literal, a prompt, a provider
    error, or any manuscript byte (C12, §10).
    """
    status = _req_enum(canon_status, "canon_status",
                       ("present", "absent", "invalid", "skipped"))
    if canon is None:
        if status == "present":
            raise CanonSchemaError("canon_status: present requires a valid canon")
        return {"canon_status": status, "schema_version": SCHEMA_VERSION,
                "canon_sha256": UNKNOWN, "chapter_count": 0, "entity_count": 0,
                "anchor_count": 0, "one_time_event_count": 0, "reveal_count": 0,
                "flashback_exception_count": 0, "fact_source_policy": UNKNOWN,
                "advisory_bible_present": False}
    if status != "present":
        raise CanonSchemaError(
            f"canon_status: {status} requires canon=None, not a CanonLiteV1")
    _validate_canon_instance(canon)
    return {
        "canon_status": status,
        "schema_version": canon.schema_version,
        "canon_sha256": canon.canon_sha256,
        "chapter_count": canon.chapter_count,
        "entity_count": len(canon.entities),
        "anchor_count": len(canon.anchors),
        "one_time_event_count": len(canon.one_time_events),
        "reveal_count": len(canon.reveals),
        "flashback_exception_count": len(canon.flashback_exceptions),
        "fact_source_policy": canon.fact_source_policy,
        "advisory_bible_present": canon.advisory_bible_sha256 != UNKNOWN,
    }


# ===========================================================================
# Mode resolution
# ===========================================================================

def resolve_mode(env: Optional[Mapping[str, str]] = None) -> str:
    """Resolve `NARASI_CANON_LITE_MODE`. Anything unrecognized is `off`.

    Fails SAFE, not open: a typo in the variable must leave production on exact legacy
    behavior (C11), never on a half-enabled path.
    """
    src = os.environ if env is None else env
    raw = str(src.get(MODE_ENV_VAR, "") or "").strip().lower()
    return raw if raw in _MODES else MODE_OFF


# ===========================================================================
# Shared-context freeze (C2 / I08)
# ===========================================================================

#: Attributes of orchestrator.context_builder.SharedContext that are load-bearing for
#: every chapter worker. If SharedContext grows a field, `unknown_attrs` reports it
#: rather than letting it drift outside the freeze unnoticed.
_FROZEN_CONTEXT_ATTRS = (
    "topic", "chapters", "style_guide", "canonical_facts", "facts_are_bible",
    "passages", "context_text", "sources", "style", "rag_used",
)


def _freezable_projection(ctx: Any) -> Any:
    """A JSON-safe deep projection of the context's load-bearing state.

    Deep by construction: nested dicts and lists are walked, so a mutation of
    `ctx.chapters[2]["title"]` changes the projection even though `ctx.chapters` is
    still the same list object.
    """
    def _walk(value: Any, depth: int = 0) -> Any:
        if depth > 12:
            return "__depth_limit__"
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, Mapping):
            pairs = sorted(
                ((str(k), v) for k, v in value.items()), key=lambda pair: pair[0])
            if len({key for key, _ in pairs}) != len(pairs):
                # Avoid collapsing distinct keys such as 1 and "1" into one digest row.
                return [["__key__", key, _walk(v, depth + 1)] for key, v in pairs]
            return {key: _walk(v, depth + 1) for key, v in pairs}
        if isinstance(value, (list, tuple)):
            return [_walk(v, depth + 1) for v in value]
        return f"__repr__:{type(value).__name__}"

    return {attr: _walk(getattr(ctx, attr, None)) for attr in _FROZEN_CONTEXT_ATTRS}


class _HardFrozenMixin:
    """Blocks attribute rebinding. Installed by swapping `__class__` (hard mode only)."""

    def __setattr__(self, name: str, value: Any) -> None:  # noqa: D105
        raise CanonFrozenError(
            f"shared context is frozen before chapter MAP (C2/I08); "
            f"refused to set {name!r}")

    def __delattr__(self, name: str) -> None:  # noqa: D105
        raise CanonFrozenError(
            f"shared context is frozen before chapter MAP (C2/I08); "
            f"refused to delete {name!r}")


class SharedContextFreeze:
    """Freeze the shared context immediately before chapter fan-out (C2, I08).

    Two layers, because they fail differently:

      * PREVENT (`hard=True`) — rebinding an attribute raises `CanonFrozenError`.
        Implemented by swapping the instance's `__class__` to a subclass, so it costs
        nothing per read and cannot be bypassed by ordinary assignment.
      * DETECT (always) — a deep digest is taken at freeze time and re-checked at
        `verify()`. This catches IN-PLACE mutation of nested containers
        (`ctx.chapters[0]["title"] = ...`), which no `__setattr__` guard can see.

    In `shadow`, hard mode is OFF: shadow must not change user-visible behavior, so a
    mutation is REPORTED, not raised. `assist`/`enforce` (L3) turn hard mode on. The
    hard path is exercised by the L1 suite so it is proven load-bearing rather than
    shipped as dead code.
    """

    __slots__ = ("_ctx", "_hard", "_baseline", "_original_class", "_released")

    def __init__(self, ctx: Any, *, hard: bool = False) -> None:
        self._ctx = ctx
        self._hard = bool(hard)
        self._released = False
        self._original_class = type(ctx)
        self._baseline = self.digest()
        if self._hard:
            frozen_cls = type(
                f"Frozen{self._original_class.__name__}",
                (_HardFrozenMixin, self._original_class), {},
            )
            object.__setattr__(ctx, "__class__", frozen_cls)

    # -- observation ------------------------------------------------------
    def digest(self) -> str:
        return _digest("canon_lite.context.v1", _freezable_projection(self._ctx))

    @property
    def baseline_digest(self) -> str:
        return self._baseline

    def unknown_attrs(self) -> tuple[str, ...]:
        """Public attributes of the context that the freeze does not cover."""
        try:
            names = {f.name for f in _dc_fields(self._original_class)}
        except TypeError:
            names = {n for n in vars(self._ctx) if not n.startswith("_")}
        return tuple(sorted(names - set(_FROZEN_CONTEXT_ATTRS)))

    def verify(self) -> tuple[bool, tuple[str, ...]]:
        """`(unchanged, violation_codes)`. Codes are bounded labels, never values."""
        codes: list[str] = []
        if self.digest() != self._baseline:
            codes.append("shared_context_mutated_after_freeze")
        extra = self.unknown_attrs()
        if extra:
            codes.append("shared_context_attr_outside_freeze")
        return (not codes, tuple(codes))

    # -- lifecycle --------------------------------------------------------
    def release(self) -> None:
        """Restore the original class. Idempotent; safe to call from a `finally`."""
        if self._released:
            return
        self._released = True
        if self._hard:
            object.__setattr__(self._ctx, "__class__", self._original_class)

    def __enter__(self) -> "SharedContextFreeze":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.release()
        return False
