"""Continuity Core CC-01 -- pre/post generation gates.

Every check here is deterministic code; there is no LLM/provider/network call anywhere in this
module. ``post_generation_gate`` always independently hashes ``chapter_text`` and never trusts
``claims["content_hash"]`` (or ``claims["language_signals"]``) for anything. Both gates rebuild
the expected Chapter Brief from contract+ledger+chapter (never merely re-hashing whatever the
caller supplied) and validate the ledger through the one shared, reusable ledger validator.
"""
from __future__ import annotations

import math
import re
from typing import Any

from .compiler import contract_hash as _contract_hash
from .ledger import ledger_binding_errors
from .models import canonical_json, sha256_hex
from .slicer import compile_chapter_brief as _compile_chapter_brief

_MAX_EVIDENCE = 500
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

_BLOCK_CODES = frozenset({
    "CONTRACT_HASH_MISMATCH", "TARGET_LANGUAGE_MISMATCH", "PLANNED_STATE_MISMATCH",
    "INPUT_HASH_MISMATCH", "CLAIMS_BINDING_MISMATCH", "BRIEF_HASH_MISMATCH",
    "LEDGER_BINDING_MISMATCH", "CLAIMS_SCHEMA_INVALID",
})
_REPAIR_CODES = frozenset({
    "FORBIDDEN_NAME", "ATTRIBUTE_CONFLICT", "ONCE_EVENT_DUPLICATE", "PREMATURE_REVEAL",
    "CUSTODY_CONFLICT", "MISSING_REQUIRED_EVENT", "LANGUAGE_LEAK",
    "EVENT_CHAPTER_MISMATCH", "UNDECLARED_EXCEPTION", "GLOBAL_FORBIDDEN_CLAIM",
    "RETELLING_NOT_ALLOWED",
})

# Curated Indonesian function-word/particle list for an independent, deterministic leak scan --
# the SAME curated word list and whole-word/case-insensitive detection contract as the existing
# python/narasi_counters.py:language_consistency_word_scan (read before writing this module, per
# the implementation order), reimplemented standalone here so `continuity` carries zero import-
# time dependency on that much larger, pipeline-coupled module.
_ID_LEAK_WORDS = (
    "dengan", "tidak", "adalah", "sudah", "mereka", "kami",
    "karena", "jika", "kalau", "kapan", "bagaimana", "mengapa", "kamu",
)
_ID_LEAK_RE = re.compile(r"(?i)\b(" + "|".join(sorted(_ID_LEAK_WORDS, key=len, reverse=True)) + r")\b")
# Full Indonesian-family skip set, matching python/narasi_counters.py's _A3_ID_FAMILY verbatim --
# these words are native in each of these target languages, not a leak.
_ID_FAMILY = frozenset({"id", "jv", "su", "ms", "min", "ban", "bug", "mad", "ace", "bjn"})

_CLAIMS_ROOT_KEYS = frozenset({
    "schema_version", "story_id", "contract_id", "chapter_id", "chapter_number", "content_hash",
    "target_language", "start_state", "end_state", "entities", "events", "reveals", "objects",
    "quantities", "threads", "language_signals",
})
_CLAIMS_STATE_KEYS = frozenset({"location_by_character", "event_status", "object_holders"})
_CLAIMS_EVENT_STATUSES = frozenset({"not_occurred", "occurred"})
_CLAIMS_ENTITY_KEYS = frozenset({"entity_id", "attributes", "evidence"})
_CLAIMS_EVENT_KEYS = frozenset({"event_id", "presentation_mode", "evidence"})
_CLAIMS_REVEAL_KEYS = frozenset({"reveal_id", "presentation_mode", "character_id", "evidence"})
_CLAIMS_OBJECT_KEYS = frozenset({"object_id", "holder_id", "evidence"})
_CLAIMS_QUANTITY_KEYS = frozenset({"quantity_id", "value", "unit", "evidence"})
_CLAIMS_THREAD_KEYS = frozenset({"thread_id", "status", "evidence"})
_CLAIMS_THREAD_STATUSES = frozenset({"opened", "progressed", "resolved"})
_CLAIMS_LANGSIG_KEYS = frozenset({"detected_language", "foreign_spans"})
_CLAIMS_EVENT_MODES = frozenset({"new_occurrence", "reference", "recap", "flashback"})
_CLAIMS_REVEAL_MODES = frozenset({"new_reveal", "reference", "false_belief"})


