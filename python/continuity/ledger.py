"""Continuity Core CC-01 -- immutable, out-of-order-safe in-memory continuity ledger.

Chapter workers remain parallel: a later chapter may pass its gate and commit before an
earlier one. ``completed_prefix`` only ever reports the largest dense run starting at chapter
1. Every function here returns a new ledger value; the input ledger is never mutated.
"""
from __future__ import annotations

import re
from typing import Any

from .compiler import contract_hash
from .models import LedgerTransitionError, canonical_json, deep_freeze_copy, sha256_hex


_SCHEMA_VERSION = "1"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_LEDGER_TOP_KEYS = frozenset({
    "schema_version", "story_id", "contract_id", "contract_hash",
    "chapter_numbers", "commits", "completed_prefix",
})
_COMMIT_RECORD_KEYS = frozenset({"content_hash", "chapter_number", "claims", "claims_hash"})


def _is_exact_int(value: Any) -> bool:
    """True int, never bool -- ``isinstance(True, int)`` is True in Python, and plain ``==``
    equality treats ``True == 1``, so every integer-typed field in this module must be checked
    with this helper instead of a bare ``isinstance(..., int)`` or ``==``."""
    return isinstance(value, int) and not isinstance(value, bool)


def _expected_chapter_numbers(contract: dict) -> dict:
    return {c["chapter_id"]: c["chapter_number"] for c in contract["chapter_contracts"]}


def new_ledger(contract: dict) -> dict:
    return {
        "schema_version": _SCHEMA_VERSION,
        "story_id": contract["story_id"],
        "contract_id": contract["contract_id"],
        "contract_hash": contract_hash(contract),
        "chapter_numbers": _expected_chapter_numbers(contract),
        "commits": {},
        "completed_prefix": 0,
    }


def _completed_prefix(commits: dict) -> int:
    numbers = {record["chapter_number"] for record in commits.values()}
    prefix = 0
    while (prefix + 1) in numbers:
        prefix += 1
    return prefix


def _self_contained_commit_record_errors(story_id: Any, contract_id: Any, chapter_numbers: dict, commits: dict) -> list:
    """Validates every commit record's exact shape, hash formats, and cross-binding to the
    ledger's OWN story_id/contract_id/chapter_numbers -- entirely self-referential, no Story
    Contract needed. Never raises (including for non-canonical/non-finite embedded Claims)."""
    errors: list = []
    seen_numbers: dict = {}
    for chapter_id, record in commits.items():
        label = f"ledger.commits[{chapter_id!r}]"
        if chapter_id not in chapter_numbers:
            errors.append(f"{label}: chapter_id is not part of ledger.chapter_numbers")
            continue
        if not isinstance(record, dict):
            errors.append(f"{label} must be an object")
            continue
        rkeys = set(record)
        rmissing = _COMMIT_RECORD_KEYS - rkeys
        rextra = rkeys - _COMMIT_RECORD_KEYS
        if rmissing:
            errors.append(f"{label} missing field(s): {sorted(rmissing)}")
        if rextra:
            errors.append(f"{label} has unknown field(s): {sorted(rextra)}")
        if rmissing or rextra:
            continue

        content_hash = record.get("content_hash")
        if not isinstance(content_hash, str) or not _HEX64_RE.fullmatch(content_hash):
            errors.append(f"{label}.content_hash must be a lowercase 64-hex string")

        expected_number = chapter_numbers.get(chapter_id)
        chapter_number = record.get("chapter_number")
        if not _is_exact_int(chapter_number):
            errors.append(f"{label}.chapter_number must be an integer, never a boolean")
        elif chapter_number != expected_number:
            errors.append(f"{label}.chapter_number does not match ledger.chapter_numbers[{chapter_id!r}]")
        elif chapter_number in seen_numbers:
            errors.append(f"{label}.chapter_number {chapter_number} duplicates {seen_numbers[chapter_number]!r}")
        else:
            seen_numbers[chapter_number] = chapter_id

        claims = record.get("claims")
        if not isinstance(claims, dict):
            errors.append(f"{label}.claims must be an object")
            continue

        claims_hash = record.get("claims_hash")
        if not isinstance(claims_hash, str) or not _HEX64_RE.fullmatch(claims_hash):
            errors.append(f"{label}.claims_hash must be a lowercase 64-hex string")
        else:
            try:
                expected_claims_hash = sha256_hex(canonical_json(claims))
            except (TypeError, ValueError):
                errors.append(f"{label}.claims is not canonically serializable")
                expected_claims_hash = None
            if expected_claims_hash is not None and claims_hash != expected_claims_hash:
                errors.append(f"{label}.claims_hash does not match the hash of {label}.claims")

        if claims.get("content_hash") != content_hash:
            errors.append(f"{label}.claims.content_hash does not match {label}.content_hash")
        if claims.get("story_id") != story_id:
            errors.append(f"{label}.claims.story_id does not match ledger.story_id")
        if claims.get("contract_id") != contract_id:
            errors.append(f"{label}.claims.contract_id does not match ledger.contract_id")
        if claims.get("chapter_id") != chapter_id:
            errors.append(f"{label}.claims.chapter_id does not match the record key {chapter_id!r}")
        claims_chapter_number = claims.get("chapter_number")
        if not _is_exact_int(claims_chapter_number):
            errors.append(f"{label}.claims.chapter_number must be an integer, never a boolean or float")
        elif claims_chapter_number != expected_number:
            errors.append(f"{label}.claims.chapter_number does not match ledger.chapter_numbers[{chapter_id!r}]")
    return errors


