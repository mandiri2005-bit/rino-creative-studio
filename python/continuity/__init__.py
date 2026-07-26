"""Narasi Continuity Core CC-01 -- deterministic continuity nucleus.

Offline, code-only package: Bible V2 validation, deterministic Story Contract v3 compilation,
Chapter Brief v1 slicing, an immutable in-memory Continuity Ledger v1, pre/post generation
gates, and changed-only targeted Narasi Review repair planning/validation. Zero LLM, provider,
network, Railway, or database dependency. Not wired into the live Narasi pipeline by this
package -- see Section 8.31 of the continuity execution plan.
"""
from .models import ContractValidationError, LedgerTransitionError, RepairValidationError
from .schema import validate_bible_v2
from .compiler import canonical_contract_json, compile_story_contract_v3, contract_hash
from .slicer import canonical_brief_json, compile_chapter_brief
from .ledger import commit_chapter, invalidate_from_chapter, new_ledger
from .gates import post_generation_gate, pre_generation_gate
from .repair import build_targeted_repair_plan, validate_repair_candidate

__all__ = [
    "ContractValidationError",
    "LedgerTransitionError",
    "RepairValidationError",
    "validate_bible_v2",
    "compile_story_contract_v3",
    "canonical_contract_json",
    "contract_hash",
    "new_ledger",
    "compile_chapter_brief",
    "canonical_brief_json",
    "pre_generation_gate",
    "post_generation_gate",
    "commit_chapter",
    "invalidate_from_chapter",
    "build_targeted_repair_plan",
    "validate_repair_candidate",
]
