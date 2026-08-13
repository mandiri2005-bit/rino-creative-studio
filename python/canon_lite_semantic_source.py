"""Canon Lite SEMANTIC SOURCE (P0-B) — the validated, tamper-evident carrier between the
Story Bible call and `build_canon_lite_v1()`.

🔴 WHY THIS MODULE EXISTS, AND WHY IT IS NOT `canon_registry`. Production already has a
   mechanism that asks an LLM for machine-checkable facts alongside the prose Story Bible —
   `NARASI_CANON_REGISTRY` (see `orchestrator/dynamic.py::build_story_bible`). It is
   deliberately NOT reused or promoted here, for reasons the owner ratified explicitly:

     · it is emitted as a fenced ```json block APPENDED AFTER the prose in the SAME text
       response, never parsed back out anywhere — it rides along as text a human reads,
       not as a value code can trust;
     · its own parser is best-effort and non-fatal by design (a triage/dedup aid, not an
       authority — see its own "RULES: pin ONLY facts a chapter's plot turns on... TRIAGE,
       not dump" framing);
     · its schema (events/timeline/kinship/exhibit_sets/chains/quantities) has no top-level
       entity table with canonical_name/aliases at all — entities exist only as bare
       `entity_id` strings referenced from `participants`/`kinship`, which is not enough to
       build a `CanonEntityV1` row from;
     · it has its own independent LLM fallback-extraction path elsewhere in the codebase,
       which is precisely the "post-hoc extraction from prose" this workstream was told not
       to build.

   None of that makes it useless — it is a real, working "ask an LLM for a JSON fence after
   prose, in one response" mechanism, and its PROMPT SHAPE is legitimate design reference.
   But it is not authority-safe for L2, and this module does not import from it, parse its
   fence, or depend on its flag. `CanonLiteSemanticSourceV1` is deliberately its own,
   narrower schema: exactly the three tuples `build_canon_lite_v1()` needs
   (`entities`/`anchors`/`one_time_events`), nothing else, parsed strictly, all-or-nothing.

🔴 WHAT "TAMPER-EVIDENT CARRIER" MEANS HERE. This object is built ONCE, right after the
   Story Bible call returns, from the SAME outline the LLM was actually shown. It binds
   itself to that outline's FULL CONTENT — not `canon_lite.outline_digest()`'s identity-only
   projection (order/id/title), which cannot see a summary/description edit — via
   `canon_lite.accepted_outline_content_digest()`. The caller (`orchestrator/static.py`,
   inside `narrate_chapters`) is required to re-check `binds_outline()` against the outline
   it is ABOUT to use for the real canon, immediately before calling `build_canon_lite_v1()`.
   A mismatch means the accepted outline moved between the two points, and the extraction on
   file describes a book that is no longer the one being written — assist must refuse rather
   than inject stale semantics.

🔴 `import canon_lite`, NEVER `from canon_lite import <class>`, ANYWHERE IN THIS FILE — a
   deliberate, load-bearing choice, not a style preference. `canon_lite.py` has its own test
   (`test_canon_lite_module_has_no_import_time_side_effects`) that calls
   `importlib.reload(canon_lite)` to prove the module is safe to import lazily — and a
   reload REBINDS every class in `canon_lite`'s namespace to a NEW object while leaving the
   MODULE object's identity untouched. `from canon_lite import CanonAnchorV1` captures the
   class reference ONCE, at THIS module's own import time; if that happens before a reload
   elsewhere in the same process and this module is not re-imported afterward (it won't
   be — it is already cached in `sys.modules`), every `isinstance` check against that stale
   reference silently compares against the WRONG class — same name, same printed repr,
   different identity, and `isinstance` returns False. `canon_lite.CanonAnchorV1`, resolved
   fresh at the point of use instead of once at import time, is immune to this by
   construction: attribute access on the module object always reads its CURRENT namespace.

No SDK, no client, no I/O, no network. This module validates and hashes; it does not call
anything.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import canon_lite

SEMANTIC_SOURCE_SCHEMA_VERSION = "canon_lite_semantic_source_v2"

#: The only `ALIAS_SOURCES` member this module ever produces. Aliases surfaced here came
#: from the Story Bible extraction, which is inherently part of BUILDING canon — not a
#: value the job/request payload supplied ("job_input"). "canon_source" is the closest,
#: correct-by-elimination fit for an alias this module itself established; it is never
#: "job_input" (nothing here is user-payload-derived) and never invented as a new member —
#: `canon_lite.ALIAS_SOURCES` is a closed, hash-tested enum this module does not touch.
ALIAS_SOURCE_SEMANTIC_EXTRACTION = "canon_source"

#: The fenced-block label this module's own prompt addendum asks for. Distinct from
#: `canon_registry` on purpose — see the module docstring.
SEMANTIC_SOURCE_FENCE_LABEL = "canon_lite_semantic_source"

_ENVELOPE_FIELDS = ("entities", "anchors", "one_time_events")
_ENTITY_FIELDS = ("canonical_name", "aliases")
_ANCHOR_FIELDS = ("kind", "literal")
_EVENT_FIELDS = ("description", "occurs_chapter")


# 🔴 NO `SemanticSourceError(canon_lite.CanonSchemaError)` SUBCLASS HERE, DELIBERATELY.
#    A `class X(Y): ...` statement binds its base class ONCE, at DEFINITION time — the one
#    place in this file the "resolve `canon_lite.*` fresh at the point of use" rule (see the
#    module docstring) cannot be applied, because Python offers no lazy base-class syntax.
#    A subclass minted before a `canon_lite` reload would carry the STALE `CanonSchemaError`
#    in its MRO forever; a caller's `except canon_lite.CanonSchemaError` built from a FRESH
#    reference would then not match it, and the exception would escape uncaught. Every raise
#    site below constructs `canon_lite.CanonSchemaError`/`CanonBoundsError` directly instead
#    — a plain attribute access, resolved fresh every time, with no class of its own to go
#    stale.


@dataclass(frozen=True, slots=True)
class CanonLiteSemanticSourceV1:
    """The validated, self-binding output of ONE Story Bible call's structured envelope.

    Deliberately built from `canon_lite`'s OWN typed rows (`CanonEntityV1`/`CanonAnchorV1`/
    `CanonEventV1`) rather than a parallel shape — this object exists to be unwrapped
    directly into `build_canon_lite_v1(entities=..., anchors=..., one_time_events=...)`,
    and a second, slightly-different type hierarchy for "the same three things, briefly"
    would be exactly the kind of drift-prone duplication this whole workstream keeps
    finding and closing.
    """
    schema_version: str
    accepted_outline_content_sha256: str
    #: Hash of the EXACT Story Bible response these tuples were extracted from, via
    #: `canon_lite.advisory_bible_digest` — the SAME projection the canon's own
    #: `advisory_bible_sha256` uses, so the two are comparable by construction.
    bible_sha256: str
    entities: tuple  # tuple[canon_lite.CanonEntityV1, ...] — see module docstring on reload
    anchors: tuple   # tuple[canon_lite.CanonAnchorV1, ...]
    one_time_events: tuple  # tuple[canon_lite.CanonEventV1, ...]
    source_sha256: str

    def __post_init__(self) -> None:
        _validate_semantic_source_instance(self)

    def to_canonical_obj(self, *, include_hash: bool = True) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "schema_version": self.schema_version,
            "accepted_outline_content_sha256": self.accepted_outline_content_sha256,
            "bible_sha256": self.bible_sha256,
            "entities": [e.to_canonical_obj() for e in self.entities],
            "anchors": [a.to_canonical_obj() for a in self.anchors],
            "one_time_events": [e.to_canonical_obj() for e in self.one_time_events],
        }
        if include_hash:
            obj["source_sha256"] = self.source_sha256
        return obj

    def compute_sha256(self) -> str:
        return canon_lite._digest("canon_lite.semantic_source.v2",
                                  self.to_canonical_obj(include_hash=False))

    def verify_sha256(self) -> bool:
        """True when `source_sha256` still binds this exact content — the envelope has
        not been tampered with (or corrupted) since it was built."""
        return self.source_sha256 == self.compute_sha256()

    def has_any_semantic_content(self) -> bool:
        """True iff at least one of the three tuples is non-empty.

        🔴 THIS IS THE `has_semantic_authority()` PRECONDITION, CHECKED EARLY. An envelope
           that parses and binds perfectly but extracted NOTHING would still make
           `canon_lite_l2.has_semantic_authority()` return False downstream — arming assist
           on it reproduces the exact failure this workstream exists to close: a canary that
           looks like it is checking something while verifying nothing. Assist must refuse
           on empty content, not merely on parse failure.
        """
        return bool(self.entities or self.anchors or self.one_time_events)

    def binds_outline(self, outline_chapters: Sequence[Mapping[str, Any]]) -> bool:
        """True iff this source's content hash still matches `outline_chapters` AS GIVEN
        RIGHT NOW — the mutation-after-binding check the caller MUST run immediately before
        `build_canon_lite_v1()`, using the exact outline about to be used for the real canon.
        """
        try:
            return self.accepted_outline_content_sha256 == \
                canon_lite.accepted_outline_content_digest(outline_chapters)
        except (canon_lite.CanonSchemaError, canon_lite.CanonBoundsError):
            # An outline that no longer even parses as a valid accepted outline plainly
            # does not bind — fail closed, do not propagate a parse error out of a
            # boolean binding check the caller expects to be total.
            return False

    def binds_bible(self, bible_text: Optional[str]) -> bool:
        """True iff `bible_text` is the EXACT Story Bible these tuples were extracted from.

        🔴 THE OUTLINE BINDING IS NOT ENOUGH ON ITS OWN. Between the Story Bible call and
           `build_canon_lite_v1()`, `narrate_chapters` can REPLACE `ctx.canonical_facts`
           without touching the outline at all — the lane-ledger enforcement path does it
           two ways: a SURGICAL line-patch (`_bible2 = _bible2.replace(old, new, 1)`) and a
           full RE-ROLL (a second `build_story_bible` call with `extra_negative`). Either
           one leaves `binds_outline()` perfectly satisfied while the canon would receive
           semantic tuples extracted from the OLD bible alongside `advisory_bible_text` from
           the NEW one — two halves of different documents, both passing validation, with
           nothing anywhere recording that they disagree.

           A reroll must therefore carry the bible AND the source from the same response;
           a surgical mutation, which produces no new source, must make assist refuse.
        """
        try:
            return self.bible_sha256 == canon_lite.advisory_bible_digest(bible_text)
        except canon_lite.CanonSchemaError:
            return False


def _validate_semantic_source_instance(src: CanonLiteSemanticSourceV1) -> None:
    if src.schema_version != SEMANTIC_SOURCE_SCHEMA_VERSION:
        raise canon_lite.CanonSchemaError(
            f"schema_version: expected {SEMANTIC_SOURCE_SCHEMA_VERSION!r}, "
            f"got {src.schema_version!r}")
    canon_lite._req_sha256(src.accepted_outline_content_sha256,
                           "accepted_outline_content_sha256")
    # A real sha256 — never `canon_lite.UNKNOWN`. `build_canon_lite_v1` tolerates UNKNOWN
    # for `advisory_bible_sha256` (a job may legitimately have no bible at all), but a
    # SEMANTIC SOURCE cannot: these tuples were extracted FROM a bible, so an envelope
    # claiming no bible is an envelope whose own provenance is unstated.
    canon_lite._req_sha256(src.bible_sha256, "bible_sha256")
    if not isinstance(src.entities, tuple):
        raise canon_lite.CanonSchemaError("entities: expected tuple")
    if not isinstance(src.anchors, tuple):
        raise canon_lite.CanonSchemaError("anchors: expected tuple")
    if not isinstance(src.one_time_events, tuple):
        raise canon_lite.CanonSchemaError("one_time_events: expected tuple")
    canon_lite._validate_entities(src.entities)
    canon_lite._validate_simple(
        src.anchors, kind="anchors", max_n=canon_lite.MAX_ANCHORS, id_attr="anchor_id",
        order_attrs=(), count=0, row_type=canon_lite.CanonAnchorV1,
        enum_attrs={"kind": canon_lite.ANCHOR_KINDS},
        text_attrs={"literal": canon_lite.MAX_LITERAL_LEN})
    # `count=0` for both calls below: `_validate_simple`'s `order_attrs` bounds-check an
    # attribute against a CHAPTER count when the attribute names a chapter order — anchors
    # carry no chapter order at all (`order_attrs=()`), and one_time_events' own
    # `occurs_chapter_order` is intentionally left UNBOUNDED here (0 disables the range
    # check) because this object is validated BEFORE it is known which final chapter count
    # it will be merged into; `build_canon_lite_v1()` re-validates the merged whole against
    # the REAL chapter count via its own `_finalize()` call, which is the binding check that
    # actually matters. Under-counting here would either reject a valid extraction for a
    # count it doesn't own yet, or (worse) over-trust a wrong count silently — no count is
    # honest about what this stage does and does not know.
    # `_validate_events`, matching `build_semantic_source_v1`. This INSTANCE validator was
    # left on `_validate_simple` when the builder moved, so a caller constructing
    # `CanonLiteSemanticSourceV1(...)` directly — which `__post_init__` routes here — could
    # still mint a source carrying duplicate labels, an unbounded label, or a non-str one.
    # That is the same shape as the bypass that made the label rules move onto the canon in
    # the first place: migrated at the builder, missed at the instance validator.
    canon_lite._validate_events(src.one_time_events, count=0, order_attrs=())
    # 🔴 "VALIDATED, SELF-BINDING" WAS A CLAIM THIS FUNCTION NEVER CHECKED. It tested the
    #    TYPE and the LENGTH of the digest and nothing else — not even that the characters
    #    were hex. Measured: `"z"*64`, `"A"*64` and `"0"*64` were all ACCEPTED by the public
    #    constructor, each producing an envelope whose own `verify_sha256()` returned False.
    #    `CanonLiteV1` has always ended `_validate_canon_instance` with exactly this pair of
    #    checks; the envelope — the object that carries semantic AUTHORITY into assist —
    #    had neither, so it was strictly weaker than the artifact it feeds.
    #
    #    Not currently exploitable end-to-end: `orchestrator/static.py` re-checks
    #    `verify_sha256()` on the arming path before trusting a source. That makes this a
    #    false contract rather than a live injection — and a caller is entitled to rely on
    #    the constructor's own promise instead of a re-check it cannot see.
    canon_lite._req_sha256(src.source_sha256, "source_sha256")
    if not src.verify_sha256():
        raise canon_lite.CanonSchemaError(
            "source_sha256: does not bind this content")


def build_semantic_source_v1(
    *,
    outline_chapters: Sequence[Mapping[str, Any]],
    bible_text: Optional[str],
    entities: Sequence = (),
    anchors: Sequence = (),
    one_time_events: Sequence = (),
) -> CanonLiteSemanticSourceV1:
    """Build the envelope deterministically. Pure: no I/O, no clock, no randomness, no LLM.

    Mirrors `canon_lite._finalize()`'s own shape on purpose, including the order of
    operations: validate the raw tuples via the SAME free functions `_finalize` uses,
    THEN hash the now-validated content, THEN construct the real frozen object — whose
    own `__post_init__` re-validates everything again. That second pass is not wasted
    work; it is `canon_lite.py`'s own established defense-in-depth (see
    `CanonLiteV1.__post_init__`: "construction must be just as strict as parsing" —
    a caller must not be able to build a plausible-looking invalid instance by skipping
    this function). Raises `CanonSchemaError`/`CanonBoundsError` on any violation; never
    returns a partially valid envelope (C7).

    `entities`/`anchors`/`one_time_events` should be `canon_lite.CanonEntityV1`/
    `CanonAnchorV1`/`CanonEventV1` instances built from the SAME `canon_lite` reference the
    caller currently holds — passing instances from a stale, pre-reload reference fails the
    `isinstance` checks below exactly as intended (see the module docstring); that is this
    function refusing to trust an object it cannot verify the type of, not a bug.
    """
    outline_content_sha = canon_lite.accepted_outline_content_digest(outline_chapters)
    bible_sha = canon_lite.advisory_bible_digest(bible_text)
    if bible_sha == canon_lite.UNKNOWN:
        raise canon_lite.CanonSchemaError(
            "bible_text: a semantic source must name the bible it was extracted from; "
            "empty/absent bible text cannot carry semantic authority")
    validated_entities = canon_lite._validate_entities(tuple(entities))
    validated_anchors = canon_lite._validate_simple(
        tuple(anchors), kind="anchors", max_n=canon_lite.MAX_ANCHORS, id_attr="anchor_id",
        order_attrs=(), count=0, row_type=canon_lite.CanonAnchorV1,
        enum_attrs={"kind": canon_lite.ANCHOR_KINDS},
        text_attrs={"literal": canon_lite.MAX_LITERAL_LEN})
    # `_validate_events`, not `_validate_simple`: the label rules (legible, and unique by
    # fold) belong to the EVENT ROW itself, so every door that builds one inherits them
    # from a single definition in `canon_lite`. This builder is exported and its callers
    # hand over ready-made `CanonEventV1`s that nothing upstream has necessarily checked,
    # so it is a real door — but so is `_finalize`, which is why the rule lives there
    # rather than being re-implemented at each entrance.
    # `order_attrs=()` / `count=0` preserved from the previous `_validate_simple` call —
    # see `_validate_semantic_source_instance` for why this stage deliberately does NOT
    # bounds-check `occurs_chapter_order`. Only the LABEL rules are inherited here.
    validated_events = canon_lite._validate_events(
        tuple(one_time_events), count=0, order_attrs=())
    hash_input = {
        "schema_version": SEMANTIC_SOURCE_SCHEMA_VERSION,
        "accepted_outline_content_sha256": outline_content_sha,
        "bible_sha256": bible_sha,
        "entities": [e.to_canonical_obj() for e in validated_entities],
        "anchors": [a.to_canonical_obj() for a in validated_anchors],
        "one_time_events": [e.to_canonical_obj() for e in validated_events],
    }
    source_sha = canon_lite._digest("canon_lite.semantic_source.v2", hash_input)
    return CanonLiteSemanticSourceV1(
        schema_version=SEMANTIC_SOURCE_SCHEMA_VERSION,
        accepted_outline_content_sha256=outline_content_sha,
        bible_sha256=bible_sha,
        entities=validated_entities, anchors=validated_anchors,
        one_time_events=validated_events, source_sha256=source_sha)


def _positional_id(prefix: str, index: int) -> str:
    """`ent1`, `anc1`, ... — never trusted from the LLM.

    🔴 POSITIONAL, NOT LLM-INVENTED. Asking the model for its own `entity_id`/`anchor_id`
       risks colliding, malformed, or unstable-across-retries values reaching a validator
       whose whole job is to reject exactly that. "Stable" for this object means stable for
       the lifetime of the ONE canon it is merged into — a positional id is deterministic
       and collision-free by construction, which is all §6.1 identity needs here.

    ⚠️ NOT USED FOR EVENTS — see `_event_id`. Entities and anchors carry their own semantic
       content on the row (`canonical_name`, `literal`), so a positional id loses nothing.
       `CanonEventV1` carries NO content field at all, so a positional id there would erase
       the event's identity entirely.
    """
    return f"{prefix}{index + 1}"


#: Longest slug fragment inside an event id. The whole id must still satisfy
#: `canon_lite._ID_RE` (<= `MAX_ID_LEN`); `evt_` + slug + `_` + 12 hex = 4 + N + 13.
_EVENT_SLUG_MAX = 40


def _event_slug(description: str) -> str:
    """The ASCII-legible half of an event id — `""` when nothing slugifies.

    ⚠️ COSMETIC ONLY. This is `[a-z0-9]`-shaped, so it erases every non-Latin script, and
       it truncates at `_EVENT_SLUG_MAX`. Both properties are fine for making an id
       skimmable in a log and were catastrophic when the id was the only identity an event
       had. Identity now lives in `CanonEventV1.label`, and the collision check compares
       `canon_lite.event_label_fold(label)` — never this. Do not reintroduce it as a
       comparison key: mutant `M11` exists specifically to fail if anyone does.
    """
    normalized = canon_lite._norm_text(description)
    slug = re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")[:_EVENT_SLUG_MAX]
    return slug.strip("_")


def _event_id(description: str, occurs_chapter: int) -> str:
    """A CONTENT-DERIVED, human-legible, collision-free event id: `evt_<slug>_<hash12>`.

    🔴 THE DEFECT THIS EXISTS FOR. `CanonEventV1` stores only `event_id` and
       `occurs_chapter_order` — no description field. With a positional id (`evt1`), two
       genuinely different events in the same chapter produced BYTE-IDENTICAL rows:
       `{"event_id":"evt1","occurs_chapter_order":1}` for both "kehilangan kunci" and
       "ledakan gedung", hence an identical `source_sha256`, an identical `canon_sha256`,
       and an identical rendered canon. The extraction was carrying a real distinction that
       died at the schema boundary.

       That is not merely lossy — it is the false-canary shape again. QC receives ONLY the
       final canon; `canon_lite_l2._accepted_canon_ids_by_claim_type` hands the extractor
       `CLAIM_ONE_TIME_EVENT: {event_id, ...}` and `render_canon` shows it those same rows.
       Given `evt1`, an extractor cannot know WHICH event to look for, so an event-only
       canon would satisfy `has_semantic_authority()` while being unable to verify anything.

    🔴 BOTH HALVES ARE LOad-BEARING, for different reasons:
       · the SLUG makes the id MEANINGFUL — `evt_ledakan_gedung_9c1f...` tells a reader and
         an extractor what the event actually is, which is the whole point of preserving
         semantic identity into the canon;
       · the HASH SUFFIX makes it UNIQUE — slugs alone collide (punctuation-only
         differences, or truncation at 40 chars), and a duplicate id would be rejected
         outright by `canon_lite._validate_simple` as a malformed canon.

       ⚠️ UNIQUE IS NOT INTERPRETABLE, and an earlier version of this docstring claimed the
          hash suffix made a colliding slug "unambiguous". It does not. It makes the ROW
          distinct; it tells a reader nothing. Two descriptions differing only past the
          40-char cap produce `evt_<same 40 chars>_1143fdbb9002` and
          `evt_<same 40 chars>_8e7101fa5c2d` — valid, distinct, and indistinguishable to
          the one consumer that matters. That gap is closed in the envelope parser, not
          here (this function sees one row and cannot detect a collision), by
          `canon_lite._validate_events`.

    The hash covers `(normalized description, occurs_chapter)`, so the same description in
    two different chapters yields two different ids. Note this does NOT make such a pair
    acceptable — `canon_lite._validate_events` refuses it on the LABEL, before any
    duplicate-id check could apply, because two events a bible describes identically are
    not tellable apart by anything QC can read.

    No prose survives into the canon: a slug is a normalized, truncated, `[a-z0-9_]`-only
    token, not the description text. `CanonEventV1` still stores no description field, and
    §10's "prose never becomes a compared value" is unchanged.

    🔴 THE SLUG IS A CONVENIENCE, NOT THE IDENTITY — `CanonEventV1.label` IS.
       An earlier round made this function REFUSE when a description slugified to nothing,
       reasoning that a hash-only `evt_<hash12>` left QC nothing to match a passage
       against. The premise was right and the remedy was wrong: `[^a-z0-9]` strips entire
       writing systems, so refusing made L3-assist structurally impossible for Korean,
       Japanese, Chinese, Russian, Arabic, Thai and Hindi — every non-Latin market — and
       flattened Vietnamese diacritics into collisions. It turned a silent degradation
       into a hard, paid-for job refusal.

       The identity now lives in `CanonEventV1.label`, which carries the description
       whole, in its own script, and travels in the very same projection QC reads. So a
       hash-only id is once again harmless HERE: it is a stable key to cite, and the row
       it sits on says in plain language which event it is. Legibility in the id remains
       best-effort; identity is no longer its job.
    """
    normalized = canon_lite._norm_text(description)
    digest = canon_lite.sha256_hex(
        canon_lite.canonical_bytes([normalized, int(occurs_chapter)]))[:12]
    slug = _event_slug(description)
    return f"evt_{slug}_{digest}" if slug else f"evt_{digest}"


#: Bounded reasons a semantic-source envelope can be refused, for the ONE log line the
#: caller emits (`orchestrator/dynamic.py`). Closed, and deliberately never the label, the
#: description, model output, or a raw exception — the same discipline
#: `canon_lite_extractor`'s `EXTRACT_REASON_CODES` follows for the extraction path.
#:
#: 🔴 THIS EXISTS BECAUSE THIS DIFF ADDED A NEW WAY FOR A PAID JOB TO DIE. A label
#:    collision refuses the envelope AFTER the Story Bible has been bought (up to
#:    `NARASI_BIBLE_BEST_OF` candidates plus a judge call), and without a code it logged as
#:    `semantic_source_parse_error class=CanonSchemaError` — indistinguishable from "the
#:    model emitted garbage", the one diagnosis that sends an operator looking in entirely
#:    the wrong place.
SEMANTIC_SOURCE_REASON_EVENT_LABEL_ILLEGIBLE = canon_lite.EVENT_LABEL_ILLEGIBLE
SEMANTIC_SOURCE_REASON_EVENT_LABEL_AMBIGUOUS = canon_lite.EVENT_LABEL_AMBIGUOUS
SEMANTIC_SOURCE_REASON_SCHEMA_INVALID = "semantic_source_schema_invalid"
SEMANTIC_SOURCE_REASON_OTHER = "other"
SEMANTIC_SOURCE_REASON_CODES = (
    SEMANTIC_SOURCE_REASON_EVENT_LABEL_ILLEGIBLE,
    SEMANTIC_SOURCE_REASON_EVENT_LABEL_AMBIGUOUS,
    SEMANTIC_SOURCE_REASON_SCHEMA_INVALID,
    SEMANTIC_SOURCE_REASON_OTHER,
)


def semantic_source_reason_code(exc: BaseException) -> str:
    """Classify an envelope refusal into a CLOSED code. Never reads the exception message.

    A specific `.reason_code` — attached by `canon_lite._event_label_error` at the two
    sites that can name a precise cause — wins. Any other schema/bounds refusal is
    SCHEMA_INVALID. Anything else is OTHER. Every branch returns a
    `SEMANTIC_SOURCE_REASON_CODES` member, so the vocabulary stays closed no matter what
    future code raises.
    """
    code = getattr(exc, "reason_code", None)
    if code in SEMANTIC_SOURCE_REASON_CODES:
        return code
    if isinstance(exc, (canon_lite.CanonSchemaError, canon_lite.CanonBoundsError)):
        return SEMANTIC_SOURCE_REASON_SCHEMA_INVALID
    return SEMANTIC_SOURCE_REASON_OTHER

def parse_semantic_source_envelope(
    raw: Any,
    *,
    outline_chapters: Sequence[Mapping[str, Any]],
    bible_text: Optional[str],
) -> CanonLiteSemanticSourceV1:
    """Strict parser for the UNTRUSTED envelope an LLM emitted. All-or-nothing (C7): any
    unknown field, wrong type, out-of-enum value, or over-length string fails the WHOLE
    parse — there is no per-row leniency and no partial envelope, for the same reason
    `canon_lite.parse_canon_lite_v1()` has none: a partially-trusted extraction that still
    *looks* like a clean result is worse than an honest, visible refusal.

    Raises `canon_lite.CanonSchemaError` or `canon_lite.CanonBoundsError` on any violation
    (resolved fresh from the caller's own `canon_lite` reference, not a subclass minted by
    this module — see the module docstring on why). Never returns a partial result.
    """
    if not isinstance(raw, Mapping):
        raise canon_lite.CanonSchemaError(
            f"semantic_source envelope: expected a mapping, got {type(raw).__name__}")
    canon_lite._reject_unknown_fields(raw, _ENVELOPE_FIELDS, "semantic_source")

    entities = []
    raw_entities = raw["entities"]
    if not isinstance(raw_entities, (list, tuple)):
        raise canon_lite.CanonSchemaError("semantic_source.entities: expected a list")
    if len(raw_entities) > canon_lite.MAX_ENTITIES:
        raise canon_lite.CanonBoundsError(
            f"semantic_source.entities: {len(raw_entities)} exceeds "
            f"{canon_lite.MAX_ENTITIES}")
    for i, row in enumerate(raw_entities):
        if not isinstance(row, Mapping):
            raise canon_lite.CanonSchemaError(f"semantic_source.entities[{i}]: expected a mapping")
        canon_lite._reject_unknown_fields(row, _ENTITY_FIELDS, f"semantic_source.entities[{i}]")
        name = canon_lite._req_str(row["canonical_name"],
                                   f"semantic_source.entities[{i}].canonical_name",
                                   max_len=canon_lite.MAX_NAME_LEN)
        raw_aliases = row["aliases"]
        if not isinstance(raw_aliases, (list, tuple)):
            raise canon_lite.CanonSchemaError(
                f"semantic_source.entities[{i}].aliases: expected a list")
        if len(raw_aliases) > canon_lite.MAX_ALIASES_PER_ENTITY:
            raise canon_lite.CanonBoundsError(
                f"semantic_source.entities[{i}].aliases: {len(raw_aliases)} exceeds "
                f"{canon_lite.MAX_ALIASES_PER_ENTITY}")
        aliases = tuple(
            canon_lite._req_str(a, f"semantic_source.entities[{i}].aliases[{j}]",
                                max_len=canon_lite.MAX_NAME_LEN)
            for j, a in enumerate(raw_aliases))
        entities.append(canon_lite.CanonEntityV1(
            entity_id=_positional_id("ent", i), canonical_name=name, aliases=aliases,
            alias_source=(ALIAS_SOURCE_SEMANTIC_EXTRACTION if aliases else "none")))

    anchors = []
    raw_anchors = raw["anchors"]
    if not isinstance(raw_anchors, (list, tuple)):
        raise canon_lite.CanonSchemaError("semantic_source.anchors: expected a list")
    if len(raw_anchors) > canon_lite.MAX_ANCHORS:
        raise canon_lite.CanonBoundsError(
            f"semantic_source.anchors: {len(raw_anchors)} exceeds {canon_lite.MAX_ANCHORS}")
    for i, row in enumerate(raw_anchors):
        if not isinstance(row, Mapping):
            raise canon_lite.CanonSchemaError(f"semantic_source.anchors[{i}]: expected a mapping")
        canon_lite._reject_unknown_fields(row, _ANCHOR_FIELDS, f"semantic_source.anchors[{i}]")
        kind = canon_lite._req_enum(row["kind"], f"semantic_source.anchors[{i}].kind",
                                    canon_lite.ANCHOR_KINDS)
        literal = canon_lite._req_str(row["literal"], f"semantic_source.anchors[{i}].literal",
                                      max_len=canon_lite.MAX_LITERAL_LEN)
        anchors.append(canon_lite.CanonAnchorV1(
            anchor_id=_positional_id("anc", i), kind=kind, literal=literal))

    events = []
    raw_events = raw["one_time_events"]
    if not isinstance(raw_events, (list, tuple)):
        raise canon_lite.CanonSchemaError("semantic_source.one_time_events: expected a list")
    if len(raw_events) > canon_lite.MAX_EVENTS:
        raise canon_lite.CanonBoundsError(
            f"semantic_source.one_time_events: {len(raw_events)} exceeds "
            f"{canon_lite.MAX_EVENTS}")
    n_chapters = len(list(outline_chapters or []))
    for i, row in enumerate(raw_events):
        if not isinstance(row, Mapping):
            raise canon_lite.CanonSchemaError(
                f"semantic_source.one_time_events[{i}]: expected a mapping")
        canon_lite._reject_unknown_fields(row, _EVENT_FIELDS,
                                          f"semantic_source.one_time_events[{i}]")
        # 🔴 READ ONCE, THEN USE ONLY THE LOCAL. `row` is an UNTRUSTED mapping, and this
        #    value now feeds three consumers — the bound check, the id's hash, and the
        #    stored label. Re-indexing `row` for each would let a Mapping with a varying
        #    `__getitem__` return different strings to different consumers, so the label
        #    the collision check compares need not be the description the id was derived
        #    from. Production hands us a plain `dict` from `json.loads`, but this module
        #    already hardens against exactly this class elsewhere, and a validator that
        #    validates one value while storing another is not a validator.
        description = canon_lite._req_str(
            row["description"], f"semantic_source.one_time_events[{i}].description",
            max_len=canon_lite.MAX_LITERAL_LEN)
        # The label IS the description, normalized — never truncated (see
        # `MAX_EVENT_LABEL_LEN`). Normalizing here rather than at comparison time means
        # the value stored, the value hashed and the value compared are one value.
        label = canon_lite._norm_text(description)
        occurs = row["occurs_chapter"]
        if isinstance(occurs, bool) or not isinstance(occurs, int):
            raise canon_lite.CanonSchemaError(
                f"semantic_source.one_time_events[{i}].occurs_chapter: expected int, "
                f"got {type(occurs).__name__}")
        if not (1 <= occurs <= max(1, n_chapters)):
            raise canon_lite.CanonBoundsError(
                f"semantic_source.one_time_events[{i}].occurs_chapter: {occurs} outside "
                f"1..{n_chapters}")
        events.append(canon_lite.CanonEventV1(
            event_id=_event_id(description, occurs),
            occurs_chapter_order=occurs, label=label))
    # No label check here: `build_semantic_source_v1` below runs `_validate_events`, and a
    # second copy of the rule at this door is exactly the drift-prone duplication that let
    # the two builders enforce DIFFERENT rules for a round.

    return build_semantic_source_v1(
        outline_chapters=outline_chapters, bible_text=bible_text, entities=entities,
        anchors=anchors, one_time_events=events)


_FENCE_RE = re.compile(
    r"```(?:json)?\s*" + re.escape(SEMANTIC_SOURCE_FENCE_LABEL) + r"\s*\n(.*?)```",
    re.DOTALL)


#: Top-level keys the structured Story Bible response must carry. Extra keys are ALLOWED
#: and deliberately so — `canon_registry`, when its flag is on, rides as a SIDECAR in this
#: same object rather than as a second independent fence (which is what made the two
#: requests compete and got the semantic one dropped in live job `oehhe741`).
STRUCTURED_BIBLE_TEXT_KEY = "bible_text"
STRUCTURED_BIBLE_SEMANTIC_KEY = "semantic_source"
#: `canon_registry`'s sidecar key. Advisory, and owned by a DIFFERENT subsystem
#: (`NARASI_CANON_REGISTRY` → `narration_api`'s canon-diff) — this module only carries it
#: across the decode so the caller can thread it onward; it never validates its contents
#: and never lets it near a `CanonLiteSemanticSourceV1`.
STRUCTURED_BIBLE_REGISTRY_KEY = "canon_registry"


def _obj(props: dict, required: Sequence[str]) -> dict:
    """A CLOSED object node. `additionalProperties: false` is the whole point: an open
    node lets the model satisfy the schema while inventing the keys we then fail to find."""
    return {"type": "object", "properties": props,
            "required": list(required), "additionalProperties": False}


#: The shape the Story Bible response MUST take, as a real JSON Schema.
#:
#: 🔴 `response_mime_type="application/json"` CONSTRAINS THE SYNTAX AND NOTHING ELSE. It
#:    obliges the model to emit valid JSON — it does not oblige it to emit OUR JSON. A live
#:    canary (2026-08-13) came back with well-formed JSON twice and failed the contract
#:    both times, because "JSON mode" was the only instruction that ever reached Vertex:
#:    `_create` coerced the whole `response_format` to `bool(...)`, so the SHAPE was thrown
#:    away one line before the provider call. A schema is what closes that gap, and it has
#:    to travel as a schema the entire way.
#:
#: Built from the SAME constants the parsers read (`_ENVELOPE_FIELDS`, `_ENTITY_FIELDS`,
#: `_ANCHOR_FIELDS`, `_EVENT_FIELDS`, `canon_lite.ANCHOR_KINDS`) so the thing we ASK for and
#: the thing we ACCEPT cannot drift apart — a hand-copied schema would be a second
#: definition of the contract, which is the failure mode this workstream keeps closing.
STRUCTURED_BIBLE_JSON_SCHEMA = _obj(
    {
        STRUCTURED_BIBLE_TEXT_KEY: {
            "type": "string",
            "description": "The prose Story Bible fact-sheet. Never JSON, never fenced.",
        },
        STRUCTURED_BIBLE_SEMANTIC_KEY: _obj(
            {
                "entities": {"type": "array", "items": _obj(
                    {"canonical_name": {"type": "string"},
                     "aliases": {"type": "array", "items": {"type": "string"}}},
                    _ENTITY_FIELDS)},
                "anchors": {"type": "array", "items": _obj(
                    {"kind": {"type": "string",
                              "enum": list(canon_lite.ANCHOR_KINDS)},
                     "literal": {"type": "string"}},
                    _ANCHOR_FIELDS)},
                "one_time_events": {"type": "array", "items": _obj(
                    {"description": {"type": "string"},
                     "occurs_chapter": {"type": "integer"}},
                    _EVENT_FIELDS)},
            },
            _ENVELOPE_FIELDS),
        # 🔴 OPTIONAL, BUT IT MUST BE LISTED — `additionalProperties: false` FORBIDS ANY KEY
        #    THAT IS NOT. Production runs `NARASI_CANON_REGISTRY=1`, whose prompt tells the
        #    model to include `canon_registry` as "a SIDECAR KEY of the same JSON object"
        #    (`orchestrator/dynamic.py`), and this module's own `STRUCTURED_BIBLE_REGISTRY_KEY`
        #    comment says extra keys are allowed *deliberately*. A closed schema that omitted
        #    it would have the provider forbidding the exact key the prompt asks for — the two
        #    halves of one request contradicting each other, with the model forced to drop the
        #    sidecar and `narration_api` then paying for a fallback re-extraction it should
        #    never need.
        #
        #    OPEN on purpose (`type: object`, no property list): the registry's shape belongs
        #    to a DIFFERENT subsystem (`NARASI_CANON_REGISTRY` → narration_api's canon-diff),
        #    and this module carries it across the decode without ever validating its
        #    contents. Modelling its keys here would be a second definition of somebody
        #    else's contract, free to drift — the failure this schema exists to prevent.
        STRUCTURED_BIBLE_REGISTRY_KEY: {
            "type": "object",
            "description": ("Advisory canon_registry sidecar, when the caller's prompt "
                            "asked for one. Never validated here."),
        },
    },
    # REQUIRED stays exactly two: the registry is advisory, and a bible that omits it is
    # still a valid bible — `parse_structured_bible_response` degrades it to None.
    (STRUCTURED_BIBLE_TEXT_KEY, STRUCTURED_BIBLE_SEMANTIC_KEY),
)

#: The OpenAI-shaped request carrying that schema. `json_schema`, NOT `json_object`: the
#: latter is the syntax-only mode that already failed in production.
STRUCTURED_BIBLE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "canon_lite_structured_bible", "strict": True,
                    "schema": STRUCTURED_BIBLE_JSON_SCHEMA},
}


def response_format_json_schema(response_format: Any) -> Optional[dict]:
    """Pull the JSON Schema out of an OpenAI-style `response_format`, or `None`.

    The ONE place that translation lives. `{"type": "json_object"}` legitimately carries no
    schema and returns `None`; anything malformed returns `None` rather than raising, because
    this runs inside a provider call that must not acquire a new way to fail.
    """
    if not isinstance(response_format, Mapping):
        return None
    block = response_format.get("json_schema")
    if not isinstance(block, Mapping):
        return None
    schema = block.get("schema")
    return dict(schema) if isinstance(schema, Mapping) else None


#: Bounded reasons ONE Story Bible response can fail the structured contract. Closed, and
#: never the response body — these travel to the single log line `build_story_bible` emits.
#:
#: 🔴 FIVE DIFFERENT FAULTS USED TO LOG AS ONE CODE, AND THEY WANT OPPOSITE RESPONSES.
#:    `structured_bible_contract_unmet` was emitted whether the response was not JSON at
#:    all, was a JSON array, or was a perfectly good JSON object using different keys. The
#:    first says JSON MODE NEVER REACHED THE PROVIDER — a transport/routing fault, fixed by
#:    a different rung or flag. The last says the transport worked and the PROMPT is wrong.
#:    A live canary (2026-08-13) burned two best-of Story Bible candidates and left exactly
#:    one bit of information behind: "unmet". That is the same coarse-code shape this
#:    workstream already removed twice (`EXTRACT_REASON_CODES`, `SEMANTIC_SOURCE_REASON_CODES`).
STRUCTURED_BIBLE_REASON_NOT_TEXT = "bible_response_not_text"
STRUCTURED_BIBLE_REASON_NOT_JSON = "bible_response_not_json"
STRUCTURED_BIBLE_REASON_JSON_NOT_OBJECT = "bible_response_json_not_object"
STRUCTURED_BIBLE_REASON_TEXT_MISSING = "bible_text_missing"
STRUCTURED_BIBLE_REASON_SEMANTIC_MISSING = "semantic_source_missing"
STRUCTURED_BIBLE_REASON_CODES = (
    STRUCTURED_BIBLE_REASON_NOT_TEXT,
    STRUCTURED_BIBLE_REASON_NOT_JSON,
    STRUCTURED_BIBLE_REASON_JSON_NOT_OBJECT,
    STRUCTURED_BIBLE_REASON_TEXT_MISSING,
    STRUCTURED_BIBLE_REASON_SEMANTIC_MISSING,
)


def parse_structured_bible_response(raw: Any) -> tuple:
    """Decode ONE Story Bible response under the P0-B STRUCTURED contract.

    Returns `(decoded, reason)`. On success `decoded` is
    `(bible_text, semantic_source_raw, canon_registry_raw)` and `reason` is `""`; on
    failure `decoded` is `None` and `reason` is a `STRUCTURED_BIBLE_REASON_CODES` member.
    The pair exists because the caller must be able to say WHICH way the contract broke —
    see the vocabulary above.

    `(bible_text, semantic_source_raw, canon_registry_raw)` is the prose fact-sheet,
    the still-raw envelope mapping, and the advisory registry sidecar (`None` when absent
    or not a mapping) — or `None` if the response does not satisfy the contract. Never
    raises. The returned `semantic_source_raw` is NOT validated here beyond "is a
    mapping": `parse_semantic_source_envelope()` remains the single strict gate for its
    contents, and duplicating any part of that check here would be a second implementation
    of a decision that already has exactly one.

    🔴 A BAD REGISTRY MUST NOT COST YOU THE BIBLE. `canon_registry` is advisory and
       report-only in its own subsystem, so a missing or malformed one degrades to `None`
       and the contract still holds. Failing the whole response over it would make a
       report-only feature able to kill a paid job — a strictly worse trade than the
       fenced-block behaviour it replaces, where a malformed registry was simply not found.

    🔴 WHY THIS REPLACED THE FENCE. The fence contract asked one free-text response to
       carry prose PLUS one or two labelled ```json blocks, and nothing in the transport
       obliged the model to produce them — `Worker` could not even express
       `response_format`. Live job `oehhe741` came back twice (both best-of candidates)
       with a complete bible and no envelope, and assist refused a job that had already
       burned ~99s of paid model work. Under `response_mime_type="application/json"` the
       WHOLE response is one JSON document, so prose-plus-fence is not merely discouraged,
       it is impossible — which is what makes this shape the contract rather than a
       preference.

    🔴 STRICT ON PURPOSE — NO FENCE-STRIPPING FALLBACK. A response wrapped in ```json
       markers is a response that did NOT come back in JSON mode, which means the
       transport silently dropped the request (the `anthropic` proto does exactly that —
       `_anthropic_messages_create` takes no `response_json`). Quietly unwrapping it would
       hide a broken transport behind a parser that "still works", which is the same class
       of silence this whole workstream exists to remove. A non-conforming response is a
       failed attempt; the caller fails over.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, STRUCTURED_BIBLE_REASON_NOT_TEXT
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        # THE TRANSPORT SIGNAL. A response that is not JSON at all did not come back in
        # JSON mode, which points at the request, not the model's willingness.
        return None, STRUCTURED_BIBLE_REASON_NOT_JSON
    if not isinstance(obj, dict):
        return None, STRUCTURED_BIBLE_REASON_JSON_NOT_OBJECT
    bible_text = obj.get(STRUCTURED_BIBLE_TEXT_KEY)
    semantic_raw = obj.get(STRUCTURED_BIBLE_SEMANTIC_KEY)
    # `type(x) is str`, not isinstance: a str SUBCLASS can carry a poisoned __eq__/__hash__
    # and this value goes on to be hashed and compared for provenance. Same discipline as
    # `_l3_canonicalize_verdict` in the P0-A preflight.
    if type(bible_text) is not str or not bible_text.strip():
        # JSON mode WAS applied — the model simply used a different shape. The opposite
        # diagnosis to NOT_JSON, and previously indistinguishable from it.
        return None, STRUCTURED_BIBLE_REASON_TEXT_MISSING
    if not isinstance(semantic_raw, dict):
        return None, STRUCTURED_BIBLE_REASON_SEMANTIC_MISSING
    registry_raw = obj.get(STRUCTURED_BIBLE_REGISTRY_KEY)
    if not isinstance(registry_raw, dict):
        registry_raw = None
    return (bible_text.strip(), semantic_raw, registry_raw), ""