def _evidence(text: Any) -> str:
    return str(text)[:_MAX_EVIDENCE]


def _violation(code: str, chapter_id: str, evidence: Any, expected: Any, actual: Any) -> dict:
    severity = "blocker" if code in _BLOCK_CODES else "high"
    return {
        "code": code,
        "severity": severity,
        "chapter_id": chapter_id,
        "evidence": _evidence(evidence),
        "expected": expected,
        "actual": actual,
    }


def _sort_violations(violations: list) -> list:
    return sorted(violations, key=lambda v: (v["code"], v["chapter_id"], v["evidence"]))


def _decision_for(violations: list) -> str:
    codes = {v["code"] for v in violations}
    if codes & _BLOCK_CODES:
        return "block"
    if codes & _REPAIR_CODES:
        return "repair"
    return "pass"


def _find_chapter(contract: dict, chapter_id: str):
    for chapter in contract["chapter_contracts"]:
        if chapter["chapter_id"] == chapter_id:
            return chapter
    return None


def _gate_result(*, contract: dict, chapter_id: str, content_hash, violations: list, coverage: str) -> dict:
    violations = _sort_violations(violations)
    decision = _decision_for(violations)
    return {
        "ok": decision == "pass",
        "decision": decision,
        "coverage": coverage,
        "story_id": contract["story_id"],
        "contract_id": contract["contract_id"],
        "contract_hash": _contract_hash(contract),
        "chapter_id": chapter_id,
        "content_hash": content_hash,
        "violations": violations,
    }


def _ledger_binding_violation(contract: dict, ledger: dict, chapter_id: str):
    errors = ledger_binding_errors(contract, ledger)
    if errors:
        return _violation("LEDGER_BINDING_MISMATCH", chapter_id, "; ".join(errors), "a valid bound ledger", None)
    return None


def _claims_attribute_errors(attrs: Any, label: str) -> list:
    """Same recursive scalar contract as Bible V2 character attributes: nonblank string keys;
    string, finite number, or string-array values; no nested objects, booleans, nulls, sets, or
    non-string array members."""
    errors: list = []
    if not isinstance(attrs, dict):
        errors.append(f"{label} must be an object")
        return errors
    for key, value in attrs.items():
        if not isinstance(key, str) or key.strip() == "":
            errors.append(f"{label} has a non-string or blank key: {key!r}")
        if isinstance(value, bool):
            errors.append(f"{label}[{key!r}] must be a string, finite number, or string array")
        elif isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                errors.append(f"{label}[{key!r}] must be finite (not NaN or infinite)")
        elif isinstance(value, str):
            if value.strip() == "":
                errors.append(f"{label}[{key!r}] must be a nonblank string")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if not isinstance(item, str):
                    errors.append(f"{label}[{key!r}][{i}] must be a string")
        else:
            errors.append(f"{label}[{key!r}] must be a string, finite number, or string array")
    return errors


