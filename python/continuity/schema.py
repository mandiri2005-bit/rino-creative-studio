"""Continuity Core CC-01 -- Bible V2 JSON schema, referential, and semantic validation.

``validate_bible_v2`` is the only public entry point. It never adds, deletes, coerces, or
silently defaults a field; every modeled level rejects unknown keys; every reference must
resolve to the correct ID family; validation failures raise ``ContractValidationError`` with
no partial/fallback object ever returned.
"""
from __future__ import annotations

import math
import re
from typing import Any

from .lifecycle import LifecycleValidationError, canonicalize_target_language
from .models import ContractValidationError, deep_freeze_copy

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{1,8})*$")

# ---------------------------------------------------------------------------------------
# Fixed-shape (extra=forbid) key sets for every nested object the Bible V2 schema defines.
# ---------------------------------------------------------------------------------------
_ROOT_KEYS = frozenset({
    "schema_version", "contract_id", "story_id", "outline_hash", "target_language",
    "chapter_count", "immutable_rules", "characters", "relationships", "locations",
    "events", "reveals", "objects", "quantities", "threads", "chapter_contracts", "exceptions",
})
_IMMUTABLE_RULES_KEYS = frozenset({"pov", "tense", "forbidden_languages", "global_forbidden_claims"})
_CHARACTER_KEYS = frozenset({
    "entity_id", "canonical_name", "aliases", "forbidden_names", "pronouns", "attributes",
    "first_introduction_chapter_id",
})
_RELATIONSHIP_KEYS = frozenset({
    "relationship_id", "from_entity_id", "to_entity_id", "kind", "symmetric",
    "valid_from_chapter_id", "valid_to_chapter_id",
})
_LOCATION_KEYS = frozenset({"location_id", "canonical_name", "aliases"})
_EVENT_KEYS = frozenset({
    "event_id", "canonical_summary", "occurrence_policy", "occurs_chapter_id", "irreversible",
    "allowed_retellings", "participant_ids", "before_event_ids", "after_event_ids",
})
_REVEAL_KEYS = frozenset({"reveal_id", "canonical_fact", "first_reveal_chapter_id", "known_by_chapter"})
_OBJECT_KEYS = frozenset({"object_id", "canonical_name", "initial_holder_id", "allowed_transfers"})
_TRANSFER_KEYS = frozenset({"chapter_id", "from_holder_id", "to_holder_id"})
_QUANTITY_KEYS = frozenset({"quantity_id", "kind", "value", "unit", "anchor_event_id"})
_THREAD_KEYS = frozenset({
    "thread_id", "canonical_summary", "opened_chapter_id", "must_progress_chapter_ids",
    "resolved_chapter_id", "resolution_requirement",
})
_CHAPTER_CONTRACT_KEYS = frozenset({
    "chapter_id", "chapter_number", "title", "relevant_entity_ids", "start_state",
    "must_include_event_ids", "must_not_claim_reveal_ids", "available_reveal_ids", "end_state",
})
_STATE_KEYS = frozenset({"location_by_character", "event_status", "object_holders"})
_EXCEPTION_KEYS = frozenset({
    "exception_id", "kind", "chapter_id", "event_id", "reveal_id", "character_id",
    "allowed_presentation_mode",
})

_OCCURRENCE_POLICIES = frozenset({"once", "repeatable"})
_ALLOWED_RETELLINGS = frozenset({"reference", "recap", "flashback"})
_EVENT_STATUSES = frozenset({"not_occurred", "occurred"})


def _fail(message: str) -> None:
    raise ContractValidationError(message)


