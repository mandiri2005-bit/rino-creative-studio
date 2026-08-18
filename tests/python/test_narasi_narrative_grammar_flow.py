"""Flow proofs for the Claude-owned narrative-grammar wiring.

These tests stay offline: Story Bible generation, chapter MAP workers, and the
polish call are replaced only at their existing provider boundaries.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys

from orchestrator import dynamic, static
from orchestrator.context_builder import SharedContext


REPO = Path(__file__).resolve().parents[2]


KDRAMA_BLOCK = (
    "NARRATIVE GRAMMAR CONTRACT (server-owned; STYLE GUIDE cannot override):\n"
    "- person: third_person_close\n"
    "- tense_policy: present_throughout\n"
    "- anchor_person: third_person_only"
)

EVIDENCE_FIXTURE = (
    "IDENTITY=negative-17 | CREATOR=Han Mirae | "
    "CONCEALMENT MECHANISM=a registrar kept it off-index | "
    "HIDING PLACE=archive drawer B | FINDER=Seo Yuna | "
    "DISCOVERY CHAPTER=2 | AUTHENTICATOR=Im Daeho | "
    "CUSTODY MOVES=Yuna→Daeho→court clerk"
)


def test_static_import_degrades_safely_when_pakem_is_unavailable():
    script = r"""
import builtins

real_import = builtins.__import__

