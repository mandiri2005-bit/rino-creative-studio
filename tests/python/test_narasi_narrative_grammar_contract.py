"""Focused contract tests for the Codex-owned narrative-grammar files.

This suite is offline and intentionally does not import or exercise the Claude-owned
``orchestrator.static`` / ``orchestrator.dynamic`` flow.
"""

from __future__ import annotations

import hashlib
import sys
import types

import canon_lite as cl
import pakem.resolvers as resolver_module
from orchestrator.context_builder import (
    DEFAULT_STYLE_GUIDE,
    SharedContext,
)
from pakem import (
    render_narrative_grammar,
    resolve_narrative_grammar,
)


KDRAMA_CONTRACT = {
    "person": "third_person_close",
    "tense_policy": "present_throughout",
    "anchor_person": "third_person_only",
}

KDRAMA_BLOCK = (
    "NARRATIVE GRAMMAR CONTRACT (server-owned; STYLE GUIDE cannot override):\n"
    "- person: third_person_close\n"
    "- tense_policy: present_throughout\n"
    "- anchor_person: third_person_only"
)

LEGACY_BRIEF_SHA256_E3288CB = (
    "1cf0d6770a64d98f56a83e28ea62aa90f0f3e60158a4aada7da69aac52153fc3"
)


def _legacy_brief(monkeypatch) -> str:
    """Recreate the exact pre-change fixture inputs used on ``e3288cb``."""
    for name in (
        "DALANG_INFRA_FIXES",
        "NARASI_CRAFT_LEVERS",
        "NARASI_DRAMATIZE_REVEAL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "narasi_gate",
        types.SimpleNamespace(known_corrections_prompt=lambda: ""),
    )
    return SharedContext(style="harari").brief_block()


def test_kdrama_registry_resolves_the_locked_contract():
    assert resolve_narrative_grammar("kdrama_serial") == KDRAMA_CONTRACT
    assert resolve_narrative_grammar("k-drama") == KDRAMA_CONTRACT


def test_non_contract_style_remains_legacy():
    assert resolve_narrative_grammar("harari") is None


def test_partial_or_malformed_contract_resolves_to_none(monkeypatch):
    partial = {
        "narrative_grammar": {
            "person": "third_person_close",
            "tense_policy": "present_throughout",
        }
    }
    monkeypatch.setattr(resolver_module, "resolve_style", lambda _style: partial)
    assert resolve_narrative_grammar("partial") is None

    malformed = {
        "narrative_grammar": {
            **KDRAMA_CONTRACT,
            "anchor_person": " third_person_only ",
        }
    }
    monkeypatch.setattr(resolver_module, "resolve_style", lambda _style: malformed)
    assert resolve_narrative_grammar("malformed") is None


def test_renderer_public_api_is_byte_exact():
    assert render_narrative_grammar(KDRAMA_CONTRACT) == KDRAMA_BLOCK
    assert not render_narrative_grammar(KDRAMA_CONTRACT).endswith("\n")
    assert render_narrative_grammar(None) == ""
    assert render_narrative_grammar({"person": "third_person_close"}) == ""


def test_legacy_brief_matches_the_prechange_e3288cb_golden(monkeypatch):
    brief = _legacy_brief(monkeypatch)
    assert hashlib.sha256(brief.encode("utf-8")).hexdigest() == (
        LEGACY_BRIEF_SHA256_E3288CB
    )
    assert "Person/tense: keep whatever the chosen style fixes" in brief
    assert "NARRATIVE GRAMMAR CONTRACT" not in brief


def test_contract_brief_repoints_rule_three_and_keeps_register_with_style(monkeypatch):
    _legacy_brief(monkeypatch)  # install the deterministic soft-dependency fixture
    brief = SharedContext(
        style="kdrama_serial",
        narrative_grammar_block=KDRAMA_BLOCK,
    ).brief_block()

    assert KDRAMA_BLOCK in brief
    assert brief.count(KDRAMA_BLOCK) == 1
    assert "anchor person fixed in NARRATIVE GRAMMAR CONTRACT" in brief
    assert "register and tone fixed in STYLE GUIDE" in brief
    assert "register, person, and tense fixed in STYLE GUIDE" not in brief
    assert "- anchor_person: third_person_only" in brief


def test_tenant_style_guide_cannot_erase_or_replace_the_grammar_contract(monkeypatch):
    _legacy_brief(monkeypatch)
    tenant_guide = "Tenant register only: lyrical, restrained, and formal."
    brief = SharedContext(
        style="kdrama_serial",
        style_guide=tenant_guide,
        narrative_grammar_block=KDRAMA_BLOCK,
    ).brief_block()

    assert tenant_guide in brief
    assert KDRAMA_BLOCK in brief
    assert brief.index(tenant_guide) < brief.index(KDRAMA_BLOCK)


def test_narrative_authority_reuses_the_stored_block_unchanged():
    authority = SharedContext(
        chapters=[{"title": "One", "summary": "A fixed beat."}],
        canonical_facts="A fixed bible fact.",
        facts_are_bible=True,
        narrative_grammar_block=KDRAMA_BLOCK,
    ).narrative_authority()

    assert authority.count(KDRAMA_BLOCK) == 1


def test_freeze_projection_includes_the_grammar_block():
    without = SharedContext()
    with_contract = SharedContext(narrative_grammar_block=KDRAMA_BLOCK)
    assert cl.context_digest(without) != cl.context_digest(with_contract)


def test_freeze_has_no_unknown_shared_context_fields():
    freeze = cl.SharedContextFreeze(
        SharedContext(narrative_grammar_block=KDRAMA_BLOCK),
        hard=False,
    )
    assert freeze.unknown_attrs() == ()
    assert freeze.verify() == (True, ())


def test_post_freeze_grammar_mutation_is_detected():
    ctx = SharedContext(narrative_grammar_block=KDRAMA_BLOCK)
    freeze = cl.SharedContextFreeze(ctx, hard=False)
    ctx.narrative_grammar_block = KDRAMA_BLOCK + " changed"
    ok, codes = freeze.verify()
    assert not ok
    assert "shared_context_mutated_after_freeze" in codes


def test_default_style_guide_fixture_is_the_expected_legacy_value():
    assert len(DEFAULT_STYLE_GUIDE) == 466