def ledger_structure_errors(ledger: Any) -> list:
    """The one SELF-CONTAINED, exact ledger-structure validator: requires no Story Contract.
    Verifies exact top-level shape/types (strict int-vs-bool, nonblank/unique-positive chapter
    map), every commit record's exact shape and hash formats, and every record's cross-binding
    to the ledger's own identity/chapter map. Returns a list of problems (empty = internally
    consistent). Never raises. ``ledger_binding_errors`` below extends this with contract-specific
    checks; this validator is also called directly by ``commit_chapter`` and
    ``invalidate_from_chapter``, which never receive a Story Contract."""
    if not isinstance(ledger, dict):
        return ["ledger must be an object"]
    keys = set(ledger)
    missing = _LEDGER_TOP_KEYS - keys
    extra = keys - _LEDGER_TOP_KEYS
    errors: list = []
    if missing:
        errors.append(f"ledger missing field(s): {sorted(missing)}")
    if extra:
        errors.append(f"ledger has unknown field(s): {sorted(extra)}")
    if errors:
        return errors

    if ledger.get("schema_version") != _SCHEMA_VERSION:
        errors.append(f"ledger.schema_version must be {_SCHEMA_VERSION!r}, got {ledger.get('schema_version')!r}")
    story_id = ledger.get("story_id")
    contract_id = ledger.get("contract_id")
    if not isinstance(story_id, str) or story_id.strip() == "":
        errors.append("ledger.story_id must be a nonblank string")
    if not isinstance(contract_id, str) or contract_id.strip() == "":
        errors.append("ledger.contract_id must be a nonblank string")
    contract_hash_value = ledger.get("contract_hash")
    if not isinstance(contract_hash_value, str) or not _HEX64_RE.fullmatch(contract_hash_value):
        errors.append("ledger.contract_hash must be a lowercase 64-hex string")

    chapter_numbers = ledger.get("chapter_numbers")
    if not isinstance(chapter_numbers, dict):
        errors.append("ledger.chapter_numbers must be an object")
        chapter_numbers = {}
    else:
        seen_values: dict = {}
        for key, value in chapter_numbers.items():
            if not isinstance(key, str) or key.strip() == "":
                errors.append(f"ledger.chapter_numbers has a non-string or blank key: {key!r}")
            if not _is_exact_int(value) or value <= 0:
                errors.append(f"ledger.chapter_numbers[{key!r}] must be a positive integer, never a boolean")
            elif value in seen_values:
                errors.append(f"ledger.chapter_numbers[{key!r}] duplicates the value at {seen_values[value]!r}")
            else:
                seen_values[value] = key

    commits = ledger.get("commits")
    if not isinstance(commits, dict):
        errors.append("ledger.commits must be an object")
        return errors  # cannot validate records or completed_prefix without a dict

    errors.extend(_self_contained_commit_record_errors(story_id, contract_id, chapter_numbers, commits))
    if errors:
        return errors  # a malformed record makes completed_prefix derivation unreliable

    completed_prefix = ledger.get("completed_prefix")
    if not _is_exact_int(completed_prefix) or completed_prefix < 0 or completed_prefix != _completed_prefix(commits):
        errors.append("ledger.completed_prefix must be a nonnegative integer equal to the value derived from ledger.commits")
    return errors


def ledger_binding_errors(contract: dict, ledger: Any) -> list:
    """Contract-aware EXTENSION of ``ledger_structure_errors``: everything the self-contained
    validator checks, PLUS that the ledger's own (already internally-consistent) identity and
    chapter map actually match the supplied Story Contract. Used by the slicer and both gates,
    which always have a ``contract`` to check against. Never raises."""
    errors = ledger_structure_errors(ledger)
    if errors:
        return errors
    if ledger.get("story_id") != contract.get("story_id"):
        errors.append("ledger.story_id does not match contract.story_id")
    if ledger.get("contract_id") != contract.get("contract_id"):
        errors.append("ledger.contract_id does not match contract.contract_id")
    if ledger.get("contract_hash") != contract_hash(contract):
        errors.append("ledger.contract_hash does not match the contract's own canonical hash")
    if ledger.get("chapter_numbers") != _expected_chapter_numbers(contract):
        errors.append("ledger.chapter_numbers does not match the contract-derived chapter map")
    return errors


