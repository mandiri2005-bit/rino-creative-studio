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
import contextvars
import re
import unicodedata
from dataclasses import dataclass, fields as _dc_fields
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "SCHEMA_VERSION",
    "L2_EXTRACTOR_VERSION",
    "L2_PREDICATE_SET_VERSION",
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
    "assist_tenants",
    "resolve_effective_mode",
    "ASSIST_TENANTS_ENV_VAR",
    "PARITY_SCHEMA_VERSION",
    "NOT_APPLICABLE_L1",
    "PARITY_MATCH",
    "PARITY_MISMATCH",
    "PARITY_NOT_APPLICABLE",
    "PARITY_SNAPSHOT_FIELDS",
    "ROUTES",
    "build_parity_snapshot",
    "check_parity",
    "mark_job_canon_ineligible",
    "job_is_canon_ineligible",
    "reset_job_canon_eligibility",
    "canonical_bytes",
    "sha256_hex",
    "build_job_config_snapshot",
    "build_canon_lite_v1",
    "outline_digest",
    "accepted_outline_content_digest",
    "advisory_bible_digest",
    "parse_canon_lite_v1",
    "render_canon",
    "telemetry_digest",
    "context_digest",
]

# ===========================================================================
# Constants — versions, sentinels, bounds, closed enums
# ===========================================================================

#: 🔴 RATIFIED 2026-08-13: v1 -> v2, because `CanonEventV1` GAINED A FIELD. This follows the
#: same rule that keeps `CLAIMS_SCHEMA_VERSION` at v2 while the QC wire moved (see
#: `canon_lite_l2`): bump when the ARTIFACT'S OWN ROWS change, not when something around it
#: does. `one_time_events` rows now carry `label`, so an artifact stamped `canon_lite_v1`
#: and one stamped this describe genuinely different shapes, and `to_canonical_obj()` —
#: which is what QC receives and what `canon_sha256` binds — is not the same projection it
#: was. Deliberately NOT bumped alongside: `JOB_CONFIG_SCHEMA_VERSION`,
#: `PARITY_SCHEMA_VERSION`, `L2_*`, and the `outline`/`job_config`/`parity` digest domains —
#: none of those objects' fields moved, and bumping them would force re-acceptance of
#: shapes that never changed.
SCHEMA_VERSION = "canon_lite_v2"
JOB_CONFIG_SCHEMA_VERSION = "job_config_snapshot_v1"
L2_EXTRACTOR_VERSION = "cl_l2_extractor_v1"
L2_PREDICATE_SET_VERSION = "cl_l2_predicates_v1"

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
#: Comma-separated tenant allowlist. Gates `assist` ONLY. Empty admits nobody.
ASSIST_TENANTS_ENV_VAR = "NARASI_CANON_LITE_ASSIST_TENANTS"

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

#: An event's legible identity (`CanonEventV1.label`).
#:
#: 🔴 BOUND TO `MAX_LITERAL_LEN` ON PURPOSE, NOT COINCIDENTALLY. The semantic-source
#:    envelope validates an event `description` at `MAX_LITERAL_LEN`, so tying the label's
#:    bound to the same constant means an accepted description ALWAYS fits its label whole.
#:    That is what removes truncation — and with it the entire class of "two labels that
#:    are identical only because both were cut at the same character". If these two ever
#:    drift apart, that collision class comes back silently, which is exactly how the
#:    40-char `_EVENT_SLUG_MAX` defect happened. One constant, so they cannot drift.
MAX_EVENT_LABEL_LEN = MAX_LITERAL_LEN

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


#: Unicode `Default_Ignorable_Code_Point`, from DerivedCoreProperties. These render as
#: NOTHING, so they can only ever smuggle a difference past a human reader or a model.
#:
#: 🔴 CATEGORY IS THE WRONG TEST, AND GUESSING IT COST TWO REVIEW ROUNDS. An earlier fold
#:    dropped `Cf`/`Cc` and reasoned that covered invisible characters. It does not:
#:    U+FE0F VARIATION SELECTOR-16 is `Mn`, U+034F COMBINING GRAPHEME JOINER is `Mn`, and
#:    the Hangul fillers U+115F/U+1160/U+3164 are `Lo` — a LETTER, so they satisfied a
#:    "contains a letter" legibility test while displaying nothing at all. Only the
#:    property itself describes the set, so the property is what is written down here.
#:    `unicodedata` does not expose it, hence the explicit ranges.
_DEFAULT_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def _is_default_ignorable(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _DEFAULT_IGNORABLE_RANGES)


def event_label_fold(label: str) -> str:
    """The COMPARISON key for an event label — never stored, never rendered, never sent.

    🔴 NFC IS THE RIGHT STORAGE FORM AND THE WRONG COMPARISON KEY. `_norm_text` is what the
       canon stores; comparing those bytes directly let visually IDENTICAL labels through
       (U+00A0 for a space, an added zero-width character, a variation selector), shipping
       two canon rows a model cannot tell apart — the defect `CanonEventV1.label` exists to
       close, one encoding layer down. Verified against the real builders, not reasoned
       about.

    The fold answers "would a reader see the same text?", so it is deliberately aggressive:
    NFKC folds compatibility variants (ligature `ﬁ`/`fi`, fullwidth `Ｒ`/`R`); every
    default-ignorable code point is dropped; every whitespace run collapses to one space
    (Python's `split()` treats NBSP as whitespace); `casefold()` last, because two labels
    differing only in case read as one name.

    Aggressive is the correct direction: a false MATCH refuses an envelope and asks the
    bible for a clearer description, while a false MISS ships two events QC must guess
    between — the failure this whole subsystem exists to prevent.

    🔴 NORMALIZE LAST, NOT ONLY FIRST — THE FOLD MUST BE IDEMPOTENT. Normalizing once up
       front is not enough, because the later steps can DE-normalize: removing an ignorable
       re-exposes a composable sequence (`a` + CGJ + combining grave becomes `a` +
       combining grave, which never recomposes to `à`), and `casefold()` does it too
       (`ẞ` → `ss`). That left `fold(fold(x)) != fold(x)` — and a projection that is not
       idempotent is not an equivalence class. The observable cost: inserting ONE invisible
       character BETWEEN a base letter and its combining mark defeated the collision check
       entirely, across Latin diacritics, Japanese dakuten and Korean jamo. Re-normalizing
       after every transformation is what closes it, and `fold(fold(x)) == fold(x)` is
       asserted in the suite so it stays closed.

    🔴 WHITESPACE IS A SEPARATOR AND MUST SURVIVE AS ONE — DROPPING IT JOINS THE WORDS
       EITHER SIDE. This loop looks redundant next to `split()`, and is not. TAB, NEWLINE,
       CR, VT and FF are category `Cc`, so an earlier version — which dropped `Cf`/`Cc`
       BEFORE collapsing — DELETED them instead of collapsing them: `"Rina\tpergi"` folded
       to `"rinapergi"` while `"Rina pergi"` folded to `"rina pergi"`, and the two shipped
       as two distinct events. That is a false MISS, the direction this fold exists to
       prevent, and it reached QC as two rows a reader cannot tell apart. Whitespace is
       therefore mapped to a space FIRST and only then are the genuinely invisible
       characters dropped — `.isspace()` is the right test because it is true for the `Cc`
       whitespace, for NBSP and for U+2028/U+2029, and false for ZWSP/ZWNJ/BOM, which
       render as nothing and must keep being deleted rather than becoming a word break.
    """
    kept: list[str] = []
    for ch in unicodedata.normalize("NFKC", label):
        if ch.isspace():
            kept.append(" ")
        elif _is_default_ignorable(ch) or unicodedata.category(ch) in ("Cf", "Cc"):
            continue
        else:
            kept.append(ch)
    return unicodedata.normalize("NFKC", " ".join("".join(kept).split()).casefold())