def _claims_schema_errors(claims: Any) -> list:
    """Strict, fully recursive Claims v1 envelope validation. Returns a list of human-readable
    problems (empty = valid); never raises -- every modeled ID, language, enum, name, evidence,
    unit, status, list member, and map value has an explicit type check here, so no malformed
    nested value can ever escape as a raw TypeError/KeyError from a later semantic check."""
    errors: list = []
    if not isinstance(claims, dict):
        return ["claims must be an object"]
    keys = set(claims)
    missing = _CLAIMS_ROOT_KEYS - keys
    extra = keys - _CLAIMS_ROOT_KEYS
    if missing:
        errors.append(f"claims missing field(s): {sorted(missing)}")
    if extra:
        errors.append(f"claims has unknown field(s): {sorted(extra)}")
    if errors:
        return errors

    def _typed_list(value, label):
        if not isinstance(value, list):
            errors.append(f"{label} must be an array")
            return []
        out = []
        for i, item in enumerate(value):
            if not isinstance(item, dict):
                errors.append(f"{label}[{i}] must be an object")
            else:
                out.append((i, item))
        return out

    def _exact(item: dict, required: frozenset, label: str) -> None:
        ikeys = set(item)
        imissing = required - ikeys
        iextra = ikeys - required
        if imissing:
            errors.append(f"{label} missing field(s): {sorted(imissing)}")
        if iextra:
            errors.append(f"{label} has unknown field(s): {sorted(iextra)}")

    def _str_field(item: dict, field: str, label: str) -> None:
        if field in item and not isinstance(item[field], str):
            errors.append(f"{label}.{field} must be a string")

    def _state_map_key(k: Any, label: str) -> None:
        if not isinstance(k, str) or k.strip() == "":
            errors.append(f"{label} has a non-string or blank key: {k!r}")

    def _state_map(state: dict, label: str) -> None:
        loc_by_char = state.get("location_by_character")
        if not isinstance(loc_by_char, dict):
            errors.append(f"{label}.location_by_character must be an object")
        else:
            for k, v in loc_by_char.items():
                _state_map_key(k, f"{label}.location_by_character")
                if not isinstance(v, str):
                    errors.append(f"{label}.location_by_character[{k!r}] must be a string")
        event_status = state.get("event_status")
        if not isinstance(event_status, dict):
            errors.append(f"{label}.event_status must be an object")
        else:
            for k, v in event_status.items():
                _state_map_key(k, f"{label}.event_status")
                if not isinstance(v, str) or v not in _CLAIMS_EVENT_STATUSES:
                    errors.append(f"{label}.event_status[{k!r}] invalid status: {v!r}")
        object_holders = state.get("object_holders")
        if not isinstance(object_holders, dict):
            errors.append(f"{label}.object_holders must be an object")
        else:
            for k, v in object_holders.items():
                _state_map_key(k, f"{label}.object_holders")
                if not isinstance(v, str):
                    errors.append(f"{label}.object_holders[{k!r}] must be a string")

    if not isinstance(claims.get("schema_version"), str):
        errors.append("claims.schema_version must be a string")
    for key in ("story_id", "contract_id", "chapter_id", "target_language"):
        if not isinstance(claims.get(key), str):
            errors.append(f"claims.{key} must be a string")
    content_hash = claims.get("content_hash")
    if not isinstance(content_hash, str) or not _HEX64_RE.fullmatch(content_hash):
        errors.append("claims.content_hash must be a lowercase 64-hex string")
    if not isinstance(claims.get("chapter_number"), int) or isinstance(claims.get("chapter_number"), bool):
        errors.append("claims.chapter_number must be an integer")

    for state_key in ("start_state", "end_state"):
        state = claims.get(state_key)
        if not isinstance(state, dict):
            errors.append(f"claims.{state_key} must be an object")
            continue
        _exact(state, _CLAIMS_STATE_KEYS, f"claims.{state_key}")
        _state_map(state, f"claims.{state_key}")

    for i, item in _typed_list(claims.get("entities"), "claims.entities"):
        label = f"claims.entities[{i}]"
        _exact(item, _CLAIMS_ENTITY_KEYS, label)
        _str_field(item, "entity_id", label)
        _str_field(item, "evidence", label)
        if "attributes" in item:
            errors.extend(_claims_attribute_errors(item["attributes"], f"{label}.attributes"))

    for i, item in _typed_list(claims.get("events"), "claims.events"):
        label = f"claims.events[{i}]"
        _exact(item, _CLAIMS_EVENT_KEYS, label)
        _str_field(item, "event_id", label)
        _str_field(item, "evidence", label)
        if "presentation_mode" in item and item["presentation_mode"] not in _CLAIMS_EVENT_MODES:
            errors.append(f"{label}.presentation_mode invalid: {item['presentation_mode']!r}")

    for i, item in _typed_list(claims.get("reveals"), "claims.reveals"):
        label = f"claims.reveals[{i}]"
        _exact(item, _CLAIMS_REVEAL_KEYS, label)
        _str_field(item, "reveal_id", label)
        _str_field(item, "character_id", label)
        _str_field(item, "evidence", label)
        if "presentation_mode" in item and item["presentation_mode"] not in _CLAIMS_REVEAL_MODES:
            errors.append(f"{label}.presentation_mode invalid: {item['presentation_mode']!r}")

    for i, item in _typed_list(claims.get("objects"), "claims.objects"):
        label = f"claims.objects[{i}]"
        _exact(item, _CLAIMS_OBJECT_KEYS, label)
        _str_field(item, "object_id", label)
        _str_field(item, "holder_id", label)
        _str_field(item, "evidence", label)

    for i, item in _typed_list(claims.get("quantities"), "claims.quantities"):
        label = f"claims.quantities[{i}]"
        _exact(item, _CLAIMS_QUANTITY_KEYS, label)
        _str_field(item, "quantity_id", label)
        _str_field(item, "unit", label)
        _str_field(item, "evidence", label)
        if "value" in item:
            value = item["value"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"{label}.value must be a number")
            elif isinstance(value, float) and not math.isfinite(value):
                errors.append(f"{label}.value must be finite (not NaN or infinite)")

    for i, item in _typed_list(claims.get("threads"), "claims.threads"):
        label = f"claims.threads[{i}]"
        _exact(item, _CLAIMS_THREAD_KEYS, label)
        _str_field(item, "thread_id", label)
        _str_field(item, "evidence", label)
        if "status" in item and (not isinstance(item["status"], str) or item["status"] not in _CLAIMS_THREAD_STATUSES):
            errors.append(f"{label}.status invalid: {item['status']!r}")

    lang_signals = claims.get("language_signals")
    if not isinstance(lang_signals, dict):
        errors.append("claims.language_signals must be an object")
    else:
        _exact(lang_signals, _CLAIMS_LANGSIG_KEYS, "claims.language_signals")
        _str_field(lang_signals, "detected_language", "claims.language_signals")
        spans = lang_signals.get("foreign_spans")
        if "foreign_spans" in lang_signals and not isinstance(spans, list):
            errors.append("claims.language_signals.foreign_spans must be an array")
        elif isinstance(spans, list):
            for i, span in enumerate(spans):
                if not isinstance(span, str):
                    errors.append(f"claims.language_signals.foreign_spans[{i}] must be a string")

    return errors