def commit_chapter(ledger: dict, claims: dict, gate_result: dict) -> dict:
    structure_errors = ledger_structure_errors(ledger)
    if structure_errors:
        raise LedgerTransitionError("; ".join(structure_errors))

    if not gate_result.get("ok") or gate_result.get("decision") != "pass" or gate_result.get("coverage") != "complete":
        raise LedgerTransitionError("commit requires ok=true, decision='pass', coverage='complete'")
    if gate_result.get("violations"):
        raise LedgerTransitionError("commit requires an empty gate_result.violations list")
    if gate_result.get("story_id") != ledger["story_id"]:
        raise LedgerTransitionError("gate_result.story_id does not match ledger story_id")
    if gate_result.get("contract_id") != ledger["contract_id"]:
        raise LedgerTransitionError("gate_result.contract_id does not match ledger contract_id")
    if gate_result.get("contract_hash") != ledger["contract_hash"]:
        raise LedgerTransitionError("gate_result.contract_hash does not match ledger contract_hash")
    if claims.get("story_id") != ledger["story_id"]:
        raise LedgerTransitionError("claims.story_id does not match ledger story_id")
    if claims.get("contract_id") != ledger["contract_id"]:
        raise LedgerTransitionError(
            f"claims.contract_id {claims.get('contract_id')!r} does not match ledger contract_id {ledger['contract_id']!r}"
        )

    chapter_id = claims.get("chapter_id")
    if chapter_id != gate_result.get("chapter_id"):
        raise LedgerTransitionError("claims.chapter_id does not match gate_result.chapter_id")
    if chapter_id not in ledger["chapter_numbers"]:
        raise LedgerTransitionError(f"chapter_id {chapter_id!r} is not a known chapter of this contract")
    if claims.get("chapter_number") != ledger["chapter_numbers"][chapter_id]:
        raise LedgerTransitionError("claims.chapter_number does not match the contract's chapter_number for this chapter_id")

    content_hash = claims.get("content_hash")
    if content_hash != gate_result.get("content_hash"):
        raise LedgerTransitionError("claims.content_hash does not match gate_result.content_hash")

    try:
        claims_hash = sha256_hex(canonical_json(claims))
    except (TypeError, ValueError) as exc:
        raise LedgerTransitionError(f"claims is not canonical JSON (e.g. contains a non-finite number): {exc}") from exc
    if claims_hash != gate_result.get("claims_hash"):
        raise LedgerTransitionError(
            "claims no longer match the exact mapping the gate accepted (claims_hash mismatch) -- "
            "any mutation to claims after a passing gate invalidates the commit"
        )

    # The COMPLETE current ledger must still be exactly what the gate validated -- any change to
    # the ledger since the gate ran changes its canonical hash and must block the commit. The
    # comparison strips THIS chapter's own commit record first (a legitimate idempotent recommit
    # necessarily runs against a ledger that already contains this exact commit), but the stored
    # record itself is separately compared in full below so stripping can never hide a change to it.
    comparison_ledger = deep_freeze_copy(ledger)
    comparison_ledger["commits"].pop(chapter_id, None)
    comparison_ledger["completed_prefix"] = _completed_prefix(comparison_ledger["commits"])
    if sha256_hex(canonical_json(comparison_ledger)) != gate_result.get("ledger_hash"):
        raise LedgerTransitionError(
            "the ledger has changed since the gate ran (ledger_hash mismatch) -- "
            "re-run the gate against the current ledger before committing"
        )

    new_state = deep_freeze_copy(ledger)
    new_record = {
        "content_hash": content_hash,
        "chapter_number": claims.get("chapter_number"),
        "claims": deep_freeze_copy(claims),
        "claims_hash": claims_hash,
    }
    existing = new_state["commits"].get(chapter_id)
    if existing is not None:
        if existing != new_record:
            raise LedgerTransitionError(
                f"chapter {chapter_id!r} already committed with a different record; "
                "call invalidate_from_chapter before recommitting"
            )
        return new_state  # idempotent recommit: the complete existing record already matches exactly
    new_state["commits"][chapter_id] = new_record
    new_state["completed_prefix"] = _completed_prefix(new_state["commits"])
    return new_state


def invalidate_from_chapter(ledger: dict, chapter_id: str) -> dict:
    structure_errors = ledger_structure_errors(ledger)
    if structure_errors:
        raise LedgerTransitionError("; ".join(structure_errors))

    new_state = deep_freeze_copy(ledger)
    cutoff = new_state["chapter_numbers"].get(chapter_id)
    if cutoff is None:
        return new_state  # chapter_id is not part of this contract at all -- nothing to invalidate
    new_state["commits"] = {
        cid: record for cid, record in new_state["commits"].items() if record["chapter_number"] < cutoff
    }
    new_state["completed_prefix"] = _completed_prefix(new_state["commits"])
    return new_state