#: Bounded reasons a one-time event row can be refused. Closed, and deliberately NOT the
#: label text: these travel to a log line (see `canon_lite_semantic_source`).
EVENT_LABEL_ILLEGIBLE = "event_label_illegible"
EVENT_LABEL_AMBIGUOUS = "event_label_ambiguous"


def _event_label_error(msg: str, code: str) -> Exception:
    exc = CanonSchemaError(msg)
    exc.reason_code = code
    return exc


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


def _req_nfc(value: Any, field: str) -> None:
    """REFUSE text that is not already NFC. The other half of `_bind_norm`, and the half
    that was missing at every validator door.

    🔴 `_req_str` NORMALIZES TO MEASURE AND RETURNS A VALUE MOST CALLERS DISCARD. That is
       not a bug in `_req_str` — the PARSER and BUILDER paths (`parse_semantic_source_
       envelope`, `build_job_config_snapshot`, the outline title) genuinely need
       normalize-and-return, because raw model/user text legitimately arrives NFD and must
       be accepted and canonicalised. The VALIDATOR paths need the opposite, and shared one
       function with them: `_validate_entities`, `_validate_simple` and `_validate_chapters`
       all call `_req_str` for its exceptions and throw the normalized string away, so the
       row keeps whatever bytes it arrived with.

       Measured consequence, on every text field at once: a row mutated after construction
       (or hand-built) and then passed to `build_canon_lite_v1` was ACCEPTED, its NFD text
       reached `to_canonical_obj()` — the projection QC reads — and `canon_sha256` was
       computed over the NFD bytes, so `verify_sha256()` returned **True**. A perfectly
       self-consistent artifact carrying text in a form the system promises it never
       stores, and two canons identical to any reader hashing differently.

    THE RULE, stated once so it cannot be half-applied again: **a field bound by
    `_bind_norm` at construction must be REFUSED by its validator in any other form.**
    Refuse, never repair — repairing inside `_validate_canon_instance` would rewrite a
    tampered artifact into passing its own `verify_sha256()`, turning the integrity check
    into a repair shop. Every `_bind_norm`'d field is covered: `expected_title`,
    `canonical_name`, every alias, `literal`, `label`, `target_language`.
    """
    if type(value) is str and value != UNKNOWN and value != _norm_text(value):
        raise CanonSchemaError(
            f"{field}: not NFC-normalized; this field is bound to NFC at construction, so "
            f"a value in any other form never went through it and would hash differently "
            f"from its identical twin")


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
    """A one-time event: an id to cite, a placement, and a LEGIBLE identity.

    🔴 `label` EXISTS BECAUSE AN ID ALONE CANNOT IDENTIFY AN EVENT. Unlike `CanonEntityV1`
       (`canonical_name`) and `CanonAnchorV1` (`literal`), this row used to carry no
       content field at all — only `event_id` and `occurs_chapter_order`. QC is handed
       exactly this projection and nothing else, so an event was identifiable only insofar
       as its id happened to be legible. Two failures followed from that, both found in
       review rather than by tests:
         · a description with no Latin letters (Korean, Japanese, Arabic, Thai …)
           slugified to nothing and produced a hash-only `evt_<hash12>`, leaving QC no
           way to match a passage to this row — i.e. continuity checking was structurally
           impossible for most of the world's scripts;
         · two descriptions agreeing on their first `_EVENT_SLUG_MAX` characters produced
           ids differing ONLY by that opaque hash — unique to the schema, indistinguishable
           to the reader and the model.
       Carrying the label directly fixes both at the source: identity now lives in a field
       meant to hold it, not in the incidental legibility of a generated id.

    §10 IS NOT WEAKENED. The privacy rule bounds what leaves for a LOG or a METRIC; this
    module's own header already classes `render_canon()` output and canonical names as
    PROMPT MATERIAL, and the label is exactly that class — the same status
    `CanonEntityV1.canonical_name` has carried all along. It is strictly weaker than those,
    in fact: no predicate ever COMPARES a label (`evaluate_semantic`'s one_time_event
    branch reads `canon_ref` only), so a label can never become a compared value or
    manufacture a violation. `telemetry_digest()` remains the only observable projection.

    ⚠️ NOT the same decision as `CanonRevealV1`, which still stores no secret. A reveal's
       content is withheld because putting a planned twist in a chapter prompt would leak
       it to the writer. An event is something that HAPPENS, already present in the
       advisory bible the writer reads, so naming it in the canon reveals nothing new.
    """

    event_id: str
    occurs_chapter_order: int   # 1..N or UNKNOWN_ORDER
    label: str                  # bounded, never compared — see the class docstring

    def __post_init__(self) -> None:
        # Every other text-bearing row binds its normalization here, and this one was the
        # exception: `_validate_simple` discards `_req_str`'s normalized return, so an
        # NFD label stayed NFD in the object AND in `canon_sha256` — two canons that
        # render identically hashing differently, through a field that is an element of
        # `CLAIM_CACHE_KEY_FIELDS`. Binding it is what makes `_bind_norm`'s own promise
        # ("equal names hash equally") true for events too.
        _bind_norm(self, "label")

    def to_canonical_obj(self) -> dict[str, Any]:
        return {"event_id": self.event_id,
                "occurs_chapter_order": self.occurs_chapter_order,
                "label": self.label}


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


