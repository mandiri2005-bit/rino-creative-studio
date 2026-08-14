"""Contract witnesses for outline-authoritative parallel chapter prompts.

These tests are intentionally local and provider-free.  They bind the production prompt
shape that lets every MAP worker see the same whole-book outline while receiving a distinct
past/current/future assignment in its variable user turn.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from orchestrator import dynamic
from orchestrator import static
from orchestrator.context_builder import SharedContext
from pakem.assembler import compose


def _chapters(n: int = 20) -> list[dict]:
    return [
        {
            "id": str(i),
            "title": f"Title {i}",
            "summary": f"SYNOPSIS_{i}_EXACT: event reserved for chapter {i}.",
            "word_target": 2000,
        }
        for i in range(1, n + 1)
    ]


def _ctx(n: int = 20) -> SharedContext:
    return SharedContext(
        topic="contract witness",
        chapters=_chapters(n),
        canonical_facts="1. Character spelling stays fixed.",
        facts_are_bible=True,
        style="storytelling",
    )


def _compose_for(ctx: SharedContext, index: int, *, scope: str | None = None,
                 prev_tail: str = ""):
    chapter = ctx.chapters[index]
    return compose(
        style="storytelling",
        language="id",
        outline=ctx.outline(),
        brief=ctx.brief_block(),
        chapter={
            **chapter,
            "index": index,
            "total": len(ctx.chapters),
            "word_min": 1800,
            "word_max": 2600,
        },
        prev_tail=prev_tail,
        chapter_scope=ctx.scope_for(index) if scope is None else scope,
        job_id="outline-worker-contract",
    )


def test_every_worker_gets_all_twenty_exact_synopses_in_one_stable_prefix():
    ctx = _ctx()
    rendered = [_compose_for(ctx, i) for i in range(len(ctx.chapters))]

    assert len({item.static_prefix for item in rendered}) == 1
    for item in rendered:
        assert "AUTHORITATIVE FULL OUTLINE" in item.static_prefix
        for chapter in ctx.chapters:
            assert chapter["summary"] in item.static_prefix


def test_middle_worker_reads_past_current_and_future_but_owns_only_current():
    ctx = _ctx()
    user = _compose_for(ctx, 1).dynamic_block

    assert "CHAPTER CONTINUITY CONTRACT:" in user
    assert "PAST COMMITMENTS" in user
    assert 'Chapter 1: "Title 1"' in user
    assert 'CURRENT ASSIGNMENT — write ONLY chapter 2 of 20: "Title 2"' in user
    assert "FUTURE RESERVED" in user
    assert 'Chapter 3: "Title 3"' in user
    assert 'Chapter 20: "Title 20"' in user
    assert "SYNOPSIS_2_EXACT" in user
    assert "STORY SO FAR" not in user


def test_rooftop_worker_locks_intra_chapter_beat_order_and_visible_story_clock():
    chapters = [
        {
            "title": "A Blanket of Stars and Rust",
            "summary": "Tae-jun returns, accepts Eun-soo's ghost-designer offer.",
            "word_target": 650,
        },
        {
            "title": "The Price of Drafted Dreams",
            "summary": (
                "Their drafting exposes Tae-jun's identity. MIDPOINT: Eun-soo proves "
                "he stole her savings. THEN Min-jae threatens foreclosure. FINALLY they "
                "recognize the institutional system as the adversary."
            ),
            "word_target": 650,
        },
        {
            "title": "Unsheltered Under the Sky",
            "summary": "On day 30 they confront the board and the lease expires.",
            "word_target": 650,
        },
    ]
    ctx = SharedContext(
        topic="Kontrak Cinta 30 Hari di Rooftop Seoul",
        chapters=chapters,
        canonical_facts="The contract lasts exactly 30 days.",
        facts_are_bible=True,
        style="kdrama_serial",
    )
    user = _compose_for(ctx, 1).dynamic_block

    assert "BEAT SEQUENCE — execute the CURRENT ASSIGNMENT's beats in the exact order" in user
    assert "Do not move a later threat, reveal, decision, or resolution" in user
    assert 'ON-PAGE HANDOFF — begin from Chapter 1, "A Blanket of Stars and Rust"' in user
    assert "Never assume an off-page decision, journey, reconciliation, or large time jump" in user
    assert "STORY CLOCK — if the premise, title, contract, countdown, or deadline" in user
    assert "never declare it expired without accounting for the intervening time" in user
    assert user.index("MIDPOINT: Eun-soo proves") < user.index("THEN Min-jae threatens")


def test_assembler_keeps_order_and_clock_rules_when_no_shared_context_scope_exists():
    ctx = _ctx(3)
    user = _compose_for(ctx, 1, scope="").dynamic_block

    assert "CHAPTER CONTINUITY CONTRACT:" not in user
    assert "Beat order: execute the summary's beats in the exact written order" in user
    assert "Handoff: before the first new set-piece" in user
    assert "Story clock: if the book has a fixed duration or deadline" in user


def test_first_and_last_worker_have_correct_boundary_semantics():
    ctx = _ctx(3)
    first = _compose_for(ctx, 0).dynamic_block
    last = _compose_for(ctx, 2).dynamic_block

    assert "PAST COMMITMENTS — none; this chapter opens the book." in first
    assert 'CURRENT ASSIGNMENT — write ONLY chapter 1 of 3: "Title 1"' in first
    assert 'Chapter 3: "Title 3"' in first
    assert 'Chapter 1: "Title 1"' in last
    assert 'CURRENT ASSIGNMENT — write ONLY chapter 3 of 3: "Title 3"' in last
    assert "FUTURE RESERVED — none; this chapter closes the book." in last


def test_real_sequential_tail_and_parallel_scope_remain_separate():
    ctx = _ctx(3)
    item = _compose_for(ctx, 1, prev_tail="ACTUAL_PREVIOUS_NARRATION_SENTINEL")
    user = item.dynamic_block

    assert "STORY SO FAR (the immediately preceding narration" in user
    assert "ACTUAL_PREVIOUS_NARRATION_SENTINEL" in user
    assert "CHAPTER CONTINUITY CONTRACT:" in user
    assert user.index("STORY SO FAR") < user.index("CHAPTER CONTINUITY CONTRACT")
    assert "ACTUAL_PREVIOUS_NARRATION_SENTINEL" not in item.static_prefix


def test_chapter_scope_is_a_falsifiable_user_only_cache_isolation_witness():
    """Mutation witness: routing either sentinel into the system prefix breaks equality."""
    ctx = _ctx(2)
    left = _compose_for(ctx, 0, scope="SCOPE_SENTINEL_ALPHA")
    right = _compose_for(ctx, 0, scope="SCOPE_SENTINEL_BETA")

    assert left.static_prefix == right.static_prefix
    assert "SCOPE_SENTINEL_ALPHA" not in left.static_prefix
    assert "SCOPE_SENTINEL_BETA" not in right.static_prefix
    assert "SCOPE_SENTINEL_ALPHA" in left.dynamic_block
    assert "SCOPE_SENTINEL_BETA" in right.dynamic_block
    assert left.dynamic_block != right.dynamic_block


def test_outline_authority_is_pinned_at_bible_creation_and_worker_consumption():
    fiction = dynamic._STORY_BIBLE_SYSTEM_FICTION
    nonfiction = dynamic._STORY_BIBLE_SYSTEM_NONFICTION
    ctx = _ctx(2)
    system = _compose_for(ctx, 0).static_prefix

    assert "immutable, highest-authority story specification" in fiction
    assert "Never override, correct, improve, or redistribute the outline" in fiction
    assert "preserve the written order of its outlined sentences and beats" in fiction
    assert "story-clock marker for EVERY chapter" in fiction
    assert "DECIDE and FIX" not in fiction
    assert "Rules: DECIDE concrete values" not in fiction
    assert "OVERRIDES heading 4" not in fiction
    assert "outline always wins over this sheet" in nonfiction
    # Exact assembler-block witness. A broad substring check is vacuous because
    # COHERENCE_RULES also says "AUTHORITATIVE FULL OUTLINE"; this assertion must
    # fail if the actual outline label regresses to the old plain "FULL OUTLINE".
    outline_label = (
        "AUTHORITATIVE FULL OUTLINE (immutable story specification; if any other "
        "prompt block conflicts with it, this outline wins):\n"
    )
    assert outline_label in system
    assert system.rstrip().endswith(outline_label + ctx.outline())
    assert "which always wins any conflict" in system
    # The authoritative outline is deliberately the final static content block, after
    # the derived bible inside NARRATIVE BRIEF, so conflict precedence is also positional.
    assert system.index("STORY BIBLE") < system.index("AUTHORITATIVE FULL OUTLINE")


def test_production_map_wires_scope_without_forging_a_previous_tail(monkeypatch):
    ctx = _ctx(3)
    captured: dict = {}

    def fake_compose(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "user"},
            ],
            cache_key="cache",
        )

    async def fake_run_worker(*args, **kwargs):
        return {"ok": True, "output": ""}

    async def fake_word_gate(result, **kwargs):
        return result

    monkeypatch.setattr(static, "_COMPOSE_OK", True)
    monkeypatch.setattr(static, "compose", fake_compose)
    monkeypatch.setattr(static, "run_worker", fake_run_worker)
    monkeypatch.setattr(static, "_apply_word_gate", fake_word_gate)

    asyncio.run(static._write_chapter(
        ctx=ctx,
        ch=ctx.chapters[1],
        no=1,
        total=3,
        style="storytelling",
        language="id",
        mode="text",
        job_id="wiring-witness",
        worker_model="gemini-2.5-flash",
        timeout=20,
        telemetry_sink=None,
    ))

    assert captured["chapter_scope"] == ctx.scope_for(1)
    assert "prev_tail" not in captured