def _require_dict(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list:
    if not isinstance(value, list):
        _fail(f"{label} must be an array")
    return value


def _require_str_list(value: Any, label: str) -> list:
    _require_list(value, label)
    for i, item in enumerate(value):
        _require_str(item, f"{label}[{i}]")
    return value


def _require_number(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be a number")
    # Arbitrary-size Python/JSON integers are always exact and finite by construction -- only a
    # float needs a finiteness check. math.isfinite() on a huge int raises OverflowError trying
    # to convert it to a float first, which would otherwise leak as a raw, undocumented exception.
    if isinstance(value, float) and not math.isfinite(value):
        _fail(f"{label} must be finite (not NaN or infinite)")


def _require_str(value: Any, label: str, *, allow_blank: bool = True) -> str:
    if not isinstance(value, str):
        _fail(f"{label} must be a string")
    if not allow_blank and value.strip() == "":
        _fail(f"{label} must not be blank")
    return value


def _validate_attributes(value: Any, label: str) -> dict:
    attrs = _require_dict(value, label)
    for key, item in attrs.items():
        _require_str(key, f"{label} key", allow_blank=False)
        if isinstance(item, bool):
            _fail(f"{label}[{key}] must be a string, number, or string array")
        if isinstance(item, (int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                _fail(f"{label}[{key}] must be finite (not NaN or infinite)")
            continue
        if isinstance(item, str):
            _require_str(item, f"{label}[{key}]", allow_blank=False)
            continue
        if isinstance(item, list):
            _require_str_list(item, f"{label}[{key}]")
            continue
        _fail(f"{label}[{key}] must be a string, number, or string array")
    return attrs


def _exact_keys(obj: dict, required: frozenset, label: str) -> None:
    keys = set(obj)
    missing = required - keys
    extra = keys - required
    if missing:
        _fail(f"{label} missing field(s): {sorted(missing)}")
    if extra:
        _fail(f"{label} has unknown field(s): {sorted(extra)}")


def _unique(ids: list, label: str) -> None:
    seen = set()
    for value in ids:
        if value in seen:
            _fail(f"duplicate {label}: {value!r}")
        seen.add(value)


def _require_ref(value: Any, family: dict, family_name: str, label: str, *, nullable: bool = False) -> None:
    if value is None:
        if nullable:
            return
        _fail(f"{label} must not be null")
    if not isinstance(value, str):
        _fail(f"{label} must be a string {family_name} id, got {type(value).__name__}")
    if value not in family:
        _fail(f"{label} does not resolve to a known {family_name} id: {value!r}")


def _require_ref_list(values: Any, family: dict, family_name: str, label: str) -> None:
    _require_list(values, label)
    for value in values:
        _require_ref(value, family, family_name, label)


def _validate_state(state: Any, label: str, entity_ids: dict, event_ids: dict, location_ids: dict, object_ids: dict) -> dict:
    state = _require_dict(state, label)
    _exact_keys(state, _STATE_KEYS, label)
    loc_by_char = _require_dict(state["location_by_character"], f"{label}.location_by_character")
    for char_id, loc_id in loc_by_char.items():
        _require_ref(char_id, entity_ids, "character", f"{label}.location_by_character key")
        _require_ref(loc_id, location_ids, "location", f"{label}.location_by_character[{char_id}]")
    event_status = _require_dict(state["event_status"], f"{label}.event_status")
    for evt_id, status in event_status.items():
        _require_ref(evt_id, event_ids, "event", f"{label}.event_status key")
        if not isinstance(status, str) or status not in _EVENT_STATUSES:
            _fail(f"{label}.event_status[{evt_id}] invalid status: {status!r}")
    object_holders = _require_dict(state["object_holders"], f"{label}.object_holders")
    for obj_id, holder_id in object_holders.items():
        _require_ref(obj_id, object_ids, "object", f"{label}.object_holders key")
        _require_ref(holder_id, entity_ids, "character", f"{label}.object_holders[{obj_id}]")
    return state


def _check_events_acyclic(events: list) -> None:
    graph: dict = {event["event_id"]: set() for event in events}
    for event in events:
        eid = event["event_id"]
        for before_id in event["before_event_ids"]:
            graph.setdefault(before_id, set()).add(eid)
        for after_id in event["after_event_ids"]:
            graph.setdefault(eid, set()).add(after_id)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}

    def visit(node: str) -> None:
        color[node] = GRAY
        for neighbor in graph.get(node, ()):
            state = color.get(neighbor, WHITE)
            if state == GRAY:
                _fail("bible_v2.events: before/after ordering contains a cycle or self-reference")
            if state == WHITE:
                visit(neighbor)
        color[node] = BLACK

    for node in list(graph):
        if color[node] == WHITE:
            visit(node)


def validate_bible_v2(payload: dict) -> dict:
    """Validate a Bible V2 JSON mapping. Returns a deep-copied, unmodified-in-content mapping
    on success (no field is added, removed, coerced, or silently defaulted). Raises
    ``ContractValidationError`` on any schema, referential, or semantic failure -- never a
    partial object."""
    root = _require_dict(payload, "bible_v2")
    _exact_keys(root, _ROOT_KEYS, "bible_v2")

    if root["schema_version"] != "2":
        _fail(f"bible_v2.schema_version must be exactly '2', got {root['schema_version']!r}")
    _require_str(root["contract_id"], "bible_v2.contract_id", allow_blank=False)
    _require_str(root["story_id"], "bible_v2.story_id", allow_blank=False)
    outline_hash = _require_str(root["outline_hash"], "bible_v2.outline_hash")
    if not _HEX64_RE.fullmatch(outline_hash):
        _fail("bible_v2.outline_hash must be lowercase 64-hex")
    target_language = _require_str(root["target_language"], "bible_v2.target_language", allow_blank=False)
    if not _LANG_RE.fullmatch(target_language):
        # BV2-01 (Codex-authorized bridge): frozen CC-01 already accepts BCP-47-like tags via
        # _LANG_RE above (including mixed-case regions like "en-US" -- unchanged). This ONLY
        # extends acceptance to the exact canonical custom-language authority B-07/B-08 already
        # approved (e.g. "Basa Jawa Krama") -- a value must equal its OWN canonical form
        # exactly, so a resealed/re-cased/re-spaced non-canonical variant still rejects.
        try:
            _is_canonical_custom_language = canonicalize_target_language(target_language) == target_language
        except LifecycleValidationError:
            _is_canonical_custom_language = False
        if not _is_canonical_custom_language:
            _fail(f"bible_v2.target_language must be a normalized lower-case language code: {target_language!r}")
    if not isinstance(root["chapter_count"], int) or isinstance(root["chapter_count"], bool):
        _fail("bible_v2.chapter_count must be an integer")
    if root["chapter_count"] <= 0:
        _fail("bible_v2.chapter_count must be positive")

    immutable_rules = _require_dict(root["immutable_rules"], "bible_v2.immutable_rules")
    _exact_keys(immutable_rules, _IMMUTABLE_RULES_KEYS, "bible_v2.immutable_rules")
    _require_str(immutable_rules["pov"], "immutable_rules.pov", allow_blank=False)
    _require_str(immutable_rules["tense"], "immutable_rules.tense", allow_blank=False)
    _require_str_list(immutable_rules["forbidden_languages"], "immutable_rules.forbidden_languages")
    _require_str_list(immutable_rules["global_forbidden_claims"], "immutable_rules.global_forbidden_claims")

    characters = _require_list(root["characters"], "bible_v2.characters")
    entity_ids: dict = {}
    for i, character in enumerate(characters):
        label = f"bible_v2.characters[{i}]"
        character = _require_dict(character, label)
        _exact_keys(character, _CHARACTER_KEYS, label)
        _require_str(character["entity_id"], f"{label}.entity_id", allow_blank=False)
        _require_str(character["canonical_name"], f"{label}.canonical_name", allow_blank=False)
        _require_str_list(character["aliases"], f"{label}.aliases")
        _require_str_list(character["forbidden_names"], f"{label}.forbidden_names")
        _require_str_list(character["pronouns"], f"{label}.pronouns")
        _validate_attributes(character["attributes"], f"{label}.attributes")
        entity_ids[character["entity_id"]] = character
    _unique([c["entity_id"] for c in characters], "entity_id")

    locations = _require_list(root["locations"], "bible_v2.locations")
    location_ids: dict = {}
    for i, location in enumerate(locations):
        label = f"bible_v2.locations[{i}]"
        location = _require_dict(location, label)
        _exact_keys(location, _LOCATION_KEYS, label)
        _require_str(location["location_id"], f"{label}.location_id", allow_blank=False)
        _require_str(location["canonical_name"], f"{label}.canonical_name", allow_blank=False)
        _require_str_list(location["aliases"], f"{label}.aliases")
        location_ids[location["location_id"]] = location
    _unique([loc["location_id"] for loc in locations], "location_id")

    events = _require_list(root["events"], "bible_v2.events")
    event_ids: dict = {}
    for i, event in enumerate(events):
        label = f"bible_v2.events[{i}]"
        event = _require_dict(event, label)
        _exact_keys(event, _EVENT_KEYS, label)
        _require_str(event["event_id"], f"{label}.event_id", allow_blank=False)
        _require_str(event["canonical_summary"], f"{label}.canonical_summary", allow_blank=False)
        if not isinstance(event["occurrence_policy"], str) or event["occurrence_policy"] not in _OCCURRENCE_POLICIES:
            _fail(f"{label}.occurrence_policy invalid: {event['occurrence_policy']!r}")
        if not isinstance(event["irreversible"], bool):
            _fail(f"{label}.irreversible must be a boolean")
        retellings = _require_list(event["allowed_retellings"], f"{label}.allowed_retellings")
        for r in retellings:
            if not isinstance(r, str) or r not in _ALLOWED_RETELLINGS:
                _fail(f"{label}.allowed_retellings has invalid value: {r!r}")
        _require_str_list(event["participant_ids"], f"{label}.participant_ids")
        _require_str_list(event["before_event_ids"], f"{label}.before_event_ids")
        _require_str_list(event["after_event_ids"], f"{label}.after_event_ids")
        event_ids[event["event_id"]] = event
    _unique([e["event_id"] for e in events], "event_id")
    for i, event in enumerate(events):
        label = f"bible_v2.events[{i}]"
        for pid in event["participant_ids"]:
            _require_ref(pid, entity_ids, "character", f"{label}.participant_ids")
        for eid in event["before_event_ids"] + event["after_event_ids"]:
            _require_ref(eid, event_ids, "event", f"{label}.before/after_event_ids")
        if event["occurrence_policy"] == "once":
            if event["occurs_chapter_id"] is None:
                _fail(f"{label}: occurrence_policy 'once' requires a planned occurs_chapter_id")
            elif not isinstance(event["occurs_chapter_id"], str):
                _fail(f"{label}.occurs_chapter_id must be a string")
        elif event["occurs_chapter_id"] is not None and not isinstance(event["occurs_chapter_id"], str):
            _fail(f"{label}.occurs_chapter_id must be a string or null")
    _check_events_acyclic(events)

    reveals = _require_list(root["reveals"], "bible_v2.reveals")
    reveal_ids: dict = {}
    for i, reveal in enumerate(reveals):
        label = f"bible_v2.reveals[{i}]"
        reveal = _require_dict(reveal, label)
        _exact_keys(reveal, _REVEAL_KEYS, label)
        _require_str(reveal["reveal_id"], f"{label}.reveal_id", allow_blank=False)
        _require_str(reveal["canonical_fact"], f"{label}.canonical_fact", allow_blank=False)
        known_by = _require_dict(reveal["known_by_chapter"], f"{label}.known_by_chapter")
        for char_id in known_by:
            _require_ref(char_id, entity_ids, "character", f"{label}.known_by_chapter key")
        reveal_ids[reveal["reveal_id"]] = reveal
    _unique([r["reveal_id"] for r in reveals], "reveal_id")

    objects = _require_list(root["objects"], "bible_v2.objects")
    object_ids: dict = {}
    for i, obj in enumerate(objects):
        label = f"bible_v2.objects[{i}]"
        obj = _require_dict(obj, label)
        _exact_keys(obj, _OBJECT_KEYS, label)
        _require_str(obj["object_id"], f"{label}.object_id", allow_blank=False)
        _require_str(obj["canonical_name"], f"{label}.canonical_name", allow_blank=False)
        transfers = _require_list(obj["allowed_transfers"], f"{label}.allowed_transfers")
        for j, transfer in enumerate(transfers):
            tlabel = f"{label}.allowed_transfers[{j}]"
            transfer = _require_dict(transfer, tlabel)
            _exact_keys(transfer, _TRANSFER_KEYS, tlabel)
            _require_str(transfer["chapter_id"], f"{tlabel}.chapter_id", allow_blank=False)
            _require_str(transfer["from_holder_id"], f"{tlabel}.from_holder_id", allow_blank=False)
            _require_str(transfer["to_holder_id"], f"{tlabel}.to_holder_id", allow_blank=False)
        object_ids[obj["object_id"]] = obj
    _unique([o["object_id"] for o in objects], "object_id")
    for i, obj in enumerate(objects):
        label = f"bible_v2.objects[{i}]"
        _require_ref(obj["initial_holder_id"], entity_ids, "character", f"{label}.initial_holder_id")
        for j, transfer in enumerate(obj["allowed_transfers"]):
            tlabel = f"{label}.allowed_transfers[{j}]"
            _require_ref(transfer["from_holder_id"], entity_ids, "character", f"{tlabel}.from_holder_id")
            _require_ref(transfer["to_holder_id"], entity_ids, "character", f"{tlabel}.to_holder_id")

    quantities = _require_list(root["quantities"], "bible_v2.quantities")
    for i, qty in enumerate(quantities):
        label = f"bible_v2.quantities[{i}]"
        qty = _require_dict(qty, label)
        _exact_keys(qty, _QUANTITY_KEYS, label)
        _require_str(qty["quantity_id"], f"{label}.quantity_id", allow_blank=False)
        _require_str(qty["kind"], f"{label}.kind", allow_blank=False)
        _require_number(qty["value"], f"{label}.value")
        _require_str(qty["unit"], f"{label}.unit", allow_blank=False)
    _unique([q["quantity_id"] for q in quantities], "quantity_id")
    for i, qty in enumerate(quantities):
        _require_ref(qty["anchor_event_id"], event_ids, "event", f"bible_v2.quantities[{i}].anchor_event_id")

    threads = _require_list(root["threads"], "bible_v2.threads")
    for i, thread in enumerate(threads):
        label = f"bible_v2.threads[{i}]"
        thread = _require_dict(thread, label)
        _exact_keys(thread, _THREAD_KEYS, label)
        _require_str(thread["thread_id"], f"{label}.thread_id", allow_blank=False)
        _require_str(thread["canonical_summary"], f"{label}.canonical_summary", allow_blank=False)
        _require_str_list(thread["must_progress_chapter_ids"], f"{label}.must_progress_chapter_ids")
        _require_str(thread["resolution_requirement"], f"{label}.resolution_requirement", allow_blank=False)
    _unique([t["thread_id"] for t in threads], "thread_id")

    chapter_contracts = _require_list(root["chapter_contracts"], "bible_v2.chapter_contracts")
    if root["chapter_count"] != len(chapter_contracts):
        _fail("bible_v2.chapter_count must equal len(chapter_contracts)")
    chapter_ids: dict = {}
    for i, chapter in enumerate(chapter_contracts):
        label = f"bible_v2.chapter_contracts[{i}]"
        chapter = _require_dict(chapter, label)
        _exact_keys(chapter, _CHAPTER_CONTRACT_KEYS, label)
        _require_str(chapter["chapter_id"], f"{label}.chapter_id", allow_blank=False)
        if not isinstance(chapter["chapter_number"], int) or isinstance(chapter["chapter_number"], bool):
            _fail(f"{label}.chapter_number must be an integer")
        _require_str(chapter["title"], f"{label}.title", allow_blank=False)
        _require_str_list(chapter["relevant_entity_ids"], f"{label}.relevant_entity_ids")
        _require_str_list(chapter["must_include_event_ids"], f"{label}.must_include_event_ids")
        _require_str_list(chapter["must_not_claim_reveal_ids"], f"{label}.must_not_claim_reveal_ids")
        _require_str_list(chapter["available_reveal_ids"], f"{label}.available_reveal_ids")
        chapter_ids[chapter["chapter_id"]] = chapter
    _unique([c["chapter_id"] for c in chapter_contracts], "chapter_id")
    numbers = sorted(c["chapter_number"] for c in chapter_contracts)
    if numbers != list(range(1, len(chapter_contracts) + 1)):
        _fail("bible_v2.chapter_contracts chapter_number values must be exactly 1..N, one each")

    # Referential checks that need the complete chapter_ids family (deferred from the loop above).
    for i, event in enumerate(events):
        if event["occurrence_policy"] == "once":
            _require_ref(event["occurs_chapter_id"], chapter_ids, "chapter", f"bible_v2.events[{i}].occurs_chapter_id")
        elif event["occurs_chapter_id"] is not None:
            _require_ref(event["occurs_chapter_id"], chapter_ids, "chapter", f"bible_v2.events[{i}].occurs_chapter_id", nullable=True)
    for i, character in enumerate(characters):
        _require_ref(character["first_introduction_chapter_id"], chapter_ids, "chapter",
                      f"bible_v2.characters[{i}].first_introduction_chapter_id")
    for i, reveal in enumerate(reveals):
        _require_ref(reveal["first_reveal_chapter_id"], chapter_ids, "chapter",
                      f"bible_v2.reveals[{i}].first_reveal_chapter_id")
        for char_id, ch_id in reveal["known_by_chapter"].items():
            _require_ref(ch_id, chapter_ids, "chapter", f"bible_v2.reveals[{i}].known_by_chapter[{char_id}]")
    for i, obj in enumerate(objects):
        for j, transfer in enumerate(obj["allowed_transfers"]):
            _require_ref(transfer["chapter_id"], chapter_ids, "chapter",
                          f"bible_v2.objects[{i}].allowed_transfers[{j}].chapter_id")
    for i, thread in enumerate(threads):
        label = f"bible_v2.threads[{i}]"
        _require_ref(thread["opened_chapter_id"], chapter_ids, "chapter", f"{label}.opened_chapter_id")
        _require_ref(thread["resolved_chapter_id"], chapter_ids, "chapter", f"{label}.resolved_chapter_id", nullable=True)
        for ch_id in thread["must_progress_chapter_ids"]:
            _require_ref(ch_id, chapter_ids, "chapter", f"{label}.must_progress_chapter_ids")
    for i, rel in enumerate(relationships := _require_list(root["relationships"], "bible_v2.relationships")):
        label = f"bible_v2.relationships[{i}]"
        rel = _require_dict(rel, label)
        _exact_keys(rel, _RELATIONSHIP_KEYS, label)
        _require_str(rel["relationship_id"], f"{label}.relationship_id", allow_blank=False)
        _require_str(rel["kind"], f"{label}.kind", allow_blank=False)
        if not isinstance(rel["symmetric"], bool):
            _fail(f"{label}.symmetric must be a boolean")
        _require_ref(rel["from_entity_id"], entity_ids, "character", f"{label}.from_entity_id")
        _require_ref(rel["to_entity_id"], entity_ids, "character", f"{label}.to_entity_id")
        _require_ref(rel["valid_from_chapter_id"], chapter_ids, "chapter", f"{label}.valid_from_chapter_id")
        _require_ref(rel["valid_to_chapter_id"], chapter_ids, "chapter", f"{label}.valid_to_chapter_id", nullable=True)
        if rel["valid_to_chapter_id"] is not None:
            from_num = chapter_ids[rel["valid_from_chapter_id"]]["chapter_number"]
            to_num = chapter_ids[rel["valid_to_chapter_id"]]["chapter_number"]
            if to_num < from_num:
                _fail(f"{label}: valid_to_chapter_id cannot precede valid_from_chapter_id")
    _unique([r["relationship_id"] for r in relationships], "relationship_id")

    for i, chapter in enumerate(chapter_contracts):
        label = f"bible_v2.chapter_contracts[{i}]"
        for eid in chapter["relevant_entity_ids"]:
            _require_ref(eid, entity_ids, "character", f"{label}.relevant_entity_ids")
        for eid in chapter["must_include_event_ids"]:
            _require_ref(eid, event_ids, "event", f"{label}.must_include_event_ids")
        for rid in chapter["must_not_claim_reveal_ids"] + chapter["available_reveal_ids"]:
            _require_ref(rid, reveal_ids, "reveal", f"{label}.must_not/available_reveal_ids")
        chapter["start_state"] = _validate_state(chapter["start_state"], f"{label}.start_state", entity_ids, event_ids, location_ids, object_ids)
        chapter["end_state"] = _validate_state(chapter["end_state"], f"{label}.end_state", entity_ids, event_ids, location_ids, object_ids)

    by_number = {c["chapter_number"]: c for c in chapter_contracts}
    for n in range(1, len(chapter_contracts)):
        if by_number[n]["end_state"] != by_number[n + 1]["start_state"]:
            _fail(f"chapter_contracts: chapter {n}'s end_state must equal chapter {n + 1}'s start_state")

    exceptions = _require_list(root["exceptions"], "bible_v2.exceptions")
    for i, exc in enumerate(exceptions):
        label = f"bible_v2.exceptions[{i}]"
        exc = _require_dict(exc, label)
        _exact_keys(exc, _EXCEPTION_KEYS, label)
        _require_str(exc["exception_id"], f"{label}.exception_id", allow_blank=False)
        _require_str(exc["kind"], f"{label}.kind", allow_blank=False)
        _require_str(exc["allowed_presentation_mode"], f"{label}.allowed_presentation_mode", allow_blank=False)
        _require_ref(exc["chapter_id"], chapter_ids, "chapter", f"{label}.chapter_id")
        _require_ref(exc["event_id"], event_ids, "event", f"{label}.event_id", nullable=True)
        _require_ref(exc["reveal_id"], reveal_ids, "reveal", f"{label}.reveal_id", nullable=True)
        _require_ref(exc["character_id"], entity_ids, "character", f"{label}.character_id", nullable=True)
        if exc["kind"] not in ("false_belief", "flashback"):
            _fail(f"{label}.kind must be 'false_belief' or 'flashback', got {exc['kind']!r}")
        if exc["kind"] == "false_belief":
            if exc["event_id"] is not None:
                _fail(f"{label}: kind='false_belief' requires a null event_id")
            if exc["reveal_id"] is None:
                _fail(f"{label}: kind='false_belief' requires a non-null reveal_id")
            if exc["character_id"] is None:
                _fail(f"{label}: kind='false_belief' requires a non-null character_id")
            if exc["allowed_presentation_mode"] != "false_belief":
                _fail(f"{label}: kind='false_belief' requires allowed_presentation_mode='false_belief'")
        elif exc["kind"] == "flashback":
            if exc["reveal_id"] is not None:
                _fail(f"{label}: kind='flashback' requires a null reveal_id")
            if exc["event_id"] is None:
                _fail(f"{label}: kind='flashback' requires a non-null event_id")
            if exc["allowed_presentation_mode"] != "flashback":
                _fail(f"{label}: kind='flashback' requires allowed_presentation_mode='flashback'")
            if "flashback" not in event_ids[exc["event_id"]]["allowed_retellings"]:
                _fail(f"{label}: event {exc['event_id']!r} does not declare 'flashback' in allowed_retellings")
    _unique([e["exception_id"] for e in exceptions], "exception_id")

    return deep_freeze_copy(root)