def accepted_outline_content_digest(outline_chapters: Sequence[Mapping[str, Any]]) -> str:
    """Hash id + order + title + summary/description for every chapter.

    🔴 DELIBERATELY WIDER THAN `outline_digest()` — that is the point of this function
       existing as a SEPARATE one, not a parameter on the old one. `outline_digest` binds
       only chapter IDENTITY (order/id/title) because identity is what `build_canon_lite_v1`
       needs to bind the outline to the job config. A semantic-extraction call (P0-B) reads
       the FULL accepted outline — summary/description included, since that prose is what a
       Story Bible call is actually shown — so binding only identity would let a
       summary/description edit made AFTER extraction silently outlive the extraction it
       should have invalidated: the entities/anchors/events on file would still describe
       text that is no longer the accepted outline, and `outline_digest` alone would not
       notice, because it was never asked to.

    Same projection as `_canonical_outline_chapters` for order/id/title (ONE identity
    projection, reused, not a second divergent one), plus each row's own `summary` —
    falling back to `description` — normalized exactly as `_story_bible_prompt` reads it,
    so the hash binds what the LLM was actually shown, not a different serialization of it.
    """
    canonical = _canonical_outline_chapters(outline_chapters)
    source = list(outline_chapters or [])
    rows = []
    for ch, raw in zip(canonical, source):
        summary = ""
        if isinstance(raw, Mapping):
            summary = str(raw.get("summary", raw.get("description", "")) or "")
        rows.append({
            "order": ch.order, "id": ch.chapter_id, "title": ch.expected_title,
            "summary": _norm_text(summary),
        })
    return _digest("canon_lite.semantic_source.accepted_outline_content.v1", rows)


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
        return _digest("canon_lite.canon.v2", self.to_canonical_obj(include_hash=False))

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
        _req_nfc(ch.expected_title, f"chapters[{i}].expected_title")
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
        _req_nfc(e.canonical_name, f"entities[{i}].canonical_name")
        if not isinstance(e.aliases, tuple):
            raise CanonSchemaError(f"entities[{i}].aliases: expected tuple")
        if len(e.aliases) > MAX_ALIASES_PER_ENTITY:
            raise CanonBoundsError(
                f"entities[{i}].aliases: {len(e.aliases)} exceeds {MAX_ALIASES_PER_ENTITY}")
        for j, alias in enumerate(e.aliases):
            _req_str(alias, f"entities[{i}].aliases[{j}]", max_len=MAX_NAME_LEN)
            _req_nfc(alias, f"entities[{i}].aliases[{j}]")
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
            _req_nfc(getattr(row, attr), f"{kind}[{i}].{attr}")
    return tuple(rows)


def _validate_events(rows, *, count: int,
                     order_attrs: Sequence[str] = ("occurs_chapter_order",)) -> tuple:
    """`_validate_simple` for one-time events, PLUS the two rules that make an event
    identifiable at all.

    🔴 THIS LIVES HERE, ON THE CANON, AND NOT IN THE SEMANTIC-SOURCE PARSER. An earlier
       round enforced these rules only in `canon_lite_semantic_source`, which left the
       FINAL CANON — the artifact QC actually reads, built by `_finalize`, which
       `orchestrator/static.py` calls directly — accepting two events whose labels differ
       only by a non-breaking space. A rule the consumer's own validator does not enforce
       is a rule with a door standing open behind it. Every constructor path runs
       `_finalize` or `_validate_canon_instance`, so putting it here closes all of them at
       once, including any future one.

    Both rules are about the ONE projection QC receives (`CanonEventV1.to_canonical_obj`):
      · a label with no letter or digit in any script names nothing, so the row is
        identity-free — and `_req_str`'s `not text.strip()` guard cannot see it, because
        `str.strip()` removes none of U+200B/U+FE0F/U+115F and friends;
      · two labels with the same fold are the same event to any reader, leaving the model
        to choose between them by their opaque id hashes.

    `order_attrs` is a PARAMETER because the two callers legitimately differ on it, and
    collapsing that was a real (caught) regression: the canon knows its final chapter count
    and must bounds-check `occurs_chapter_order` against it, while a semantic source is
    validated BEFORE it is known which chapter count it will be merged into and passes
    `()` on purpose (see `_validate_semantic_source_instance`). The LABEL rules below are
    identical for both — those depend on nothing the caller owns.
    """
    rows = _validate_simple(
        rows, kind="one_time_events", max_n=MAX_EVENTS, id_attr="event_id",
        order_attrs=order_attrs, count=count, row_type=CanonEventV1,
        text_attrs={"label": MAX_EVENT_LABEL_LEN})
    seen: dict[str, int] = {}
    for i, row in enumerate(rows):
        label = row.label
        # `type(x) is str`, not isinstance: this value is folded and used as a dict key, so
        # a str SUBCLASS with a poisoned __eq__/__hash__ must not reach either.
        if type(label) is not str:
            raise _event_label_error(
                f"one_time_events[{i}].label: expected exactly str, "
                f"got {type(label).__name__}", EVENT_LABEL_ILLEGIBLE)
        # NFC is NOT checked here. It was, for one round — and that was the same mistake
        # this module keeps making one layer down: a rule written for `label` alone while
        # `canonical_name`, every alias and `literal` had the identical hole. It now lives
        # in `_req_nfc`, called from `_validate_simple` above (which this function runs
        # first) and from every other validator door. One rule, one definition, every
        # text field — see `_req_nfc`'s docstring for the measured consequence.
        #
        # It raises a plain `CanonSchemaError` with no `reason_code` deliberately: the
        # envelope classifier maps that to `semantic_source_schema_invalid`, so the agreed
        # four-code vocabulary stays closed and this needed no fifth code.
        fold = event_label_fold(label)
        if not any(unicodedata.category(ch)[0] in ("L", "N") for ch in fold):
            raise _event_label_error(
                f"one_time_events[{i}].label: contains no letter or digit in any script, "
                f"so it names no event the QC extractor could match a passage to",
                EVENT_LABEL_ILLEGIBLE)
        first = seen.get(fold)
        if first is not None:
            raise _event_label_error(
                f"one_time_events[{i}]: its label is indistinguishable from "
                f"one_time_events[{first}]'s, so the two events differ only by an opaque "
                f"hash the QC extractor cannot interpret", EVENT_LABEL_AMBIGUOUS)
        seen[fold] = i
    return rows