def pre_generation_gate(contract: dict, ledger: dict, brief: dict) -> dict:
    chapter_id = brief.get("chapter_id") if isinstance(brief, dict) else None
    chapter = _find_chapter(contract, chapter_id) if isinstance(brief, dict) else None
    expected_contract_hash = _contract_hash(contract)
    violations: list = []

    if not isinstance(brief, dict):
        violations.append(_violation(
            "BRIEF_HASH_MISMATCH", chapter_id, str(brief), "a brief object", brief,
        ))
        return _gate_result(contract=contract, chapter_id=chapter_id, content_hash=None, violations=violations, coverage="complete")

    if chapter is None:
        violations.append(_violation(
            "CONTRACT_HASH_MISMATCH", chapter_id, "brief.chapter_id does not resolve in contract",
            expected_contract_hash, brief.get("contract_hash"),
        ))
        return _gate_result(contract=contract, chapter_id=chapter_id, content_hash=None, violations=violations, coverage="complete")

    ledger_violation = _ledger_binding_violation(contract, ledger, chapter_id)
    if ledger_violation is not None:
        violations.append(ledger_violation)
        return _gate_result(contract=contract, chapter_id=chapter_id, content_hash=None, violations=violations, coverage="incomplete")

    if brief.get("contract_hash") != expected_contract_hash:
        violations.append(_violation(
            "CONTRACT_HASH_MISMATCH", chapter_id, str(brief.get("contract_hash")),
            expected_contract_hash, brief.get("contract_hash"),
        ))

    # Expected-artifact re-derivation: rebuild the Brief from contract+ledger+chapter_id rather
    # than merely re-hashing whatever payload the caller supplied. A recomputed hash proves only
    # self-consistency, not that the mapping was actually compiled from this contract/ledger --
    # a caller can forge any field and reseal a self-consistent brief_hash over the forgery.
    expected_brief = _compile_chapter_brief(contract, ledger, chapter_id)
    if not isinstance(brief, dict) or brief != expected_brief:
        violations.append(_violation(
            "BRIEF_HASH_MISMATCH", chapter_id, str(brief.get("brief_hash") if isinstance(brief, dict) else brief),
            expected_brief["brief_hash"], brief.get("brief_hash") if isinstance(brief, dict) else None,
        ))

    if brief.get("target_language") != contract["target_language"]:
        violations.append(_violation(
            "TARGET_LANGUAGE_MISMATCH", chapter_id, str(brief.get("target_language")),
            contract["target_language"], brief.get("target_language"),
        ))
    if brief.get("start_state") != chapter["start_state"]:
        violations.append(_violation(
            "PLANNED_STATE_MISMATCH", chapter_id, "start_state", chapter["start_state"], brief.get("start_state"),
        ))
    if brief.get("end_state") != chapter["end_state"]:
        violations.append(_violation(
            "PLANNED_STATE_MISMATCH", chapter_id, "end_state", chapter["end_state"], brief.get("end_state"),
        ))
    return _gate_result(contract=contract, chapter_id=chapter_id, content_hash=None, violations=violations, coverage="complete")


