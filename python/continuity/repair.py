"""Continuity Core CC-01 -- changed-only targeted Narasi Review repair planning/validation.

This package only builds and validates repair payloads; it never calls the existing revise
model itself. A repair plan includes only ``decision == "repair"`` chapters -- never a clean
chapter and never a full book.
"""
from __future__ import annotations

import re
from typing import Any

from .models import RepairValidationError, canonical_json, deep_freeze_copy, sha256_hex

_MAX_EVIDENCE = 500
_MAX_ATTEMPTS = 2
_PRESERVE_HEADING = True
_WORD_BAND_MIN_RATIO = 0.75
_WORD_BAND_MAX_RATIO = 1.35
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_POST_IDENTITY_FIELDS = (
    "story_id", "contract_id", "contract_hash", "chapter_id", "chapter_number",
    "brief_hash", "content_hash", "claims_hash",
)
_POST_HASH_FIELDS = frozenset({"contract_hash", "brief_hash", "content_hash", "claims_hash"})
_FIXED_WORD_BAND = {"min_ratio": _WORD_BAND_MIN_RATIO, "max_ratio": _WORD_BAND_MAX_RATIO}


def _evidence(text: Any) -> str:
    return str(text)[:_MAX_EVIDENCE]


def _violation(code: str, chapter_id: str, evidence: Any, expected: Any, actual: Any) -> dict:
    return {
        "code": code, "severity": "blocker", "chapter_id": chapter_id,
        "evidence": _evidence(evidence), "expected": expected, "actual": actual,
    }


def _sort_violations(violations: list) -> list:
    return sorted(violations, key=lambda v: (v["code"], v["chapter_id"], v["evidence"]))


def _is_exact_int(value: Any) -> bool:
    """True int, never bool -- plain ``==``/``!=`` treats ``True == 1`` and ``1.0 == 1``, so every
    integer-typed identity/policy field here must be checked with this helper first."""
    return isinstance(value, int) and not isinstance(value, bool)


def _self_hash(payload: dict, exclude_key: str) -> "str | None":
    """None (never a matching hash) for a payload that isn't even canonically serializable (e.g.
    contains a non-finite float) -- callers compare this against a stored hash, so None safely
    reads as a mismatch instead of letting json.dumps's ValueError escape as a raw exception."""
    try:
        return sha256_hex(canonical_json({k: v for k, v in payload.items() if k != exclude_key}))
    except (TypeError, ValueError):
        return None


def build_targeted_repair_plan(chapters: dict, gate_results: dict, briefs: dict) -> dict:
    items = []
    for chapter_id in sorted(gate_results):
        gate = gate_results[chapter_id]
        if gate.get("decision") != "repair":
            continue
        if chapter_id not in chapters or chapter_id not in briefs:
            raise RepairValidationError(
                f"chapter {chapter_id!r} has a 'repair' gate result but is missing from chapters/briefs"
            )
        text = chapters[chapter_id]
        brief = briefs[chapter_id]
        if gate.get("chapter_id") != chapter_id or brief.get("chapter_id") != chapter_id:
            raise RepairValidationError(f"chapter_id {chapter_id!r} is not consistently bound across gate/brief")
        if gate.get("story_id") != brief.get("story_id"):
            raise RepairValidationError(f"gate_result.story_id does not match brief.story_id for {chapter_id!r}")
        if gate.get("contract_id") != brief.get("contract_id"):
            raise RepairValidationError(f"gate_result.contract_id does not match brief.contract_id for {chapter_id!r}")
        if gate.get("contract_hash") != brief.get("contract_hash"):
            raise RepairValidationError(f"gate_result.contract_hash does not match brief.contract_hash for {chapter_id!r}")
        if gate.get("brief_hash") != brief.get("brief_hash"):
            raise RepairValidationError(
                f"gate_result.brief_hash does not match the embedded brief's own hash for {chapter_id!r} "
                "-- the brief was changed after the gate ran"
            )
        if gate.get("content_hash") != sha256_hex(text):
            raise RepairValidationError(f"gate_result.content_hash does not match the original text hash for {chapter_id!r}")
        item = {
            "chapter_id": chapter_id,
            "original_content_hash": sha256_hex(text),
            "target_language": brief["target_language"],
            "original_text": text,
            "brief": deep_freeze_copy(brief),
            "violations": deep_freeze_copy(gate.get("violations", [])),
            "max_attempts": _MAX_ATTEMPTS,
            "preserve_heading": _PRESERVE_HEADING,
            "word_band": {"min_ratio": _WORD_BAND_MIN_RATIO, "max_ratio": _WORD_BAND_MAX_RATIO},
        }
        item["item_hash"] = _self_hash(item, "item_hash")
        items.append(item)
    return {"items": deep_freeze_copy(items), "whole_book_rewrite": False}


