"""Continuity Core CC-01 -- deterministic, code-compiled Chapter Brief v1.

A Brief is a bounded, chapter-relevant slice of the Story Contract -- never an LLM summary,
never raw prior-chapter prose, never a full manuscript. ``compile_chapter_brief`` is pure and
deterministic: the same (contract, ledger, chapter_id) always yields the same brief.
"""
from __future__ import annotations

from .compiler import contract_hash
from .ledger import ledger_binding_errors
from .models import ContractValidationError, canonical_json, deep_freeze_copy, sha256_hex

_BRIEF_SCHEMA_VERSION = "1"
_MAX_BRIEF_BYTES = 16_384


def _find_chapter(contract: dict, chapter_id: str) -> dict:
    for chapter in contract["chapter_contracts"]:
        if chapter["chapter_id"] == chapter_id:
            return chapter
    raise ContractValidationError(f"unknown chapter_id: {chapter_id!r}")


def _transitioning_ids(start_map: dict, end_map: dict) -> set:
    missing = object()
    keys = set(start_map) | set(end_map)
    return {key for key in keys if start_map.get(key, missing) != end_map.get(key, missing)}


def _event_ordering_closure(seed_ids: set, events_by_id: dict) -> set:
    closure = set(seed_ids)
    frontier = list(seed_ids)
    while frontier:
        current = frontier.pop()
        event = events_by_id.get(current)
        if event is None:
            continue
        for neighbor in event["before_event_ids"] + event["after_event_ids"]:
            if neighbor not in closure:
                closure.add(neighbor)
                frontier.append(neighbor)
    return closure


def compile_chapter_brief(contract: dict, ledger: dict, chapter_id: str) -> dict:
    ledger_errors = ledger_binding_errors(contract, ledger)
    if ledger_errors:
        raise ContractValidationError("; ".join(ledger_errors))

    chapter = _find_chapter(contract, chapter_id)
    relevant_entities = set(chapter["relevant_entity_ids"])

    # Event relevance: required this chapter, transitions this chapter, or referenced by one of
    # this chapter's exceptions -- plus the ordering-dependency closure needed to explain those.
    # Participant overlap ALONE is never sufficient (it let unrelated protagonist-sharing events
    # leak into every brief they share a character with).
    events_by_id = {event["event_id"]: event for event in contract["events"]}
    exception_event_ids = {
        exc["event_id"] for exc in contract["exceptions"]
        if exc["chapter_id"] == chapter["chapter_id"] and exc.get("event_id") is not None
    }
    event_seed = (
        set(chapter["must_include_event_ids"])
        | _transitioning_ids(chapter["start_state"]["event_status"], chapter["end_state"]["event_status"])
        | exception_event_ids
    )
    relevant_events = _event_ordering_closure(event_seed, events_by_id)

    # Object relevance: the holder changes this chapter, or an allowed_transfer is planned for
    # this chapter -- not merely "appears somewhere in the start/end state maps," which pulled in
    # every statically-held, never-transferred object regardless of relevance.
    relevant_objects = _transitioning_ids(
        chapter["start_state"]["object_holders"], chapter["end_state"]["object_holders"]
    ) | {
        obj["object_id"] for obj in contract["objects"]
        if any(transfer["chapter_id"] == chapter["chapter_id"] for transfer in obj["allowed_transfers"])
    }
    relevant_locations = set(chapter["start_state"]["location_by_character"].values()) | set(
        chapter["end_state"]["location_by_character"].values()
    )
    relevant_reveals = set(chapter["must_not_claim_reveal_ids"]) | set(chapter["available_reveal_ids"])
    relevant_chapter_id = chapter["chapter_id"]

    entities = [c for c in contract["characters"] if c["entity_id"] in relevant_entities]
    events = [e for e in contract["events"] if e["event_id"] in relevant_events]
    objects = [o for o in contract["objects"] if o["object_id"] in relevant_objects]
    locations = [loc for loc in contract["locations"] if loc["location_id"] in relevant_locations]
    this_chapter_number = chapter["chapter_number"]
    relationships = [
        r for r in contract["relationships"]
        if r["from_entity_id"] in relevant_entities and r["to_entity_id"] in relevant_entities
        and ledger["chapter_numbers"][r["valid_from_chapter_id"]] <= this_chapter_number
        and (r["valid_to_chapter_id"] is None or this_chapter_number <= ledger["chapter_numbers"][r["valid_to_chapter_id"]])
    ]
    reveals = [r for r in contract["reveals"] if r["reveal_id"] in relevant_reveals]
    quantities = [q for q in contract["quantities"] if q["anchor_event_id"] in relevant_events]
    threads = [
        t for t in contract["threads"]
        if relevant_chapter_id in ({t["opened_chapter_id"], t["resolved_chapter_id"]} | set(t["must_progress_chapter_ids"]))
    ]
    exceptions = [e for e in contract["exceptions"] if e["chapter_id"] == relevant_chapter_id]

    brief_without_hash = {
        "schema_version": _BRIEF_SCHEMA_VERSION,
        "story_id": contract["story_id"],
        "contract_id": contract["contract_id"],
        "contract_hash": contract_hash(contract),
        "chapter_id": chapter["chapter_id"],
        "chapter_number": chapter["chapter_number"],
        "title": chapter["title"],
        "target_language": contract["target_language"],
        "immutable_rules": contract["immutable_rules"],
        "start_state": chapter["start_state"],
        "end_state": chapter["end_state"],
        "must_include_event_ids": chapter["must_include_event_ids"],
        "must_not_claim_reveal_ids": chapter["must_not_claim_reveal_ids"],
        "available_reveal_ids": chapter["available_reveal_ids"],
        "entities": entities,
        "relationships": relationships,
        "locations": locations,
        "events": events,
        "reveals": reveals,
        "objects": objects,
        "quantities": quantities,
        "threads": threads,
        "exceptions": exceptions,
        "neighbor_state": {
            "previous_end": chapter["start_state"],
            "next_start": chapter["end_state"],
        },
        "ledger_summary": {
            "completed_prefix": ledger["completed_prefix"],
            "committed_chapter_ids": sorted(ledger["commits"]),
        },
    }
    brief_without_hash = deep_freeze_copy(brief_without_hash)
    brief_hash = sha256_hex(canonical_json(brief_without_hash))
    brief = dict(brief_without_hash)
    brief["brief_hash"] = brief_hash
    if len(canonical_json(brief).encode("utf-8")) > _MAX_BRIEF_BYTES:
        raise ContractValidationError(
            f"chapter brief for {chapter_id!r} exceeds the {_MAX_BRIEF_BYTES}-byte bound even "
            "after relevance filtering; compilation fails closed rather than returning it"
        )
    return brief


def canonical_brief_json(brief: dict) -> str:
    return canonical_json(brief)
