"""Continuity Core CC-01 -- shared exception types and canonicalization helpers.

This module has zero dependency on any other ``continuity`` submodule and zero dependency
on the rest of the Narasi codebase. It is imported by every other file in this package.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


class ContractValidationError(Exception):
    """Raised when Bible V2 input, a chapter-brief request, or a repair-plan input fails
    schema, referential, or semantic validation. Never returned as a partial object or an
    empty fallback -- the caller must handle the raised exception."""


class LedgerTransitionError(Exception):
    """Raised when a ledger commit or invalidation would violate the ledger's binding or
    transition rules (wrong contract binding, non-passing gate, conflicting content hash for
    an already-committed chapter, etc.)."""


class RepairValidationError(Exception):
    """Raised when repair-plan construction or candidate validation is given structurally
    malformed input (e.g. a chapters/gate_results/briefs mapping missing an entry for a
    chapter the other mappings reference). A *rejected* repair candidate is reported through
    the normal accepted=False / violations=[...] result shape, not through this exception."""


def canonical_json(payload: Any) -> str:
    """UTF-8, ``ensure_ascii=False``, sorted keys, compact separators, no whitespace, no
    trailing newline -- the one canonicalization rule used everywhere in this package.
    ``allow_nan=False`` so NaN/Infinity/-Infinity raise ``ValueError`` instead of silently
    serializing as the non-standard JSON tokens Python's json module otherwise emits by
    default -- no artifact in this package may ever hash a non-finite number."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def deep_freeze_copy(value: Any) -> Any:
    """A plain, dependency-free deep copy used at every public-function input/output boundary
    so callers' mappings are never mutated and returned structures are never aliased back to
    caller-owned containers. Handles the JSON-shaped value space (dict/list/str/int/float/bool/
    None) that every public function in this package operates on."""
    if isinstance(value, dict):
        return {k: deep_freeze_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [deep_freeze_copy(v) for v in value]
    if isinstance(value, tuple):
        return [deep_freeze_copy(v) for v in value]
    return value