def extract_semantic_source_json(text: str) -> Optional[Any]:
    """Pull the ` ```json canon_lite_semantic_source ... ``` ` fence out of raw LLM text
    and JSON-decode it. Returns `None` — never raises — when the fence is absent or its
    content is not valid JSON; a missing/malformed fence is reported as "nothing found",
    not as an error, because the caller's own policy (refuse for assist, tolerate for
    shadow) is what decides whether that absence matters, not this function.

    The label is matched literally (`canon_registry`'s own fence, or any other label, is
    NOT a match) — this is the one thing keeping this extractor from ever picking up a
    different JSON block that happens to share the generic ` ```json ` fence syntax.

    ⚠️ NO LONGER THE PRODUCTION CONTRACT. `build_story_bible(structured_semantic=True)`
       now uses `parse_structured_bible_response()` against a JSON-mode response; see its
       docstring for why the fence was abandoned. This function is retained as a tested
       utility, but wiring it back into the bible path would re-create the `oehhe741`
       defect — a contract the transport never enforces.
    """
    if not isinstance(text, str) or not text:
        return None
    m = _FENCE_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return None


__all__ = [
    "SEMANTIC_SOURCE_SCHEMA_VERSION",
    "SEMANTIC_SOURCE_FENCE_LABEL",
    "ALIAS_SOURCE_SEMANTIC_EXTRACTION",
    "CanonLiteSemanticSourceV1",
    "build_semantic_source_v1",
    "parse_semantic_source_envelope",
    "parse_structured_bible_response",
    "STRUCTURED_BIBLE_REASON_CODES",
    "STRUCTURED_BIBLE_JSON_SCHEMA",
    "STRUCTURED_BIBLE_RESPONSE_FORMAT",
    "response_format_json_schema",
    "STRUCTURED_BIBLE_TEXT_KEY",
    "STRUCTURED_BIBLE_SEMANTIC_KEY",
    "STRUCTURED_BIBLE_REGISTRY_KEY",
    "extract_semantic_source_json",
]
