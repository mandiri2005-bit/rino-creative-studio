"""Behavioral proofs for cross-chunk HEAVY polish seam repair.

All provider work is replaced at the existing ``synthesize`` boundary. The
production reducer and its acceptance guards run unchanged.
"""

from __future__ import annotations

import asyncio

from orchestrator import static


ORIGINAL_TAIL = "ORIGINAL_TAIL_SENTINEL."
POLISHED_TAIL = "POLISHED_TAIL_SENTINEL."
BRIDGE = "Yuna signs the release, then crosses town before dawn."
AUTHORITY = "AUTHORITATIVE OUTLINE AND STORY BIBLE ONLY."


def _long_two_chapter_book() -> str:
    first = " ".join([f"alpha{i}" for i in range(329)] + [ORIGINAL_TAIL])
    second = " ".join([f"beta{i}" for i in range(329)] + ["SECOND_END."])
    return (
        f"## Chapter 1: Origin\n\n{first}\n\n"
        f"## Chapter 2: Consequence\n\n{second}"
    )


def _force_two_chunks(monkeypatch, *, parallel: int) -> tuple[str, list[str]]:
    book = _long_two_chapter_book()
    monkeypatch.setattr(static, "max_tokens_for", lambda _model: 500)
    monkeypatch.setenv("NARASI_POLISH_CHUNK", "1")
    monkeypatch.setenv("NARASI_POLISH_CHUNK_WORDS", "400")
    monkeypatch.setenv("NARASI_POLISH_PARALLEL", str(parallel))
    monkeypatch.setenv("NARASI_POLISH_MAX_WORDS", "5000")
    monkeypatch.setenv("NARASI_POLISH_BIG_MODEL", "")
    # ceil=500 -> fit_words=int(475/(1.45*1.08))=303. Each chapter is
    # deliberately larger, so the real chunker yields exactly two chunks.
    chunks = static._split_into_chunks(book, 303)
    assert len(chunks) == 2
    return book, chunks


def _run_reduce(book: str, *, mode: str):
    return asyncio.run(static._polish_reduce(
        book=book,
        topic="A buried negative",
        style="kdrama_serial",
        language="English",
        polish=mode,
        manager_model="test-model",
        timeout=2,
        telemetry_sink=None,
        authority_text=AUTHORITY,
    ))


def _read_only_tail(system: str) -> str:
    return system.split("--- BEGIN READ-ONLY TAIL ---\n", 1)[1].split(
        "\n--- END READ-ONLY TAIL ---", 1
    )[0]


def _headings(text: str) -> tuple[str, ...]:
    return tuple(line for line, _offset in static._polish_heading_records(text))


def test_bounded_tail_is_confined_to_the_previous_chunks_final_chapter():
    previous_chunk = (
        "## Chapter 1: Earlier\n\n" + "earlier-only " * 50 + "end.\n\n"
        "## Chapter 2: Previous\n\n" + "previous-only " * 20 + "end."
    )

    tail = static._bounded_polish_tail(previous_chunk)

    assert "## Chapter 2: Previous" in tail
    assert "previous-only" in tail
    assert "## Chapter 1: Earlier" not in tail
    assert "earlier-only" not in tail