def _validate_canon_instance(canon: CanonLiteV1) -> None:
    """Validate a fully constructed artifact, including its self-binding hash."""
    _req_enum(canon.schema_version, "schema_version", (SCHEMA_VERSION,))
    _req_sha256(canon.outline_sha256, "outline_sha256")
    _req_sha256(canon.generation_config_sha256, "generation_config_sha256")
    _req_str(canon.target_language, "target_language",
             max_len=MAX_LANGUAGE_LEN, allow_unknown=True)
    _req_nfc(canon.target_language, "target_language")
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
    _validate_events(canon.one_time_events, count=count)
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
    _req_nfc(canon_kwargs["target_language"], "target_language")
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
    canon_kwargs["one_time_events"] = _validate_events(
        canon_kwargs["one_time_events"], count=count)
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
        canon_sha256=_digest("canon_lite.canon.v2", hash_input), **canon_kwargs)


def advisory_bible_digest(advisory_bible_text: Optional[str]) -> str:
    """Hash the ADVISORY prose bible, or `UNKNOWN` when there is none.

    🔴 ONE PROJECTION, TWO CALLERS. `build_canon_lite_v1` computes the canon's own
       `advisory_bible_sha256` from this, and `canon_lite_semantic_source` binds its
       envelope to the bible it was extracted from using the SAME function — so
       "the canon's bible" and "the bible the semantic tuples came from" are comparable
       by construction. Two separately-written hashes of "the bible" would be exactly the
       kind of near-duplicate projection that lets a swap slip through unnoticed: the
       binding check would compare two numbers that were never guaranteed to agree even
       when nothing had changed.

    Only the hash ever travels: the bible's TEXT is not authority (§6.1) and prose must
    never enter a value that gets compared or reported (§10).
    """
    if advisory_bible_text is None:
        return UNKNOWN
    if not isinstance(advisory_bible_text, str):
        raise CanonSchemaError(
            "advisory_bible_text: expected str or None, got "
            f"{type(advisory_bible_text).__name__}")
    if not advisory_bible_text.strip():
        return UNKNOWN
    return sha256_hex(_norm_text(advisory_bible_text).encode("utf-8"))


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

    bible_sha = advisory_bible_digest(advisory_bible_text)

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
    "one_time_events": (CanonEventV1, ("event_id", "occurs_chapter_order", "label")),
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


# ===========================================================================
# Generation-leak containment (F1, BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md)
#
# Canary v9 delivered `[anc1]`..`[anc4]` verbatim into customer prose. `render_canon()`
# above is unchanged and stays QC/L3's contract (it needs ids to address spans). A
# chapter WRITER never needs an id — only the literal/canonical_name/label — so this
# section adds a separate, id-free projection for that injection point, plus a
# scrub/detect pair as the backstop for whatever the projection misses (a provider
# that echoes structure it was never shown, a future caller that reintroduces
# `render_canon()` at an injection site, content generated before this existed).
#
# `chapter_id` (CanonChapterV1) is only PARTLY in the closed set below (2026-08-15
# re-audit REJECT finding, refined from the original all-or-nothing exclusion): a
# chapter whose id is exactly its own decimal order number ("1" for chapter 1, "2" for
# chapter 2, ...) is the common, human-facing shape `_canonical_outline_chapters` falls
# back to by default — bracket-matching THAT would false-positive on any legitimate
# numbered reference or footnote in generated prose ("[1]"), so it stays excluded, same
# as before. An id that is anything else — a custom outline-supplied slug
# ("private-chapter-alpha"), a UUID-like id, or the generated "ch{n}" fallback
# `_canonical_outline_chapters` uses when an outline id fails `_ID_RE` — is exactly as
# opaque as an entity/anchor/event/reveal/exception id and was reaching generation with
# no closed-set coverage to catch it if it ever leaked. No prefix guessing: the test is
# an exact string comparison against this chapter's own `order`, nothing else.
# ===========================================================================

def canon_bound_marker_ids(canon: CanonLiteV1) -> frozenset[str]:
    """The closed set of opaque internal ids bound into THIS canon instance.

    Every id-bearing field across the five component types that carry one, PLUS any
    chapter id that is not simply its own order number written out (see comment above)
    — never a prefix guess, never borrowed from another canon, never a bare regex. An id
    is "bound" for a run if and only if it is the literal id of a row this exact canon
    instance carries.
    """
    if not isinstance(canon, CanonLiteV1):
        raise CanonSchemaError("canon_bound_marker_ids: expected a CanonLiteV1")
    ids: set[str] = set()
    ids.update(c.chapter_id for c in canon.chapters if c.chapter_id != str(c.order))
    ids.update(e.entity_id for e in canon.entities)
    ids.update(a.anchor_id for a in canon.anchors)
    ids.update(ev.event_id for ev in canon.one_time_events)
    ids.update(r.reveal_id for r in canon.reveals)
    ids.update(f.exception_id for f in canon.flashback_exceptions)
    return frozenset(ids)


def _bound_marker_rx(bound_ids: frozenset[str]) -> Optional["re.Pattern[str]"]:
    if not bound_ids:
        return None
    alternation = "|".join(re.escape(i) for i in sorted(bound_ids, key=len, reverse=True))
    # IGNORECASE (adversarial-audit finding, 2026-08-14 night): bound ids are always
    # lowercase (_ID_RE), but a model echoing one into prose can trivially capitalize
    # it via ordinary sentence-initial auto-capitalization ("[Anc1]") -- both this
    # function's callers (scrub AND detect, the delivery gate's actual authority)
    # shared this one blind spot, so fixing it here fixes both at once.
    return re.compile(r"\[(?:" + alternation + r")\]", re.IGNORECASE)


