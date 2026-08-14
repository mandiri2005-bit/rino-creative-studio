"""Contract witnesses for outline-authoritative parallel chapter prompts.

These tests are intentionally local and provider-free.  They bind the production prompt
shape that lets every MAP worker see the same whole-book outline while receiving a distinct
past/current/future assignment in its variable user turn.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from orchestrator import dynamic
from orchestrator import static
from orchestrator.context_builder import SharedContext
from narasi_outline_packet import (
    OUTLINE_PACKET_CONTRACT_VERSION,
    outline_packet_bundle,
    split_ordered_outline_beats,
)
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
    assert f"OUTLINE PACKET CONTRACT: {OUTLINE_PACKET_CONTRACT_VERSION}" in user
    assert "PREVIOUS OUTLINE COMMITMENT:" in user
    assert 'Chapter 1: "Title 1"' in user
    assert "SYNOPSIS_1_EXACT" in user
    assert "CURRENT ORDERED OUTLINE BEATS:" in user
    assert 'Chapter 2: "Title 2"' in user
    assert "NEXT RESERVED OUTLINE STATE:" in user
    assert 'Chapter 3: "Title 3"' in user
    assert "SYNOPSIS_3_EXACT" in user
    assert 'Chapter 20: "Title 20"' not in user
    assert "SYNOPSIS_2_EXACT" in user
    assert "STORY SO FAR" not in user
    assert user.index("THIS CHAPTER") < user.index("CHAPTER CONTINUITY CONTRACT")
    assert user.index("CHAPTER CONTINUITY CONTRACT") < user.index("RESPONSE SHAPE")


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

    assert "CURRENT ORDERED OUTLINE BEATS:" in user
    assert "ON-PAGE HANDOFF:" in user
    assert "elapsed interval, the causal change or decision, and the opening location" in user
    assert "A time label alone is insufficient" in user
    assert "STORY CLOCK: along the forward timeline" in user
    assert "explicitly labelled flashback or parallel track" in user
    assert "unless the outline explicitly labels a flashback" in user
    assert user.index("MIDPOINT: Eun-soo proves") < user.index("THEN Min-jae threatens")
    assert user.index("THEN Min-jae threatens") < user.index("FINALLY they")


def test_rooftop_chapter_three_ledger_pins_surrender_before_recording_and_deposition():
    chapters = [
        {"title": "Setup", "summary": "The contract begins."},
        {"title": "Exposure", "summary": "The theft is exposed."},
        {
            "title": "Rooftop Reckoning",
            "summary": (
                "PRESENT: Tae-jun surrenders the rooftop rights. "
                "LEGAL REALISM: The recordings are ruled inadmissible. "
                "DEPOSITION: Eun-soo's deposition closes the proof chain."
            ),
        },
    ]
    packet = SharedContext(topic="roof", chapters=chapters).scope_for(2)

    surrender = "PRESENT: Tae-jun surrenders the rooftop rights."
    recording = "LEGAL REALISM: The recordings are ruled inadmissible."
    deposition = "DEPOSITION: Eun-soo's deposition closes the proof chain."
    assert packet.index(surrender) < packet.index(recording) < packet.index(deposition)


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

    assert "PREVIOUS OUTLINE COMMITMENT:\nNONE — this chapter opens the book." in first
    assert 'CURRENT ORDERED OUTLINE BEATS:\nChapter 1: "Title 1"' in first
    assert 'Chapter 2: "Title 2"' in first
    assert 'Chapter 3: "Title 3"' not in first
    assert 'Chapter 2: "Title 2"' in last
    assert 'CURRENT ORDERED OUTLINE BEATS:\nChapter 3: "Title 3"' in last
    assert "NEXT RESERVED OUTLINE STATE:\nNONE — this chapter closes the book." in last


def test_real_sequential_tail_and_parallel_scope_remain_separate():
    ctx = _ctx(3)
    item = _compose_for(ctx, 1, prev_tail="ACTUAL_PREVIOUS_NARRATION_SENTINEL")
    user = item.dynamic_block

    assert "STORY SO FAR (the immediately preceding narration" in user
    assert "ACTUAL_PREVIOUS_NARRATION_SENTINEL" in user
    assert "CHAPTER CONTINUITY CONTRACT:" in user
    assert user.index("STORY SO FAR") < user.index("CHAPTER CONTINUITY CONTRACT")
    assert user.index("THIS CHAPTER") < user.index("CHAPTER CONTINUITY CONTRACT")
    assert user.index("CHAPTER CONTINUITY CONTRACT") < user.index("RESPONSE SHAPE")
    assert "ACTUAL_PREVIOUS_NARRATION_SENTINEL" not in item.static_prefix


def test_outline_packet_split_is_conservative_verbatim_and_ordered():
    summary = (
        "PRESENT: Board confrontation. B-PLOT CLIMAX: Family audit. "
        "LEGAL REALISM: Recordings are inadmissible; Tae-jun chooses deposition. "
        "A-PLOT RESOLUTION: They remain together."
    )
    beats = split_ordered_outline_beats(summary)

    assert beats == (
        "PRESENT: Board confrontation.",
        "B-PLOT CLIMAX: Family audit.",
        "LEGAL REALISM: Recordings are inadmissible; Tae-jun chooses deposition.",
        "A-PLOT RESOLUTION: They remain together.",
    )
    assert all(beat in summary for beat in beats)
    assert split_ordered_outline_beats(
        "An unlabelled dense paragraph stays intact. It is not guessed into semantics."
    ) == ("An unlabelled dense paragraph stays intact. It is not guessed into semantics.",)


def test_outline_packet_bundle_binds_exact_rendered_bytes_in_book_order():
    ctx = _ctx(3)
    bundle = outline_packet_bundle(ctx.chapters)
    packet_hashes = {
        str(index + 1): hashlib.sha256(ctx.scope_for(index).encode("utf-8")).hexdigest()
        for index in range(3)
    }
    identity = [[index, packet_hashes[str(index + 1)]] for index in range(3)]
    expected_bundle = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert bundle["contract_version"] == OUTLINE_PACKET_CONTRACT_VERSION
    assert bundle["packet_sha256_by_chapter"] == packet_hashes
    assert bundle["bundle_sha256"] == expected_bundle
    assert bundle["packets_by_chapter"]["2"] == ctx.scope_for(1)
    # Golden renderer pin: any label/order/split/placement byte change must bump
    # the contract version and deliberately update these values together.
    assert OUTLINE_PACKET_CONTRACT_VERSION == "outline_packet_v2"
    assert bundle["packet_sha256_by_chapter"] == {
        "1": "e3b4fb7edb85cd2a392ebdfa239d3fd6fb7670dd2c481948419b61a33e8b0fb7",
        "2": "bfb273992b4bbc74a8dd562af1d9a0924a8710dc3680a773c92d8a21c55fa077",
        "3": "bca009c6682f23a67a7f72b435c64e098da7faf873b78f477c51070f9e680000",
    }
    assert bundle["bundle_sha256"] == \
        "11d722a06fe01cce513d17e5500de8d526871960ea45bc524fe7acd27f542f8a"


def test_packet_restores_planning_leak_guard_and_first_chapter_clock_start():
    chapters = [
        {"title": "Open", "summary": None, "description": "Scene 1 — establish day one."},
        {"title": "Next", "summary": "Continue."},
    ]
    packet = SharedContext(topic="clock", chapters=chapters).scope_for(0)
    assert "Scene 1 — establish day one." in packet
    assert "internal planning references" in packet
    assert "never copy their numbering or Scene N / Chapter N planning labels" in packet
    assert "STORY CLOCK START:" in packet
    assert "ON-PAGE HANDOFF:" not in packet


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

    result = asyncio.run(static._write_chapter(
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
    bundle = outline_packet_bundle(ctx.chapters)
    assert result["outline_packet_contract_version"] == bundle["contract_version"]
    assert result["outline_packet_sha256"] == bundle["packet_sha256_by_chapter"]["2"]
    assert result["outline_packet_bundle_sha256"] == bundle["bundle_sha256"]


def test_fallback_uses_identical_packet_bytes_version_and_hash(monkeypatch):
    ctx = _ctx(3)
    captured = {}

    async def fake_run_worker(worker, prompt, **kwargs):
        captured["prompt"] = prompt
        return {"ok": True, "output": "Complete fallback body."}

    async def fake_word_gate(result, **kwargs):
        return result

    monkeypatch.setattr(static, "_COMPOSE_OK", False)
    monkeypatch.setattr(static, "run_worker", fake_run_worker)
    monkeypatch.setattr(static, "_apply_word_gate", fake_word_gate)

    result = asyncio.run(static._write_chapter(
        ctx=ctx, ch=ctx.chapters[1], no=1, total=3,
        style="storytelling", language="id", mode="text",
        job_id="fallback-packet-witness", worker_model="test-model",
        timeout=20, telemetry_sink=None,
    ))
    bundle = outline_packet_bundle(ctx.chapters)
    exact_packet = bundle["packets_by_chapter"]["2"]

    assert f"CHAPTER CONTINUITY CONTRACT:\n{exact_packet}\n\nRESPONSE SHAPE" in captured["prompt"]
    assert result["outline_packet_contract_version"] == bundle["contract_version"]
    assert result["outline_packet_sha256"] == hashlib.sha256(exact_packet.encode()).hexdigest()
    assert result["outline_packet_bundle_sha256"] == bundle["bundle_sha256"]


def test_rooftop_beat_order_mutation_is_killed_without_pattern_miss(tmp_path):
    root = Path(__file__).resolve().parents[2]
    source_path = root / "python" / "narasi_outline_packet.py"
    source = source_path.read_text(encoding="utf-8")
    old = 'lines.extend(f"  {number}. {beat}" for number, beat in enumerate(beats, 1))'
    new = 'lines.extend(f"  {number}. {beat}" for number, beat in enumerate(reversed(beats), 1))'
    assert source.count(old) == 1, "PATTERN-MISS: beat-order mutation no longer applies exactly once"
    (tmp_path / "narasi_outline_packet.py").write_text(source.replace(old, new), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(root / "python")))
    witness = (
        "from narasi_outline_packet import render_outline_execution_packet as render\n"
        "chapters=[{'title':'Setup','summary':'The contract begins.'},"
        "{'title':'Exposure','summary':'The theft is exposed.'},"
        "{'title':'Rooftop','summary':'PRESENT: Tae-jun surrenders the rooftop rights. "
        "LEGAL REALISM: The recordings are ruled inadmissible. "
        "DEPOSITION: Eun-soo closes the proof chain.'}]\n"
        "packet=render(chapters,2)\n"
        "assert packet.index('PRESENT:') < packet.index('LEGAL REALISM:') < packet.index('DEPOSITION:')\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", witness],
        cwd=root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=30, check=False,
    )
    assert run.returncode != 0 and "AssertionError" in run.stdout, \
        f"SURVIVED rooftop beat-order mutation:\n{run.stdout}"