def post_generation_gate(contract: dict, ledger: dict, brief: dict, chapter_text: str, claims: dict) -> dict:
    chapter_id = brief.get("chapter_id") if isinstance(brief, dict) else None
    chapter = _find_chapter(contract, chapter_id) if isinstance(brief, dict) else None
    expected_contract_hash = _contract_hash(contract)
    real_content_hash = sha256_hex(chapter_text)
    violations: list = []

    def _finish(*, coverage: str) -> dict:
        result = _gate_result(
            contract=contract, chapter_id=chapter_id, content_hash=real_content_hash,
            violations=violations, coverage=coverage,
        )
        result["brief_hash"] = brief.get("brief_hash") if isinstance(brief, dict) else None
        result["chapter_number"] = brief.get("chapter_number") if isinstance(brief, dict) else None
        try:
            result["claims_hash"] = sha256_hex(canonical_json(claims)) if isinstance(claims, dict) else None
        except (TypeError, ValueError):
            result["claims_hash"] = None
        try:
            result["ledger_hash"] = sha256_hex(canonical_json(ledger)) if isinstance(ledger, dict) else None
        except (TypeError, ValueError):
            result["ledger_hash"] = None
        return result

    if not isinstance(brief, dict):
        violations.append(_violation(
            "BRIEF_HASH_MISMATCH", chapter_id, str(brief), "a brief object", brief,
        ))
        return _finish(coverage="complete")

    if chapter is None or brief.get("contract_hash") != expected_contract_hash:
        violations.append(_violation(
            "CONTRACT_HASH_MISMATCH", chapter_id, str(brief.get("contract_hash")),
            expected_contract_hash, brief.get("contract_hash"),
        ))
        return _finish(coverage="complete")

    ledger_violation = _ledger_binding_violation(contract, ledger, chapter_id)
    if ledger_violation is not None:
        violations.append(ledger_violation)
        return _finish(coverage="incomplete")

    expected_brief = _compile_chapter_brief(contract, ledger, chapter_id)
    if not isinstance(brief, dict) or brief != expected_brief:
        violations.append(_violation(
            "BRIEF_HASH_MISMATCH", chapter_id, str(brief.get("brief_hash") if isinstance(brief, dict) else brief),
            expected_brief["brief_hash"], brief.get("brief_hash") if isinstance(brief, dict) else None,
        ))

    if brief.get("target_language") != contract["target_language"]:
        violations.append(_violation(
            "TARGET_LANGUAGE_MISMATCH", chapter_id, str(brief.get("target_language")),
            contract["target_language"], brief.get("target_language"),
        ))

    # Strict, recursive Claims v1 schema validation runs BEFORE any value comparison on claims
    # (including content_hash's own format) -- a malformed envelope is CLAIMS_SCHEMA_INVALID
    # even when a field also happens to carry the "wrong" value for some other check.
    schema_errors = _claims_schema_errors(claims)
    if schema_errors:
        violations.append(_violation(
            "CLAIMS_SCHEMA_INVALID", chapter_id, "; ".join(schema_errors),
            "a valid Claims v1 envelope", None,
        ))
        return _finish(coverage="incomplete")

    claimed_hash = claims.get("content_hash")
    if claimed_hash != real_content_hash:
        violations.append(_violation(
            "INPUT_HASH_MISMATCH", chapter_id, str(claimed_hash), real_content_hash, claimed_hash,
        ))
        return _finish(coverage="complete")

    # ---- identity binding + referential binding of every claim, all fail-closed together ----
    binding_violations: list = []
    if claims.get("schema_version") != "1":
        binding_violations.append(_violation(
            "CLAIMS_BINDING_MISMATCH", chapter_id, str(claims.get("schema_version")), "1", claims.get("schema_version"),
        ))
    if claims.get("story_id") != brief.get("story_id"):
        binding_violations.append(_violation(
            "CLAIMS_BINDING_MISMATCH", chapter_id, str(claims.get("story_id")), brief.get("story_id"), claims.get("story_id"),
        ))
    if claims.get("contract_id") != brief.get("contract_id"):
        binding_violations.append(_violation(
            "CLAIMS_BINDING_MISMATCH", chapter_id, str(claims.get("contract_id")), brief.get("contract_id"), claims.get("contract_id"),
        ))
    if claims.get("chapter_id") != chapter_id:
        binding_violations.append(_violation(
            "CLAIMS_BINDING_MISMATCH", chapter_id, str(claims.get("chapter_id")), chapter_id, claims.get("chapter_id"),
        ))
    if claims.get("chapter_number") != brief.get("chapter_number"):
        binding_violations.append(_violation(
            "CLAIMS_BINDING_MISMATCH", chapter_id, str(claims.get("chapter_number")), brief.get("chapter_number"), claims.get("chapter_number"),
        ))
    if claims.get("target_language") != brief.get("target_language"):
        binding_violations.append(_violation(
            "CLAIMS_BINDING_MISMATCH", chapter_id, str(claims.get("target_language")), brief.get("target_language"), claims.get("target_language"),
        ))

    entity_ids = {c["entity_id"] for c in contract["characters"]}
    event_ids = {e["event_id"] for e in contract["events"]}
    reveal_ids = {r["reveal_id"] for r in contract["reveals"]}
    object_ids = {o["object_id"] for o in contract["objects"]}
    quantity_ids = {q["quantity_id"] for q in contract["quantities"]}
    thread_ids = {t["thread_id"] for t in contract["threads"]}
    location_ids = {loc["location_id"] for loc in contract["locations"]}

    def _ref(value, family, family_name, evidence_label):
        if value not in family:
            binding_violations.append(_violation(
                "CLAIMS_BINDING_MISMATCH", chapter_id, evidence_label, f"a known {family_name} id", value,
            ))

    for entity in claims.get("entities", []):
        _ref(entity.get("entity_id"), entity_ids, "entity", f"entity_id={entity.get('entity_id')!r}")
    for event in claims.get("events", []):
        _ref(event.get("event_id"), event_ids, "event", f"event_id={event.get('event_id')!r}")
    for reveal in claims.get("reveals", []):
        _ref(reveal.get("reveal_id"), reveal_ids, "reveal", f"reveal_id={reveal.get('reveal_id')!r}")
        _ref(reveal.get("character_id"), entity_ids, "entity", f"reveal.character_id={reveal.get('character_id')!r}")
    for obj in claims.get("objects", []):
        _ref(obj.get("object_id"), object_ids, "object", f"object_id={obj.get('object_id')!r}")
        _ref(obj.get("holder_id"), entity_ids, "entity", f"object.holder_id={obj.get('holder_id')!r}")
    for qty in claims.get("quantities", []):
        _ref(qty.get("quantity_id"), quantity_ids, "quantity", f"quantity_id={qty.get('quantity_id')!r}")
    for thread in claims.get("threads", []):
        _ref(thread.get("thread_id"), thread_ids, "thread", f"thread_id={thread.get('thread_id')!r}")
    for state_key in ("start_state", "end_state"):
        state = claims.get(state_key) or {}
        for char_id, loc_id in (state.get("location_by_character") or {}).items():
            _ref(char_id, entity_ids, "entity", f"{state_key}.location_by_character key={char_id!r}")
            _ref(loc_id, location_ids, "location", f"{state_key}.location_by_character[{char_id!r}]={loc_id!r}")
        for evt_id in (state.get("event_status") or {}):
            _ref(evt_id, event_ids, "event", f"{state_key}.event_status key={evt_id!r}")
        for obj_id, holder_id in (state.get("object_holders") or {}).items():
            _ref(obj_id, object_ids, "object", f"{state_key}.object_holders key={obj_id!r}")
            _ref(holder_id, entity_ids, "entity", f"{state_key}.object_holders[{obj_id!r}]={holder_id!r}")

    if binding_violations:
        violations.extend(binding_violations)
        return _finish(coverage="incomplete")

    # ---- claimed start/end state vs this chapter's planned state ----
    if claims.get("start_state") != chapter["start_state"]:
        violations.append(_violation(
            "PLANNED_STATE_MISMATCH", chapter_id, "claims.start_state", chapter["start_state"], claims.get("start_state"),
        ))
    if claims.get("end_state") != chapter["end_state"]:
        violations.append(_violation(
            "PLANNED_STATE_MISMATCH", chapter_id, "claims.end_state", chapter["end_state"], claims.get("end_state"),
        ))

    by_entity = {c["entity_id"]: c for c in contract["characters"]}
    by_event = {e["event_id"]: e for e in contract["events"]}

    # ---- FORBIDDEN_NAME: story-wide, never trusts claims ----
    for character in contract["characters"]:
        for forbidden in character.get("forbidden_names", []):
            if not forbidden:
                continue
            match = re.search(r"\b" + re.escape(forbidden) + r"\b", chapter_text)
            if match:
                start, end = match.start(), match.end()
                snippet = chapter_text[max(0, start - 30): end + 30]
                violations.append(_violation(
                    "FORBIDDEN_NAME", chapter_id, snippet, character["canonical_name"], forbidden,
                ))

    # ---- GLOBAL_FORBIDDEN_CLAIM: story-wide literal scan, never trusts claims ----
    for forbidden_claim in contract["immutable_rules"].get("global_forbidden_claims", []):
        if not forbidden_claim:
            continue
        idx = chapter_text.find(forbidden_claim)
        if idx != -1:
            snippet = chapter_text[max(0, idx - 30): idx + len(forbidden_claim) + 30]
            violations.append(_violation(
                "GLOBAL_FORBIDDEN_CLAIM", chapter_id, snippet, "must not appear in chapter_text", forbidden_claim,
            ))

    # ---- ATTRIBUTE_CONFLICT: claimed entity attributes/pronouns vs canonical bible facts ----
    for claim_entity in claims.get("entities", []):
        bible_char = by_entity.get(claim_entity.get("entity_id"))
        if bible_char is None:
            continue
        claim_attrs = claim_entity.get("attributes") or {}
        if "pronouns" in claim_attrs and claim_attrs["pronouns"] != bible_char.get("pronouns"):
            violations.append(_violation(
                "ATTRIBUTE_CONFLICT", chapter_id, claim_entity.get("evidence", ""),
                bible_char.get("pronouns"), claim_attrs["pronouns"],
            ))
        bible_attrs = bible_char.get("attributes") or {}
        for key, value in claim_attrs.items():
            if key == "pronouns":
                continue
            if key in bible_attrs and bible_attrs[key] != value:
                violations.append(_violation(
                    "ATTRIBUTE_CONFLICT", chapter_id, claim_entity.get("evidence", ""), bible_attrs[key], value,
                ))

    # ---- EVENT_CHAPTER_MISMATCH + RETELLING_NOT_ALLOWED: per-claim event mode checks ----
    for event in claims.get("events", []):
        bible_event = by_event.get(event.get("event_id"))
        if bible_event is None:
            continue
        mode = event.get("presentation_mode")
        if mode == "new_occurrence" and bible_event["occurrence_policy"] == "once":
            if bible_event.get("occurs_chapter_id") != chapter_id:
                violations.append(_violation(
                    "EVENT_CHAPTER_MISMATCH", chapter_id, event.get("evidence", ""),
                    bible_event.get("occurs_chapter_id"), chapter_id,
                ))
        if mode in ("reference", "recap", "flashback") and mode not in bible_event.get("allowed_retellings", []):
            violations.append(_violation(
                "RETELLING_NOT_ALLOWED", chapter_id, event.get("evidence", ""),
                bible_event.get("allowed_retellings"), mode,
            ))

    # ---- ONCE_EVENT_DUPLICATE: at most one new_occurrence claim, ever, per once-policy event ----
    once_events = {e["event_id"] for e in contract["events"] if e["occurrence_policy"] == "once"}
    prior_new_occurrence = set()
    for record in (ledger.get("commits") or {}).values():
        for event in (record.get("claims") or {}).get("events", []):
            if event.get("presentation_mode") == "new_occurrence":
                prior_new_occurrence.add(event.get("event_id"))
    seen_this_chapter = set()
    for event in claims.get("events", []):
        event_id = event.get("event_id")
        if event.get("presentation_mode") != "new_occurrence" or event_id not in once_events:
            continue
        if event_id in prior_new_occurrence or event_id in seen_this_chapter:
            violations.append(_violation(
                "ONCE_EVENT_DUPLICATE", chapter_id, event.get("evidence", ""),
                "at most one new_occurrence for a once-policy event", event_id,
            ))
        seen_this_chapter.add(event_id)

    # ---- UNDECLARED_EXCEPTION: false_belief/flashback modes require a matching brief exception --
    # a presentation-mode string alone grants nothing; the exception must actually be declared for
    # this exact reveal/character (false_belief) or event (flashback) in the current chapter's brief.
    brief_exceptions = brief.get("exceptions") or []
    for reveal in claims.get("reveals", []):
        if reveal.get("presentation_mode") == "false_belief":
            matched = any(
                exc.get("kind") == "false_belief"
                and exc.get("reveal_id") == reveal.get("reveal_id")
                and exc.get("character_id") == reveal.get("character_id")
                for exc in brief_exceptions
            )
            if not matched:
                violations.append(_violation(
                    "UNDECLARED_EXCEPTION", chapter_id, reveal.get("evidence", ""),
                    "a declared false_belief exception for this reveal/character", reveal.get("reveal_id"),
                ))
    for event in claims.get("events", []):
        if event.get("presentation_mode") == "flashback":
            matched = any(
                exc.get("kind") == "flashback" and exc.get("event_id") == event.get("event_id")
                for exc in brief_exceptions
            )
            if not matched:
                violations.append(_violation(
                    "UNDECLARED_EXCEPTION", chapter_id, event.get("evidence", ""),
                    "a declared flashback exception for this event", event.get("event_id"),
                ))

    # ---- PREMATURE_REVEAL: a new_reveal claim for a reveal this chapter cannot yet make ----
    available_reveals = set(brief.get("available_reveal_ids") or [])
    for reveal in claims.get("reveals", []):
        if reveal.get("presentation_mode") == "new_reveal" and reveal.get("reveal_id") not in available_reveals:
            violations.append(_violation(
                "PREMATURE_REVEAL", chapter_id, reveal.get("evidence", ""),
                "reveal available in this chapter", reveal.get("reveal_id"),
            ))

    # ---- CUSTODY_CONFLICT: claimed object holder vs the contract's planned end-state holder ----
    expected_holders = chapter["end_state"]["object_holders"]
    for obj in claims.get("objects", []):
        expected_holder = expected_holders.get(obj.get("object_id"))
        if expected_holder is not None and obj.get("holder_id") != expected_holder:
            violations.append(_violation(
                "CUSTODY_CONFLICT", chapter_id, obj.get("evidence", ""), expected_holder, obj.get("holder_id"),
            ))

    # ---- MISSING_REQUIRED_EVENT: a required event is satisfied only by a new_occurrence claim,
    # never merely a reference/recap ----
    claimed_new_occurrence_ids = {
        e.get("event_id") for e in claims.get("events", []) if e.get("presentation_mode") == "new_occurrence"
    }
    for required in chapter["must_include_event_ids"]:
        if required not in claimed_new_occurrence_ids:
            violations.append(_violation(
                "MISSING_REQUIRED_EVENT", chapter_id, "no new_occurrence claim for required event", required, None,
            ))

    # ---- LANGUAGE_LEAK: independent scan of chapter_text; never trusts claims.language_signals ----
    target_language = (contract.get("target_language") or "").split("-")[0].lower()
    if target_language not in _ID_FAMILY:
        match = _ID_LEAK_RE.search(chapter_text)
        if match:
            start, end = match.start(), match.end()
            snippet = chapter_text[max(0, start - 24): end + 30]
            violations.append(_violation(
                "LANGUAGE_LEAK", chapter_id, snippet, contract["target_language"], match.group(0),
            ))

    return _finish(coverage="complete")