def scrub_bound_markers(text: str, canon: Optional[CanonLiteV1]) -> tuple[str, int]:
    """Remove every `[<id>]` bracket token whose id is bound in `canon` — nothing else.

    A bracket that does not exactly match a bound id of THIS canon — a legitimate
    `[Tuesday]`, a footnote `[1]`, an id that belongs to some OTHER canon instance —
    is left untouched byte for byte. Only the bracket token itself is removed (never
    adjacent characters). A doubled space/tab created BY a removal is collapsed to
    one — but ONLY at the boundary of a marker (or a back-to-back RUN of markers)
    that was actually removed; whitespace elsewhere in the string, however doubled
    or indented, is never touched (adversarial-audit finding, 2026-08-14 night: the
    prior whole-string collapse mangled verse indentation, a deliberate double space,
    and a markdown hard-break, anywhere in the text, whenever ANY marker was removed
    anywhere else in it). Pure, deterministic, no I/O.

    This is prevention's BACKSTOP, not the primary defence — `render_canon_for_generation`
    is what should stop an id ever reaching a chapter prompt. A caller MUST still verify
    with `detect_bound_markers` after scrubbing — that is the function a delivery gate
    should trust, not this one's return count, so a scrub bug can never fool the gate.
    """
    if not text or canon is None:
        return text, 0
    rx = _bound_marker_rx(canon_bound_marker_ids(canon))
    if rx is None:
        return text, 0
    matches = list(rx.finditer(text))
    if not matches:
        return text, 0

    # Group back-to-back matches (no characters between them, e.g. "[anc1][anc2]")
    # into one run -- each marker alone only sees ONE of its two neighbouring
    # spaces, so treating them separately collapses only one side and leaves a
    # doubled space (this exact shape has its own regression test).
    runs: list[tuple[int, int]] = []
    run_start, run_end = matches[0].start(), matches[0].end()
    for m in matches[1:]:
        if m.start() == run_end:
            run_end = m.end()
        else:
            runs.append((run_start, run_end))
            run_start, run_end = m.start(), m.end()
    runs.append((run_start, run_end))

    pieces: list[str] = []
    cursor = 0
    for start, end in runs:
        pieces.append(text[cursor:start])
        cursor = end
        has_leading_ws = bool(pieces[-1]) and pieces[-1][-1] in " \t"
        has_trailing_ws = cursor < len(text) and text[cursor] in " \t"
        if has_leading_ws and has_trailing_ws:
            # space + marker(s) + space -> collapse to the one leading space
            # already emitted; drop the one trailing space, localized to THIS run.
            cursor += 1
        elif start == 0 and has_trailing_ws:
            # marker(s) at the very start of the string -> no stray leading space.
            cursor += 1
    pieces.append(text[cursor:])
    return "".join(pieces), len(matches)


def detect_bound_markers(text: str, canon: Optional[CanonLiteV1]) -> tuple[str, ...]:
    """The independent verification half — the ONLY function a delivery gate may trust.

    Returns the bound ids (deduplicated, first-seen order, always lowercase-canonical
    regardless of how the echo was actually cased) still present as a `[id]` token in
    `text`. An empty tuple is the only value that means clear. Reports bare ids, never
    surrounding prose — ids are opaque structural tokens, not canonical names or
    literals, so this respects the same privacy discipline `telemetry_digest` enforces
    (C12): counts and bounded tokens only, never prose.
    """
    if not text or canon is None:
        return ()
    rx = _bound_marker_rx(canon_bound_marker_ids(canon))
    if rx is None:
        return ()
    seen: list[str] = []
    seen_set: set[str] = set()
    for m in rx.finditer(text):
        token = m.group(0)[1:-1].lower()
        if token not in seen_set:
            seen_set.add(token)
            seen.append(token)
    return tuple(seen)


GENERATION_PROJECTION_VERSION = "canon_lite_generation_v1"


def render_canon_for_generation(canon: CanonLiteV1) -> str:
    """Render the canon for a chapter-writer prompt WITHOUT any opaque internal id.

    `render_canon()` stays PROMPT MATERIAL for QC/L3 exactly as it is — do not touch
    it, do not change `canon_sha256`, do not change what QC/L3 receive. A chapter
    writer never needs an id, only a literal/canonical_name/label; rendering the id
    anyway is what let a model echo `[anc3]` straight into prose. This projection is
    deliberately a DIFFERENT text with its own version constant
    (`GENERATION_PROJECTION_VERSION`, never conflated with `SCHEMA_VERSION`) — a
    caller that hashes this rendering gets a hash that means "generation projection
    identity", distinct from `canon.canon_sha256` (untouched content identity) and
    from a hash of `render_canon()`'s own output.

    Deterministic and byte-stable under the same rule `render_canon()` follows.
    """
    if not isinstance(canon, CanonLiteV1):
        raise CanonSchemaError("render_canon_for_generation: expected a CanonLiteV1")
    _validate_canon_instance(canon)
    lines: list[str] = [
        f"CANON {GENERATION_PROJECTION_VERSION} {canon.canon_sha256}",
        f"language={canonical_bytes(canon.target_language).decode('utf-8')}",
        f"fact_source_policy={canonical_bytes(canon.fact_source_policy).decode('utf-8')}",
        f"chapters={canon.chapter_count}",
        "",
        "[CHAPTERS]",
    ]
    for c in canon.chapters:
        # 2026-08-15 re-audit REJECT finding: this used to call `c.to_canonical_obj()`,
        # which carries `chapter_id` — the one component type this function forgot to
        # re-project id-free (every other loop below already omits its opaque id). A
        # chapter writer needs `order`/`expected_title` only, never the id.
        lines.append(canonical_bytes(
            {"order": c.order, "expected_title": c.expected_title}
        ).decode("utf-8"))
    lines += ["", "[ENTITIES]"]
    for e in canon.entities:
        lines.append(canonical_bytes(
            {"canonical_name": e.canonical_name, "aliases": list(e.aliases)}
        ).decode("utf-8"))
    lines += ["", "[ANCHORS]"]
    for a in canon.anchors:
        lines.append(canonical_bytes({"kind": a.kind, "literal": a.literal}).decode("utf-8"))
    lines += ["", "[ONE_TIME_EVENTS]"]
    for ev in canon.one_time_events:
        lines.append(canonical_bytes(
            {"occurs_chapter_order": ev.occurs_chapter_order, "label": ev.label}
        ).decode("utf-8"))
    lines += ["", "[REVEALS]"]
    for r in canon.reveals:
        lines.append(canonical_bytes(
            {"planned_chapter_order": r.planned_chapter_order}).decode("utf-8"))
    lines += ["", "[FLASHBACK_EXCEPTIONS]"]
    for f in canon.flashback_exceptions:
        lines.append(canonical_bytes(
            {"chapter_order": f.chapter_order, "reason_code": f.reason_code}
        ).decode("utf-8"))
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
    """Resolve `NARASI_CANON_LITE_MODE` — the GLOBAL configuration, no tenant.

    Fails SAFE, not open: a typo in the variable must leave production on exact legacy
    behavior (C11), never on a half-enabled path.

    🔴 THIS IS THE CONFIGURATION, NOT THE DECISION FOR A JOB. Callers that ask "is
       this deployment configured for a mode" want this one — the operator Phase A
       readiness verdict and the metered-wave host gate both do. Callers that ask
       "does THIS job run assist" want `resolve_effective_mode`, which additionally
       requires the tenant.

       These were briefly the same function, and making the single one tenant-aware
       silently turned every existing caller into "off" whenever a tenant was not
       threaded through: the metered wave then never ran for a legitimate canary,
       so no session was ever produced and assist ended `unchecked` having repaired
       nothing. Splitting them is what makes "forgot to pass the tenant" a visible
       type of mistake instead of a silent downgrade.
    """
    src = os.environ if env is None else env
    raw = str(src.get(MODE_ENV_VAR, "") or "").strip().lower()
    return raw if raw in _MODES else MODE_OFF