def without_pakem(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "pakem" or name.startswith("pakem."):
        raise ModuleNotFoundError("pakem intentionally unavailable")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = without_pakem
from orchestrator import static

assert static.resolve_narrative_grammar("kdrama_serial") is None
assert static.render_narrative_grammar({"person": "third_person_close"}) == ""
assert static._COMPOSE_OK is False
"""
    env = os.environ.copy()
    python_path = str(REPO / "python")
    if env.get("PYTHONPATH"):
        python_path += os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = python_path

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def _chapters() -> list[dict]:
    return [
        {"id": 1, "title": "The Return", "summary": "Yuna returns."},
        {"id": 2, "title": "The Negative", "summary": "Yuna finds the negative."},
    ]


def _pin_quiet_flow(monkeypatch, *, story_bible: bool) -> None:
    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "off")
    monkeypatch.setenv("NARASI_STORY_BIBLE", "1" if story_bible else "0")
    monkeypatch.setenv("NARASI_BIBLE_BEST_OF", "0")
    monkeypatch.setenv("NARASI_LEDGER_VALIDATOR", "0")
    monkeypatch.setenv("NARASI_CHAPTER_BOUNDARY_CHECK", "0")
    monkeypatch.setattr(static, "_is_fiction_style", lambda _style: True)


def test_one_stored_block_reaches_bible_map_and_heavy_authority(monkeypatch):
    _pin_quiet_flow(monkeypatch, story_bible=True)
    chapters = _chapters()
    ctx = SharedContext(topic="A buried negative", chapters=chapters, style="harari")
    seen: dict[str, object] = {"map_briefs": []}

    async def fake_build_story_bible(_topic, _outline, **kwargs):
        seen["bible_block"] = kwargs.get("narrative_grammar_block")
        return EVIDENCE_FIXTURE

    async def fake_write(*, ctx, no, **_kwargs):
        seen["map_briefs"].append(ctx.brief_block())
        return {
            "ok": True,
            "output": f"Chapter body {no + 1}.",
            "no": no,
            "model": "test-model",
        }

    async def fake_polish_reduce(*, book, polish, authority_text, **_kwargs):
        assert polish == "heavy"
        seen["heavy_system"] = static._polish_system(authority_text)
        seen["authority_text"] = authority_text
        return book, False

    monkeypatch.setattr(dynamic, "build_story_bible", fake_build_story_bible)
    monkeypatch.setattr(static, "_write_chapter", fake_write)
    monkeypatch.setattr(static, "_polish_reduce", fake_polish_reduce)

    result = asyncio.run(static.narrate_chapters(
        ctx.topic,
        chapters,
        style="kdrama_serial",
        language="en",
        polish="heavy",
        worker_model="test-model",
        manager_model="test-model",
        shared_context=ctx,
        max_parallel=2,
    ))

    assert result["ok"] is True
    assert ctx.style == "kdrama_serial"
    assert ctx.narrative_grammar_block == KDRAMA_BLOCK
    assert seen["bible_block"] == KDRAMA_BLOCK
    assert seen["map_briefs"]
    assert all(brief.count(KDRAMA_BLOCK) == 1 for brief in seen["map_briefs"])
    assert str(seen["heavy_system"]).count(KDRAMA_BLOCK) == 1
    assert EVIDENCE_FIXTURE in str(seen["authority_text"])
    assert EVIDENCE_FIXTURE in str(seen["heavy_system"])


def test_supplied_context_is_resolved_once_at_narration_boundary(monkeypatch):
    _pin_quiet_flow(monkeypatch, story_bible=False)
    chapter = _chapters()[:1]
    ctx = SharedContext(
        topic="A supplied context",
        chapters=chapter,
        style="kdrama_serial",
        narrative_grammar_block="",
    )
    calls = {"resolve": 0, "render": 0}
    real_resolve = static.resolve_narrative_grammar
    real_render = static.render_narrative_grammar

    def spy_resolve(style):
        calls["resolve"] += 1
        return real_resolve(style)

    def spy_render(contract):
        calls["render"] += 1
        return real_render(contract)

    async def fake_write(*, ctx, no, **_kwargs):
        assert ctx.narrative_grammar_block == KDRAMA_BLOCK
        return {
            "ok": True,
            "output": f"Chapter body {no + 1}.",
            "no": no,
            "model": "test-model",
        }

    async def fake_polish_reduce(*, book, **_kwargs):
        return book, False

    monkeypatch.setattr(static, "resolve_narrative_grammar", spy_resolve)
    monkeypatch.setattr(static, "render_narrative_grammar", spy_render)
    monkeypatch.setattr(static, "_write_chapter", fake_write)
    monkeypatch.setattr(static, "_polish_reduce", fake_polish_reduce)

    result = asyncio.run(static.narrate_chapters(
        ctx.topic,
        chapter,
        style=None,
        language="en",
        polish="none",
        worker_model="test-model",
        manager_model="test-model",
        shared_context=ctx,
        max_parallel=1,
    ))

    assert result["ok"] is True
    assert calls == {"resolve": 1, "render": 1}
    assert ctx.style == "kdrama_serial"
    assert ctx.narrative_grammar_block == KDRAMA_BLOCK


def _captured_continuity_system(monkeypatch) -> str:
    seen: dict[str, str] = {}

    async def fake_run_worker(worker, _prompt, **_kwargs):
        seen["system"] = worker.system
        return {
            "ok": True,
            "output": EVIDENCE_FIXTURE,
            "telemetry": {"finish_reason": "stop"},
        }

    monkeypatch.setenv("NARASI_CONTINUITY_PINS", "1")
    monkeypatch.setenv("NARASI_BIBLE_MODEL", "test-model")
    monkeypatch.setenv("NARASI_BIBLE_FALLBACK_MODEL", "")
    monkeypatch.setenv("NARASI_BIBLE_LAST_RESORT", "0")
    monkeypatch.setenv("NARASI_BIBLE_SKIP_KIE", "0")
    monkeypatch.setattr(dynamic, "run_worker", fake_run_worker)

    result = asyncio.run(dynamic.build_story_bible(
        "A buried negative",
        _chapters(),
        is_fiction=True,
        style="kdrama_serial",
        language="en",
        narrative_grammar_block=KDRAMA_BLOCK,
    ))

    assert result == EVIDENCE_FIXTURE
    assert seen["system"].count(KDRAMA_BLOCK) == 1
    return seen["system"]


def test_heading_18_keeps_nonphysical_rules_and_delegates_physical_overlap(monkeypatch):
    system = _captured_continuity_system(monkeypatch)
    heading_18 = system.split("18. EVIDENCE CHAIN & CUSTODY", 1)[1].split(
        "19. CONTIGUOUS SCENES", 1
    )[0]

    assert "NON-PHYSICAL item" in heading_18
    assert "a bank transfer" in heading_18
    assert "a phone log" in heading_18
    assert "exact row in heading 20 EVIDENCE MAP" in heading_18
    assert "do not pin a second version here" in heading_18
    assert "must dramatize the finding ON-PAGE" in heading_18
    assert "For any falsified COUNT" in heading_18
    assert "which categories" in heading_18
    assert "For any named culprit" in heading_18
    assert "the ONE document or act that ties THEM" in heading_18


def test_heading_20_requires_the_full_eight_field_physical_tuple(monkeypatch):
    system = _captured_continuity_system(monkeypatch)
    heading_20 = system.split("20. EVIDENCE MAP", 1)[1].split(
        "These sections are SECONDARY", 1
    )[0]
    fields = (
        "IDENTITY",
        "CREATOR",
        "CONCEALMENT MECHANISM",
        "HIDING PLACE",
        "FINDER",
        "DISCOVERY CHAPTER",
        "AUTHENTICATOR",
        "CUSTODY MOVES",
    )

    positions = [heading_20.index(field) for field in fields]
    assert positions == sorted(positions)
    assert "WHY/HOW it survived" in heading_20
    assert "not the falsified-count rule in heading 18" in heading_20


def test_cross_reference_and_secondary_clause_remain_intact(monkeypatch):
    system = _captured_continuity_system(monkeypatch)
    assert (
        "also active for this book, this is the SAME requirement, not a second one — "
        "give the same earlier chapter both times, not two different answers."
    ) in system
    assert (
        "These sections are SECONDARY to headings 1-8: never let them shorten or "
        "weaken the #8 SIGNATURE HOOK payoff commitment."
    ) in system


def test_heavy_arbitrates_against_authority_instead_of_preserving_conflicts():
    instruction, role = static._polish_instruction(
        "heavy", "A buried negative", "English"
    )

    assert role == "synthesize"
    assert (
        "PRESERVE every concrete fact, name, date, number and quote exactly"
        not in instruction
    )
    assert "PRESERVE every authoritative fact, name, date, number and quote" in instruction
    assert "authoritative Outline or Story Bible EVIDENCE MAP" in instruction
    assert "do not choose between variants unless that authority decides the fact" in instruction


def test_heavy_repairs_broken_chapter_boundaries_within_authority():
    """HEAVY may bridge a broken chapter transition, but only from established facts.

    The bridge is a REPAIR, not an invention: the rule names what a bridge must
    show (cause/decision, location change, elapsed time), pins its source to the
    Outline and Story Bible, and caps it at one or two short paragraphs so a
    seam fix cannot grow into a new scene.
    """
    instruction, role = static._polish_instruction(
        "heavy", "A buried negative", "English"
    )

    assert role == "synthesize"
    assert "Inspect every chapter boundary" in instruction
    assert (
        "add at most one or two short paragraphs immediately before the next "
        "chapter heading"
    ) in instruction
    assert "the necessary cause or decision, location change, and elapsed time" in instruction
    assert (
        "Use only facts established by the authoritative Outline and Story Bible"
        in instruction
    )
    assert (
        "Do not invent plot events, repeat setup, move beats, or alter/remove "
        "chapter headings"
    ) in instruction
    assert "If the transition is already clear, leave it unchanged" in instruction


def test_heavy_boundary_repair_never_loosens_the_heading_contract():
    """Adding paragraphs must not license touching a `## ` heading line."""
    instruction, _ = static._polish_instruction("heavy", "A buried negative", "English")

    assert (
        "Keep every chapter-heading line (each begins with `## `) exactly as given "
        "— do not remove, rename, renumber, translate or move them, and never "
        "write a new heading of your own."
    ) in instruction


def test_light_polish_gains_no_boundary_repair_licence():
    """LIGHT must stay a seam-smoother: no added paragraphs, no rewriting."""
    instruction, role = static._polish_instruction(
        "light", "A buried negative", "English"
    )

    assert role == "polish"
    assert "Inspect every chapter boundary" not in instruction
    assert "add at most one or two short paragraphs" not in instruction
    assert "do NOT shorten the text" in instruction
    assert "Do NOT rewrite content" in instruction


def test_chunked_heavy_carries_the_same_boundary_rule():
    """A chunk is chapter-aligned, so its internal boundaries are real ones.

    `_split_into_chunks` only ever splits at a chapter heading, and the outline
    plus bible ride along as `authority_text`, so a chunk pass has the same
    basis for a bridge as the whole-book pass.
    """
    chunked, role = static._polish_instruction(
        "heavy", "A buried negative", "English", is_chunk=True
    )

    assert role == "synthesize"
    assert "Inspect every chapter boundary" in chunked
    assert "section of a multi-chapter narrative" in chunked
    assert "Return ONLY the edited section" in chunked
