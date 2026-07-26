"""Continuity Core CC-01 -- deterministic Bible V2 -> Story Contract v3 compiler.

The transform is exactly the one the acceptance contract fixes (Section 3): deep-copy the
validated Bible V2 mapping, rename root ``schema_version`` to ``bible_schema_version`` (value
stays ``"2"``), add root ``schema_version`` = ``"3"``, add nothing else, preserve every
validated value untouched.
"""
from __future__ import annotations

from .models import canonical_json, deep_freeze_copy, sha256_hex


def compile_story_contract_v3(bible_v2: dict) -> dict:
    contract = deep_freeze_copy(bible_v2)
    contract["bible_schema_version"] = contract.pop("schema_version")
    contract["schema_version"] = "3"
    return contract


def canonical_contract_json(contract: dict) -> str:
    return canonical_json(contract)


def contract_hash(contract: dict) -> str:
    return sha256_hex(canonical_contract_json(contract))