def assist_tenants(env: Optional[Mapping[str, str]] = None) -> frozenset:
    """The tenants allowed onto `assist`, from a comma-separated allowlist.

    Empty when unset, and an empty allowlist admits nobody — see
    `resolve_effective_mode`.
    """
    src = os.environ if env is None else env
    raw = str(src.get(ASSIST_TENANTS_ENV_VAR, "") or "")
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def resolve_effective_mode(env: Optional[Mapping[str, str]] = None, *,
                           tenant_id: Optional[str] = None) -> str:
    """The mode for ONE job: the global mode, narrowed by the assist allowlist.

    🔴 `assist` IS PER-TENANT, AND THAT IS NOT A CONVENIENCE. The variable is
       process-wide, so flipping it to `assist` would put EVERY job this worker
       picks up onto the repair path at once — including jobs already sitting in
       the queue, which the pre-deploy drain gate says nothing about (it proves
       zero ACTIVE, not zero WAITING). There is no such thing as "one cohort"
       without this gate.

       Every other tenant resolves to `off` — the legacy path, no import, no spend,
       no L3 payload. An empty or unset allowlist admits NOBODY, which makes
       flipping the mode without naming a tenant a no-op rather than a fleet-wide
       activation. `shadow` and `enforce` are untouched: shadow is observational
       and enforce is refused everywhere already.
    """
    mode = resolve_mode(env)
    if mode != MODE_ASSIST:
        return mode
    tid = str(tenant_id or "").strip()
    src = os.environ if env is None else env
    return MODE_ASSIST if tid and tid in assist_tenants(src) else MODE_OFF


# ===========================================================================
# L1.1 — §9 mode/config parity between the dispatcher and the executor
# ===========================================================================
#
# §9: "Effective mode, config digest, canon/extractor versions, predicate-set version,
# and model route are snapshotted at job start and transported to the worker. The worker
# must detect configuration mismatch; startup logging alone is not parity proof."
# §13 makes that check part of what activation IS, not an optional extra.
#
# Three design constraints shaped this, each one a way it could have failed silently:
#
#   1. **An ABSENT snapshot is the mismatch, not a neutral case.** C11 forbids flag-off
#      from changing the payload, so a dispatcher on `off` adds nothing — which is
#      exactly what an executor on `shadow` sees during activation skew. Treating a
#      missing key as "nothing to check" would wave through the one condition this
#      mechanism exists to catch, on every single activation.
#   2. **The snapshot is server-side and rides as a SIBLING of the request body.** The
#      body is user-supplied and is forwarded verbatim into the queue payload, so a
#      snapshot placed inside it would be forgeable. `check_parity` is never given the
#      body.
#   3. **JSON primitives only, closed schema.** The real transport is Redis, i.e. a JSON
#      round-trip. A value that survives an in-process stub but not `json.dumps` would
#      pass every offline test and fail in production.

PARITY_SCHEMA_VERSION = "canon_lite_parity_v1"

#: Explicit "this versioned input does not exist yet at L1" marker. Distinct from
#: UNKNOWN: not "we don't know", but "this layer is not built".
NOT_APPLICABLE_L1 = "__not_applicable_l1__"

PARITY_MATCH = "MATCH"
PARITY_MISMATCH = "MISMATCH"
PARITY_NOT_APPLICABLE = "NOT_APPLICABLE"

ROUTES = ("bullmq_worker", "api_direct", "api_fallback")

PARITY_SNAPSHOT_FIELDS = (
    "schema_version", "effective_mode", "config_digest", "canon_version",
    "extractor_version", "predicate_set_version", "route", "model_route",
    "snapshot_sha256",
)

_PARITY_HASHED_FIELDS = tuple(f for f in PARITY_SNAPSHOT_FIELDS if f != "snapshot_sha256")


def _config_digest(mode: str) -> str:
    """Digest of the contract both services must agree on.

    Deliberately wider than the mode alone: it also binds the artifact schema versions,
    so two services running DIFFERENT BUILDS are caught even when their flags agree.
    A flag-only comparison would call that pair a match.
    """
    return _digest("canon_lite.parity_config.v1", {
        "parity_schema_version": PARITY_SCHEMA_VERSION,
        "canon_schema_version": SCHEMA_VERSION,
        "job_config_schema_version": JOB_CONFIG_SCHEMA_VERSION,
        "extractor_version": L2_EXTRACTOR_VERSION,
        "predicate_set_version": L2_PREDICATE_SET_VERSION,
        # Hash-only build binding. Railway supplies this non-secret commit SHA to both
        # services; different deploys must not read as the same configuration merely
        # because their schema constants and mode happen to agree.
        "runtime_build_sha": _runtime_build_sha(),
        "effective_mode": mode,
    })