def validate_repair_candidate(original_text: str, candidate_text: str, repair_item: dict, post_gate_result: dict) -> dict:
    if not isinstance(repair_item, dict):
        return {
            "accepted": False, "decision": "reject", "chapter_id": None,
            "violations": [_violation("REPAIR_BINDING_MISMATCH", None, "repair_item is not an object", "an object", repair_item)],
        }
    chapter_id = repair_item.get("chapter_id")
    brief = repair_item.get("brief")
    brief = brief if isinstance(brief, dict) else {}
    post = post_gate_result if isinstance(post_gate_result, dict) else {}
    violations: list = []
    if not isinstance(post_gate_result, dict):
        violations.append(_violation(
            "REPAIR_BINDING_MISMATCH", chapter_id, "post_gate_result is not an object", "an object", post_gate_result,
        ))

    # ---- repair item integrity: item_hash and the embedded brief's brief_hash are MANDATORY,
    # not merely checked when present -- their absence is itself a binding failure. When present,
    # the item (including its embedded brief, violations, and every fixed constant) must be
    # exactly what plan construction produced, checked via its own build-time self-hash so any
    # tampering anywhere in the item is caught uniformly. ----
    if not isinstance(repair_item, dict) or "item_hash" not in repair_item:
        violations.append(_violation(
            "REPAIR_BINDING_MISMATCH", chapter_id, "repair_item.item_hash is missing", "present", None,
        ))
    elif _self_hash(repair_item, "item_hash") != repair_item.get("item_hash"):
        violations.append(_violation(
            "REPAIR_BINDING_MISMATCH", chapter_id, "repair item is not self-consistent",
            repair_item.get("item_hash"), _self_hash(repair_item, "item_hash"),
        ))
    if not isinstance(brief, dict) or "brief_hash" not in brief:
        violations.append(_violation(
            "REPAIR_BINDING_MISMATCH", chapter_id, "repair_item.brief.brief_hash is missing", "present", None,
        ))
    elif _self_hash(brief, "brief_hash") != brief.get("brief_hash"):
        violations.append(_violation(
            "REPAIR_BINDING_MISMATCH", chapter_id, "embedded brief is not self-consistent",
            brief.get("brief_hash"), _self_hash(brief, "brief_hash"),
        ))

    # ---- original text is immutably fixed at plan-build time: both the caller-supplied
    # original_text parameter AND the item's own embedded original_text must hash to the item's
    # embedded original_content_hash -- independent checks, since a caller could tamper with
    # either one without touching the other. ----
    if sha256_hex(original_text) != repair_item.get("original_content_hash"):
        violations.append(_violation(
            "REPAIR_BINDING_MISMATCH", chapter_id, sha256_hex(original_text),
            repair_item.get("original_content_hash"), sha256_hex(original_text),
        ))
    embedded_original = repair_item.get("original_text")
    if not isinstance(embedded_original, str) or sha256_hex(embedded_original) != repair_item.get("original_content_hash"):
        violations.append(_violation(
            "REPAIR_BINDING_MISMATCH", chapter_id, "repair_item.original_text is not self-consistent",
            repair_item.get("original_content_hash"),
            sha256_hex(embedded_original) if isinstance(embedded_original, str) else None,
        ))

    # ---- fixed repair policy is enforced from these code-owned constants, independent of
    # whatever repair_item/item_hash currently say -- a caller who changes a policy field and
    # then recomputes item_hash over the changed item must still be rejected. ----
    if not _is_exact_int(repair_item.get("max_attempts")) or repair_item.get("max_attempts") != _MAX_ATTEMPTS:
        violations.append(_violation(
            "REPAIR_BINDING_MISMATCH", chapter_id, str(repair_item.get("max_attempts")), _MAX_ATTEMPTS, repair_item.get("max_attempts"),
        ))
    if repair_item.get("preserve_heading") is not True:
        violations.append(_violation(
            "REPAIR_BINDING_MISMATCH", chapter_id, str(repair_item.get("preserve_heading")), _PRESERVE_HEADING, repair_item.get("preserve_heading"),
        ))
    if repair_item.get("word_band") != _FIXED_WORD_BAND:
        violations.append(_violation(
            "REPAIR_BINDING_MISMATCH", chapter_id, str(repair_item.get("word_band")), _FIXED_WORD_BAND, repair_item.get("word_band"),
        ))
    if repair_item.get("target_language") != brief.get("target_language"):
        violations.append(_violation(
            "REPAIR_BINDING_MISMATCH", chapter_id, str(repair_item.get("target_language")),
            brief.get("target_language"), repair_item.get("target_language"),
        ))

    # ---- post-gate identity/hash fields are always mandatory -- the only valid repair success
    # path is a complete post-generation gate result; a minimal stub is never accepted. ----
    candidate_hash = sha256_hex(candidate_text)
    for key in _POST_IDENTITY_FIELDS:
        if key not in post:
            violations.append(_violation(
                "REPAIR_BINDING_MISMATCH", chapter_id, f"post.{key} is missing", "present", None,
            ))
        elif key in _POST_HASH_FIELDS and (not isinstance(post[key], str) or not _HEX64_RE.fullmatch(post[key])):
            violations.append(_violation(
                "REPAIR_BINDING_MISMATCH", chapter_id, f"post.{key}={post[key]!r}", "a lowercase 64-hex string", post[key],
            ))
    if "chapter_id" in post and post["chapter_id"] != chapter_id:
        violations.append(_violation("REPAIR_BINDING_MISMATCH", chapter_id, str(post["chapter_id"]), chapter_id, post["chapter_id"]))
    if "story_id" in post and post["story_id"] != brief.get("story_id"):
        violations.append(_violation("REPAIR_BINDING_MISMATCH", chapter_id, str(post["story_id"]), brief.get("story_id"), post["story_id"]))
    if "contract_id" in post and post["contract_id"] != brief.get("contract_id"):
        violations.append(_violation("REPAIR_BINDING_MISMATCH", chapter_id, str(post["contract_id"]), brief.get("contract_id"), post["contract_id"]))
    if "contract_hash" in post and post["contract_hash"] != brief.get("contract_hash"):
        violations.append(_violation("REPAIR_BINDING_MISMATCH", chapter_id, str(post["contract_hash"]), brief.get("contract_hash"), post["contract_hash"]))
    if "chapter_number" in post and not _is_exact_int(post["chapter_number"]):
        violations.append(_violation(
            "REPAIR_BINDING_MISMATCH", chapter_id, str(post["chapter_number"]), "an integer, never bool/float", post["chapter_number"],
        ))
    elif "chapter_number" in post and post["chapter_number"] != brief.get("chapter_number"):
        violations.append(_violation("REPAIR_BINDING_MISMATCH", chapter_id, str(post["chapter_number"]), brief.get("chapter_number"), post["chapter_number"]))
    if "brief_hash" in post and post["brief_hash"] != brief.get("brief_hash"):
        violations.append(_violation("REPAIR_BINDING_MISMATCH", chapter_id, str(post["brief_hash"]), brief.get("brief_hash"), post["brief_hash"]))
    if "content_hash" in post and post["content_hash"] != candidate_hash:
        violations.append(_violation("REPAIR_BINDING_MISMATCH", chapter_id, str(post["content_hash"]), candidate_hash, post["content_hash"]))

    orig_lines = original_text.splitlines()
    cand_lines = candidate_text.splitlines()
    orig_heading = orig_lines[0] if orig_lines else ""
    cand_heading = cand_lines[0] if cand_lines else ""
    if _PRESERVE_HEADING and cand_heading != orig_heading:
        violations.append(_violation("HEADING_CHANGED", chapter_id, cand_heading, orig_heading, cand_heading))

    # Word band is always the fixed contract constant -- a tampered/widened repair_item["word_band"]
    # (e.g. a caller-supplied {0.0, 100.0}) must never be able to widen acceptance, regardless of
    # whether it was also caught above by the code-owned policy check.
    orig_words = len(original_text.split())
    cand_words = len(candidate_text.split())
    lo = orig_words * _WORD_BAND_MIN_RATIO
    hi = orig_words * _WORD_BAND_MAX_RATIO
    if not (lo <= cand_words <= hi):
        violations.append(_violation(
            "WORD_BAND_VIOLATION", chapter_id, f"{cand_words} words",
            f"[{lo:.2f}, {hi:.2f}] words (original={orig_words})", cand_words,
        ))

    # A clean pass requires post.violations to be EXACTLY an empty list -- not merely falsy.
    # Coalescing via `or []` would wrongly treat None/{}/""/()/False as "clean"; only a real,
    # literal empty list is.
    post_violations = post.get("violations")
    if post.get("ok") is not True or post.get("decision") != "pass" or post.get("coverage") != "complete" or post_violations != []:
        violations.append(_violation(
            "POST_GATE_NOT_CLEAN", chapter_id, str(post.get("decision")),
            "ok=true, decision=pass, coverage=complete, and an entirely empty violations list",
            {"ok": post.get("ok"), "decision": post.get("decision"), "coverage": post.get("coverage"), "violations": post_violations},
        ))

    violations = _sort_violations(violations)
    return {
        "accepted": not violations,
        "decision": "reject" if violations else "pass",
        "chapter_id": chapter_id,
        "violations": violations,
    }
