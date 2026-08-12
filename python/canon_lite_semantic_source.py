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
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import canon_lite

SEMANTIC_SOURCE_SCHEMA_VERSION = "canon_lite_semantic_source_v1"

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
        return canon_lite._digest("canon_lite.semantic_source.v1",
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
    canon_lite._validate_simple(
        src.one_time_events, kind="one_time_events", max_n=canon_lite.MAX_EVENTS,
        id_attr="event_id", order_attrs=(), count=0, row_type=canon_lite.CanonEventV1)
    if not isinstance(src.source_sha256, str) or len(src.source_sha256) != 64:
        raise canon_lite.CanonSchemaError("source_sha256: expected a 64-char hex sha256")


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
    validated_events = canon_lite._validate_simple(
        tuple(one_time_events), kind="one_time_events", max_n=canon_lite.MAX_EVENTS,
        id_attr="event_id", order_attrs=(), count=0, row_type=canon_lite.CanonEventV1)
    hash_input = {
        "schema_version": SEMANTIC_SOURCE_SCHEMA_VERSION,
        "accepted_outline_content_sha256": outline_content_sha,
        "bible_sha256": bible_sha,
        "entities": [e.to_canonical_obj() for e in validated_entities],
        "anchors": [a.to_canonical_obj() for a in validated_anchors],
        "one_time_events": [e.to_canonical_obj() for e in validated_events],
    }
    source_sha = canon_lite._digest("canon_lite.semantic_source.v1", hash_input)
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
       · the HASH SUFFIX makes it UNAMBIGUOUS — slugs alone collide (punctuation-only
         differences, truncation at 40 chars, or a description that is entirely non-ASCII
         and slugifies to nothing at all), and a collision would resurrect the exact bug
         this function closes.

    The hash covers `(normalized description, occurs_chapter)`, so the SAME description in
    two DIFFERENT chapters yields two different ids (legitimately different occurrences),
    while the same description twice in the SAME chapter yields one id — which
    `canon_lite._validate_simple`'s duplicate-id check then rejects, correctly: that is a
    malformed envelope listing one event twice, not two events.

    No prose survives into the canon: a slug is a normalized, truncated, `[a-z0-9_]`-only
    token, not the description text. `CanonEventV1` still stores no description field, and
    §10's "prose never becomes a compared value" is unchanged.
    """
    normalized = canon_lite._norm_text(description)
    digest = canon_lite.sha256_hex(
        canon_lite.canonical_bytes([normalized, int(occurs_chapter)]))[:12]
    slug = re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")[:_EVENT_SLUG_MAX]
    slug = slug.strip("_")
    # An all-non-ASCII description slugifies to "" — the id is then hash-only, still valid
    # and still unique, just not legible. Legibility is best-effort; uniqueness is not.
    return f"evt_{slug}_{digest}" if slug else f"evt_{digest}"


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
        # `description` is read for VALIDATION ONLY (bounded length, must be a real
        # string) — it is intentionally NOT stored on `CanonEventV1`, which carries only
        # `event_id`/`occurs_chapter_order` (§6.1's identity+placement, no prose). Prose
        # never becomes a hashed/compared value (§10) — this mirrors why `CanonRevealV1`
        # does not store a reveal's secret either.
        canon_lite._req_str(row["description"],
                            f"semantic_source.one_time_events[{i}].description",
                            max_len=canon_lite.MAX_LITERAL_LEN)
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
            event_id=_event_id(row["description"], occurs), occurs_chapter_order=occurs))

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


def parse_structured_bible_response(raw: Any) -> Optional[tuple]:
    """Decode ONE Story Bible response under the P0-B STRUCTURED contract.

    Returns `(bible_text, semantic_source_raw, canon_registry_raw)` — the prose fact-sheet,
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
        return None
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    bible_text = obj.get(STRUCTURED_BIBLE_TEXT_KEY)
    semantic_raw = obj.get(STRUCTURED_BIBLE_SEMANTIC_KEY)
    # `type(x) is str`, not isinstance: a str SUBCLASS can carry a poisoned __eq__/__hash__
    # and this value goes on to be hashed and compared for provenance. Same discipline as
    # `_l3_canonicalize_verdict` in the P0-A preflight.
    if type(bible_text) is not str or not bible_text.strip():
        return None
    if not isinstance(semantic_raw, dict):
        return None
    registry_raw = obj.get(STRUCTURED_BIBLE_REGISTRY_KEY)
    if not isinstance(registry_raw, dict):
        registry_raw = None
    return (bible_text.strip(), semantic_raw, registry_raw)


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
    "STRUCTURED_BIBLE_TEXT_KEY",
    "STRUCTURED_BIBLE_SEMANTIC_KEY",
    "STRUCTURED_BIBLE_REGISTRY_KEY",
    "extract_semantic_source_json",
]