def _runtime_build_sha(env: Optional[Mapping[str, str]] = None) -> str:
    """Return a bounded build marker for the config digest, never for diagnostics."""
    src = os.environ if env is None else env
    raw = str(src.get("RAILWAY_GIT_COMMIT_SHA", "") or "").strip().lower()
    if not raw:
        return "__unset__"
    if re.fullmatch(r"[0-9a-f]{40}", raw):
        return raw
    return "__invalid__"


def build_parity_snapshot(
    *,
    effective_mode: str,
    route: str,
    model_route: str,
) -> dict[str, str]:
    """Build the job-start snapshot. Server-side only; never accepts user input.

    Returns a flat dict of JSON strings — no nested objects, no numbers, no None — so it
    survives the Redis JSON round-trip byte-for-byte.
    """
    mode = _req_enum(effective_mode, "effective_mode", _MODES)
    if mode == MODE_OFF:
        # C11: an `off` dispatcher must add nothing to the payload. Refusing here means
        # the caller cannot accidentally serialise an "off" snapshot and change the
        # flag-off payload shape.
        raise CanonSchemaError("build_parity_snapshot: refused for mode=off (C11)")
    if not re.fullmatch(r"[0-9a-f]{40}", _runtime_build_sha()):
        # P0's config-parity rule: inaccessible/invalid safe metadata is a blocker,
        # never assumed parity. Returning two equal "__unset__" digests would turn
        # shared ignorance into a false MATCH.
        raise CanonSchemaError(
            "build_parity_snapshot: runtime build SHA unavailable")
    payload = {
        "schema_version": PARITY_SCHEMA_VERSION,
        "effective_mode": mode,
        "config_digest": _config_digest(mode),
        "canon_version": SCHEMA_VERSION,
        "extractor_version": L2_EXTRACTOR_VERSION,
        "predicate_set_version": L2_PREDICATE_SET_VERSION,
        "route": _req_enum(route, "route", ROUTES),
        # This is the actual non-secret worker model alias resolved for the job, not the
        # execution route above. §9 requires both concepts not to be conflated.
        "model_route": _req_str(model_route, "model_route", max_len=128),
    }
    payload["snapshot_sha256"] = _digest(
        "canon_lite.parity_snapshot.v1", {k: payload[k] for k in _PARITY_HASHED_FIELDS})
    return payload


def check_parity(
    snapshot: Any,
    *,
    local_mode: str,
    local_route: str,
    local_model_route: str,
) -> tuple[str, tuple[str, ...]]:
    """Compare a transported snapshot against this process's own configuration.

    Returns `(verdict, codes)`. Codes are bounded labels — never a transported value,
    never a digest from the snapshot, never anything a caller could echo into a log to
    leak what was sent.

    Fails closed: anything it cannot fully validate is `MISMATCH`, never `MATCH`.
    """
    mode = _req_enum(local_mode, "local_mode", _MODES)
    route = _req_enum(local_route, "local_route", ROUTES)
    model_route = _req_str(
        local_model_route, "local_model_route", max_len=128)
    if (mode != MODE_OFF
            and not re.fullmatch(r"[0-9a-f]{40}", _runtime_build_sha())):
        return PARITY_MISMATCH, ("local_build_unavailable",)

    if snapshot is None:
        # The activation-skew case. Only genuinely fine when this process is also off.
        if mode == MODE_OFF:
            return PARITY_NOT_APPLICABLE, ()
        return PARITY_MISMATCH, ("snapshot_absent",)

    if not isinstance(snapshot, Mapping):
        return PARITY_MISMATCH, ("snapshot_malformed",)
    if sorted(map(str, snapshot)) != sorted(PARITY_SNAPSHOT_FIELDS):
        # Closed schema: an extra key is as disqualifying as a missing one.
        return PARITY_MISMATCH, ("snapshot_schema_invalid",)
    if any(not isinstance(snapshot[k], str) for k in PARITY_SNAPSHOT_FIELDS):
        return PARITY_MISMATCH, ("snapshot_type_invalid",)
    if snapshot["schema_version"] != PARITY_SCHEMA_VERSION:
        return PARITY_MISMATCH, ("parity_schema_version_differs",)

    expected_hash = _digest("canon_lite.parity_snapshot.v1",
                            {k: snapshot[k] for k in _PARITY_HASHED_FIELDS})
    if snapshot["snapshot_sha256"] != expected_hash:
        return PARITY_MISMATCH, ("snapshot_unbound",)

    codes: list[str] = []
    if snapshot["effective_mode"] != mode:
        codes.append("mode_differs")
    if snapshot["config_digest"] != _config_digest(mode):
        codes.append("config_digest_differs")
    if snapshot["canon_version"] != SCHEMA_VERSION:
        codes.append("canon_version_differs")
    if snapshot["extractor_version"] != L2_EXTRACTOR_VERSION:
        codes.append("extractor_version_differs")
    if snapshot["predicate_set_version"] != L2_PREDICATE_SET_VERSION:
        codes.append("predicate_set_version_differs")
    if snapshot["route"] != route:
        codes.append("route_differs")
    if snapshot["model_route"] != model_route:
        codes.append("model_route_differs")
    if codes:
        return PARITY_MISMATCH, tuple(codes)
    return PARITY_MATCH, ()


# -- per-job eligibility -----------------------------------------------------
# A contextvar, not a parameter, because the verdict is computed in the job runner and
# consumed deep inside `narrate_chapters`, across the router. Contextvars are inherited
# by every task the job spawns (the same mechanism `narasi_gate.set_alt_history` uses),
# so concurrent jobs in one worker process cannot read each other's verdict.
_JOB_CANON_INELIGIBLE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "canon_lite_job_ineligible", default=False)


def mark_job_canon_ineligible() -> contextvars.Token:
    """Bar Canon Lite from this job and return the token needed to restore its context."""
    return _JOB_CANON_INELIGIBLE.set(True)


def job_is_canon_ineligible() -> bool:
    return bool(_JOB_CANON_INELIGIBLE.get())


def reset_job_canon_eligibility(token: Optional[contextvars.Token] = None) -> None:
    """Restore eligibility.

    Production passes the exact token returned by `mark_job_canon_ineligible` from a
    `finally` block. Tests may omit it to establish a clean fixture context.
    """
    if token is None:
        _JOB_CANON_INELIGIBLE.set(False)
    else:
        _JOB_CANON_INELIGIBLE.reset(token)


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