def test_heavy_serially_bridges_with_the_accepted_previous_chunk_tail(monkeypatch):
    book, chunks = _force_two_chunks(monkeypatch, parallel=4)
    calls: list[dict[str, str]] = []
    active = 0
    peak = 0

    async def fake_synthesize(instruction, results, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        current = results[0]["output"]
        calls.append({
            "instruction": instruction,
            "system": kwargs.get("system", ""),
            "input": current,
            "task_id": kwargs.get("task_id", ""),
        })
        if len(calls) == 1:
            output = current.replace(ORIGINAL_TAIL, POLISHED_TAIL)
        else:
            output = f"{BRIDGE}\n\n{current}"
        active -= 1
        return {"ok": True, "output": output, "telemetry": {}}

    monkeypatch.setattr(static, "synthesize", fake_synthesize)
    polished, ok = _run_reduce(book, mode="heavy")

    assert ok is True
    assert len(calls) == len(chunks) == 2
    assert peak == 1, "HEAVY must ignore parallel configuration and preserve dependency order"

    tail = _read_only_tail(calls[1]["system"])
    assert len(tail.split()) == 300
    assert POLISHED_TAIL in tail
    assert ORIGINAL_TAIL not in tail
    assert "alpha0" not in tail, "the context must be a bounded tail, not the full chunk"
    assert AUTHORITY in calls[1]["system"]
    assert static._POLISH_TAIL_LABEL in calls[1]["system"]
    assert "INCOMING CROSS-CHUNK SEAM" in calls[1]["instruction"]
    assert calls[1]["input"] == chunks[1], "the tail must not enter editable manuscript text"

    expected_first = chunks[0].replace(ORIGINAL_TAIL, POLISHED_TAIL)
    expected = f"{expected_first}\n\n{BRIDGE}\n\n{chunks[1]}"
    assert polished == expected
    assert f"{POLISHED_TAIL}\n\n{BRIDGE}\n\n## Chapter 2: Consequence" in polished
    assert _headings(polished) == _headings(book)


def test_heading_mutation_is_rejected_and_original_chunk_survives(monkeypatch):
    book, chunks = _force_two_chunks(monkeypatch, parallel=3)
    calls = 0

    async def fake_synthesize(_instruction, results, **_kwargs):
        nonlocal calls
        calls += 1
        current = results[0]["output"]
        if calls == 2:
            current = current.replace(
                "## Chapter 2: Consequence", "## Chapter 9: Mutated"
            )
        return {"ok": True, "output": current, "telemetry": {}}

    monkeypatch.setattr(static, "synthesize", fake_synthesize)
    polished, ok = _run_reduce(book, mode="heavy")

    assert calls == len(chunks) == 2
    assert ok is True  # chunk 1 succeeded; only the unsafe chunk 2 candidate was rejected
    assert polished == book
    assert "## Chapter 9: Mutated" not in polished
    assert _headings(polished) == _headings(book)


def test_clean_heavy_seam_adds_no_bridge_or_bytes(monkeypatch):
    book, chunks = _force_two_chunks(monkeypatch, parallel=2)
    calls = 0

    async def fake_synthesize(_instruction, results, **_kwargs):
        nonlocal calls
        calls += 1
        return {"ok": True, "output": results[0]["output"], "telemetry": {}}

    monkeypatch.setattr(static, "synthesize", fake_synthesize)
    polished, ok = _run_reduce(book, mode="heavy")

    assert calls == len(chunks) == 2
    assert ok is True
    assert polished == book
    assert _headings(polished) == _headings(book)


def test_read_only_tail_echo_is_rejected_before_delivery(monkeypatch):
    book, chunks = _force_two_chunks(monkeypatch, parallel=2)
    calls = 0

    async def fake_synthesize(_instruction, results, **kwargs):
        nonlocal calls
        calls += 1
        current = results[0]["output"]
        if calls == 2:
            tail_words = _read_only_tail(kwargs["system"]).split()
            # Twenty verbatim words are small enough to stay inside every bloat/bridge
            # allowance, so only the dedicated READ-ONLY leak guard rejects this echo.
            current = f"{' '.join(tail_words[:20])}\n\n{current}"
        return {"ok": True, "output": current, "telemetry": {}}

    monkeypatch.setattr(static, "synthesize", fake_synthesize)
    polished, ok = _run_reduce(book, mode="heavy")

    assert calls == len(chunks) == 2
    assert ok is True
    assert polished == book
    assert polished.count("alpha30 alpha31 alpha32") == 1


def test_bridge_prefix_is_counted_by_the_existing_bloat_guard(monkeypatch):
    book, chunks = _force_two_chunks(monkeypatch, parallel=2)
    calls = 0

    async def fake_synthesize(_instruction, results, **_kwargs):
        nonlocal calls
        calls += 1
        current = results[0]["output"]
        if calls == 2:
            # One paragraph and below the dedicated 160-word bridge ceiling, but large
            # enough to push the complete candidate over the existing 135% chunk band.
            bridge = " ".join(f"bridge{i}" for i in range(130)) + "."
            current = f"{bridge}\n\n{current}"
        return {"ok": True, "output": current, "telemetry": {}}

    monkeypatch.setattr(static, "synthesize", fake_synthesize)
    polished, ok = _run_reduce(book, mode="heavy")

    assert calls == len(chunks) == 2
    assert ok is True
    assert polished == book
    assert "bridge129" not in polished


def test_bridge_prefix_cannot_mask_editable_chunk_truncation(monkeypatch):
    book, chunks = _force_two_chunks(monkeypatch, parallel=2)
    calls = 0

    async def fake_synthesize(_instruction, results, **_kwargs):
        nonlocal calls
        calls += 1
        current = results[0]["output"]
        if calls == 2:
            heading = "## Chapter 2: Consequence"
            bridge = " ".join(f"bridge{i}" for i in range(150)) + "."
            # Total words clear 75% only because of the bridge; the editable
            # manuscript after its unchanged heading retains far less than 75%.
            short_body = " ".join(current.split()[4:104]) + " truncated."
            current = f"{bridge}\n\n{heading}\n\n{short_body}"
        return {"ok": True, "output": current, "telemetry": {}}

    monkeypatch.setattr(static, "synthesize", fake_synthesize)
    polished, ok = _run_reduce(book, mode="heavy")

    assert calls == len(chunks) == 2
    assert ok is True
    assert polished == book
    assert "truncated." not in polished


def test_light_keeps_parallel_path_without_tail_or_bridge_licence(monkeypatch):
    book, chunks = _force_two_chunks(monkeypatch, parallel=2)
    calls: list[dict[str, str]] = []
    active = 0
    peak = 0

    async def fake_synthesize(instruction, results, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append({
            "instruction": instruction,
            "system": kwargs.get("system", ""),
            "input": results[0]["output"],
        })
        await asyncio.sleep(0.01)
        active -= 1
        return {"ok": True, "output": results[0]["output"], "telemetry": {}}

    monkeypatch.setattr(static, "synthesize", fake_synthesize)
    polished, ok = _run_reduce(book, mode="light")

    assert calls and len(calls) == len(chunks) == 2
    assert peak == 2, "LIGHT must retain the existing configured parallel path"
    assert ok is True
    assert polished == book
    assert all(static._POLISH_TAIL_LABEL not in call["system"] for call in calls)
    assert all("INCOMING CROSS-CHUNK SEAM" not in call["instruction"] for call in calls)
    assert sorted(call["input"] for call in calls) == sorted(chunks)