def context_digest(ctx: Any) -> str:
    """The deep digest of a shared context's load-bearing state.

    Public because a chapter worker has to be able to compute it on the context it
    was ACTUALLY handed. Comparing that against the freeze baseline is the only
    check that sees a mutation which happened during the MAP and was undone before
    the post-MAP `verify()` ran.
    """
    return _digest("canon_lite.context.v1", _freezable_projection(ctx))


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


# ---------------------------------------------------------------------------
# Deep immutability for the frozen containers (hard mode)
# ---------------------------------------------------------------------------
#
# 🔴 `__setattr__` GUARDS ONLY THE ATTRIBUTE, NEVER WHAT IT POINTS AT.
#    `ctx.chapters = []` raises; `ctx.chapters[0]["title"] = "x"` and
#    `ctx.chapters.append(...)` do not touch `ctx` at all, so no attribute guard
#    can see them. Left at detection-only they change the input of every worker
#    that has not started yet, and the post-MAP digest reports it after the whole
#    book has been written and paid for. Under `assist` the nested containers are
#    therefore REPLACED with types that refuse to mutate, so the write fails at
#    the moment it is attempted and the sibling workers never see it.
#
# `list`/`dict` are subclassed rather than wrapped in a proxy so that
# `isinstance(x, list)`, json encoding, and every read path keep working
# unchanged — only the mutating half of the API is removed.

_FROZEN_CONTAINER_MSG = (
    "shared context is frozen before chapter MAP (C2/I08); "
    "refused to mutate a nested {kind} in place")


class _FrozenList(list):
    """A list that refuses every in-place mutation. Reads are untouched."""

    __slots__ = ()

    def _refuse(self, *_a: Any, **_k: Any) -> Any:
        raise CanonFrozenError(_FROZEN_CONTAINER_MSG.format(kind="list"))

    __setitem__ = _refuse
    __delitem__ = _refuse
    __iadd__ = _refuse
    __imul__ = _refuse
    append = _refuse
    extend = _refuse
    insert = _refuse
    pop = _refuse
    remove = _refuse
    clear = _refuse
    sort = _refuse
    reverse = _refuse


class _FrozenDict(dict):
    """A dict that refuses every in-place mutation. Reads are untouched."""

    __slots__ = ()

    def _refuse(self, *_a: Any, **_k: Any) -> Any:
        raise CanonFrozenError(_FROZEN_CONTAINER_MSG.format(kind="dict"))

    __setitem__ = _refuse
    __delitem__ = _refuse
    __ior__ = _refuse
    pop = _refuse
    popitem = _refuse
    clear = _refuse
    update = _refuse
    setdefault = _refuse


def _deep_frozen(value: Any, depth: int = 0) -> Any:
    """Rebuild `value` with every nested list/dict replaced by a refusing one.

    Depth-bounded exactly like `_freezable_projection`, so what is prevented and
    what is detected cover the same ground: a structure deeper than the limit is
    left alone by both rather than being silently half-guarded.
    """
    if depth > 12:
        return value
    if isinstance(value, Mapping):
        return _FrozenDict(
            (k, _deep_frozen(v, depth + 1)) for k, v in value.items())
    if isinstance(value, list):
        return _FrozenList(_deep_frozen(v, depth + 1) for v in value)
    if isinstance(value, tuple):
        return tuple(_deep_frozen(v, depth + 1) for v in value)
    return value


class SharedContextFreeze:
    """Freeze the shared context immediately before chapter fan-out (C2, I08).

    Two layers, because they fail differently:

      * PREVENT (`hard=True`) — rebinding an attribute raises `CanonFrozenError`.
        Implemented by swapping the instance's `__class__` to a subclass, so it costs
        nothing per read and cannot be bypassed by ordinary assignment. The nested
        containers are ALSO replaced with refusing ones (`_deep_frozen`), because an
        attribute guard cannot see `ctx.chapters[0]["title"] = ...` — that write
        never touches `ctx`. Without it, "frozen" would mean only that the label
        cannot be repointed while the thing it points at stays public and writable.
      * DETECT (always) — a deep digest is taken at freeze time and re-checked at
        `verify()`. Retained even under hard mode: it is the backstop for anything
        prevention cannot reach (a container past the depth limit, a type neither
        list nor dict), and it is the only layer `shadow` is allowed to use.

    In `shadow`, hard mode is OFF: shadow must not change user-visible behavior, so a
    mutation is REPORTED, not raised. `assist`/`enforce` (L3) turn hard mode on. The
    hard path is exercised by the L1 suite so it is proven load-bearing rather than
    shipped as dead code.
    """

    __slots__ = ("_ctx", "_hard", "_baseline", "_original_class", "_released",
                 "_original_containers")

    def __init__(self, ctx: Any, *, hard: bool = False) -> None:
        self._ctx = ctx
        self._hard = bool(hard)
        self._released = False
        self._original_class = type(ctx)
        self._original_containers: dict[str, Any] = {}
        if self._hard:
            # Deep-freeze the containers BEFORE the class swap and before the
            # baseline: `object.__setattr__` would bypass the guard anyway, but
            # doing it first keeps the ordering obvious. Reads are unaffected, so
            # the baseline is the same digest either way.
            for attr in _FROZEN_CONTEXT_ATTRS:
                current = getattr(ctx, attr, None)
                if isinstance(current, (list, dict, Mapping)):
                    self._original_containers[attr] = current
                    object.__setattr__(ctx, attr, _deep_frozen(current))
        self._baseline = self.digest()
        if self._hard:
            frozen_cls = type(
                f"Frozen{self._original_class.__name__}",
                (_HardFrozenMixin, self._original_class), {},
            )
            object.__setattr__(ctx, "__class__", frozen_cls)

    # -- observation ------------------------------------------------------
    def digest(self) -> str:
        return context_digest(self._ctx)

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
        """Restore the original class and containers. Idempotent; safe in a `finally`.

        The caller must get its own mutable objects back: a retry, or any code that
        runs after the fan-out, is entitled to a context that behaves normally.
        """
        if self._released:
            return
        self._released = True
        if self._hard:
            object.__setattr__(self._ctx, "__class__", self._original_class)
            for attr, original in self._original_containers.items():
                object.__setattr__(self._ctx, attr, original)
            self._original_containers = {}

    def __enter__(self) -> "SharedContextFreeze":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.release()
        return False
