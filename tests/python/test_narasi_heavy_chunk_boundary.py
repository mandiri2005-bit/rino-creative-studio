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


def _run_reduce(book: str, *, mode: str, authority_text: str = AUTHORITY):
    return asyncio.run(static._polish_reduce(
        book=book,
        topic="A buried negative",
        style="kdrama_serial",
        language="English",
        polish=mode,
        manager_model="test-model",
        timeout=2,
        telemetry_sink=None,
        authority_text=authority_text,
    ))


def _read_only_tail(system: str) -> str:
    return system.split("--- BEGIN READ-ONLY TAIL ---\n", 1)[1].split(
        "\n--- END READ-ONLY TAIL ---", 1
    )[0]


def _headings(text: str) -> tuple[str, ...]:
    return tuple(line for line, _offset in static._polish_heading_records(text))


EXPECTED_HEAVY_PROCEDURE = """MANDATORY HEAVY-POLISH PROCEDURE
(The authoritative Outline and Story Bible remain the factual authority.)

1. FACT AUTHORITY
Preserve every fact fixed by the Outline and Story Bible. Do not invent plot events or choose a conflicting variant without authority.

2. BOUNDARY INSPECTION
Treat the final two paragraphs before each chapter heading and the first two paragraphs after it as one editable seam.

3. BRIDGE REPAIR
If the seam is broken, add at most one or two short paragraphs immediately before the next chapter heading. Show only the necessary cause or decision, location change, and elapsed time.

4. SEAM DEDUPLICATION
When adding a bridge, remove or compress equivalent transition setup after the heading. Preserve the first unique plot action.

5. EVIDENCE PROVENANCE
For every recurring evidence object, enforce one origin, hiding place, finder, acquisition event, and custody chain from the Story Bible EVIDENCE MAP. A legal challenge must match the acquisition actually depicted. If authority is silent, preserve the earliest on-page acquisition unless the Outline explicitly says otherwise.

6. HARD PRESERVATION
Preserve every unique plot event, action beat, scene outcome, and evidentiary fact. Retain at least 80% of the input word count. Do not move beats, repeat setup, create new scenes, or alter, remove, rename, renumber, translate, or move any chapter heading.

7. NO-OP WHEN CLEAN
If a boundary or evidence chain is already consistent, leave it unchanged."""


def test_heavy_instruction_is_one_numbered_procedure_with_separate_output_rule():
    instruction, role = static._polish_instruction(
        "heavy", "The sealed ledger", "English"
    )

    assert role == "synthesize"
    assert static._HEAVY_POLISH_PROCEDURE == EXPECTED_HEAVY_PROCEDURE
    assert instruction.count(EXPECTED_HEAVY_PROCEDURE) == 1
    assert instruction.endswith(
        "FINAL OUTPUT\nReturn ONLY the edited book in English, no notes."
    )
    assert instruction.index(EXPECTED_HEAVY_PROCEDURE) < instruction.index("FINAL OUTPUT")
    assert "voice and tense" not in instruction
    assert "tighten flabby passages" not in instruction
    assert (
        "Preserve every unique plot event, action beat, scene outcome, and evidentiary fact"
        in instruction
    )
    assert "Retain at least 80% of the input word count" in instruction


def test_whole_book_heavy_repairs_bridge_and_deduplicates_post_heading_setup(monkeypatch):
    book = (
        "## Chapter 1: Decision\n\n"
        "Mira finds the address and weighs the risk. " + "She checks every detail. " * 22
        + "\n\n## Chapter 2: Vault\n\n"
        "Without explanation, Mira is suddenly inside the archive. "
        "Mira decides to cross town, takes the night train, and reaches the archive. "
        "She opens the first unique locker and photographs its seal. "
        + "She records the contents carefully. " * 22
    )
    repaired = book.replace(
        "\n\n## Chapter 2: Vault\n\n"
        "Without explanation, Mira is suddenly inside the archive. "
        "Mira decides to cross town, takes the night train, and reaches the archive. ",
        "\n\nMira accepts the risk, takes the night train across town, and reaches the "
        "archive two hours later.\n\n"
        "## Chapter 2: Vault\n\n",
    )
    calls: list[dict] = []

    async def fake_synthesize(instruction, results, **kwargs):
        calls.append({"instruction": instruction, "input": results[0]["output"], **kwargs})
        return {"ok": True, "output": repaired, "telemetry": {}}

    monkeypatch.setattr(static, "synthesize", fake_synthesize)
    monkeypatch.setattr(static, "max_tokens_for", lambda _model: 100000)
    polished, ok = _run_reduce(book, mode="heavy")

    assert (ok, len(calls)) == (True, 1)
    assert polished == repaired
    assert _headings(polished) == _headings(book)
    assert "two hours later.\n\n## Chapter 2: Vault" in polished
    assert "Mira decides to cross town" not in polished
    assert "She opens the first unique locker" in polished
    assert EXPECTED_HEAVY_PROCEDURE in calls[0]["instruction"]


def test_whole_book_heavy_reconciles_evidence_map_and_legal_challenge(monkeypatch):
    authority = """AUTHORITATIVE OUTLINE
The ledger is the recurring physical evidence.

STORY BIBLE EVIDENCE MAP
Object: red ledger
Origin: the North Quay accounts office
Hiding place: archive locker 17
Finder: Mira
Acquisition: Mira cuts locker 17's wax seal on-page
Custody: Mira -> counsel
Legal challenge: defense disputes the wax seal Mira cut at locker 17"""
    book = (
        "## Chapter 1: Ledger\n\n"
        "Jon says he found the red ledger at Central Station and handed it to counsel. "
        + "The ledger remains the case's central evidence. " * 28
        + "\n\n## Chapter 2: Hearing\n\n"
        "The defense challenges a warrantless seizure by Jon at Central Station. "
        + "The court examines the acquisition and custody record. " * 28
    )
    reconciled = book.replace(
        "Jon says he found the red ledger at Central Station and handed it to counsel.",
        "Mira finds the red ledger in archive locker 17, cuts its wax seal, and hands it to counsel.",
    ).replace(
        "The defense challenges a warrantless seizure by Jon at Central Station.",
        "The defense challenges the wax seal Mira cut when acquiring the ledger from locker 17.",
    )
    calls: list[dict] = []

    async def fake_synthesize(instruction, results, **kwargs):
        calls.append({"instruction": instruction, "input": results[0]["output"], **kwargs})
        return {"ok": True, "output": reconciled, "telemetry": {}}

    monkeypatch.setattr(static, "synthesize", fake_synthesize)
    monkeypatch.setattr(static, "max_tokens_for", lambda _model: 100000)
    polished, ok = _run_reduce(book, mode="heavy", authority_text=authority)

    assert (ok, len(calls)) == (True, 1)
    assert polished == reconciled
    assert _headings(polished) == _headings(book)
    assert "Mira finds the red ledger in archive locker 17" in polished
    assert "wax seal Mira cut when acquiring the ledger from locker 17" in polished
    assert "Central Station" not in polished
    assert authority in calls[0]["system"]
    assert "A legal challenge must match the acquisition actually depicted." in calls[0]["instruction"]


def test_whole_book_clean_heavy_pass_is_a_byte_identical_no_op(monkeypatch):
    book = (
        "## Chapter 1: Custody\n\nMira logs the sealed ledger before dawn. "
        + "The custody record remains intact. " * 24
        + "\n\n## Chapter 2: Hearing\n\nTwo hours later, counsel presents that same ledger. "
        + "The hearing follows the documented chain. " * 24
    )
    calls = 0

    async def fake_synthesize(_instruction, results, **_kwargs):
        nonlocal calls
        calls += 1
        return {"ok": True, "output": results[0]["output"], "telemetry": {}}

    monkeypatch.setattr(static, "synthesize", fake_synthesize)
    monkeypatch.setattr(static, "max_tokens_for", lambda _model: 100000)
    polished, ok = _run_reduce(book, mode="heavy")

    assert (ok, calls) == (True, 1)
    assert polished == book
    assert _headings(polished) == _headings(book)


def test_light_instruction_bytes_are_unchanged():
    instruction, role = static._polish_instruction(
        "light", "The sealed ledger", "English"
    )

    assert role == "polish"
    assert instruction == (
        'You are the editor-in-chief doing a LIGHT final pass of a multi-chapter narrative '
        'about "The sealed ledger". ONLY smooth the seams between chapters, remove obvious '
        'cross-chapter repetition, and keep the register consistent. Do NOT rewrite content, '
        'do NOT change any fact, name, date or number, do NOT shorten the text. Keep every '
        'chapter-heading line (each begins with `## `) exactly as given — do not remove, rename, '
        'renumber, translate or move them, and never write a new heading of your own. Return '
        'ONLY the lightly-edited edited book in English.'
    )


def _chapter_with_word_count(heading: str, word_count: int) -> str:
    body_words = word_count - len(heading.split())
    assert body_words >= 1
    body = " ".join(
        [f"kept{i}" for i in range(body_words - 1)] + ["end."]
    )
    chapter = f"{heading}\n\n{body}"
    assert len(chapter.split()) == word_count
    return chapter


def _retained_candidate(text: str, word_count: int) -> str:
    headings = _headings(text)
    assert len(headings) == 1
    return _chapter_with_word_count(headings[0], word_count)


def test_whole_book_heavy_rejects_79_percent_and_accepts_exactly_80(monkeypatch):
    book = _chapter_with_word_count("## Chapter 1: Retention", 100)
    candidates = [
        _retained_candidate(book, 79),
        _retained_candidate(book, 80),
    ]
    calls = 0

    async def fake_synthesize(_instruction, _results, **_kwargs):
        nonlocal calls
        output = candidates[calls]
        calls += 1
        return {"ok": True, "output": output, "telemetry": {}}

    monkeypatch.setattr(static, "synthesize", fake_synthesize)
    monkeypatch.setattr(static, "max_tokens_for", lambda _model: 100000)

    rejected, rejected_ok = _run_reduce(book, mode="heavy")
    accepted, accepted_ok = _run_reduce(book, mode="heavy")

    assert (rejected, rejected_ok) == (book, False)
    assert (accepted, accepted_ok) == (candidates[1], True)
    assert calls == 2


def test_chunked_heavy_applies_80_percent_to_each_editable_chunk(monkeypatch):
    first = _chapter_with_word_count("## Chapter 1: First", 100)
    second = _chapter_with_word_count("## Chapter 2: Second", 100)
    book = f"{first}\n\n{second}"
    monkeypatch.setattr(static, "max_tokens_for", lambda _model: 150)
    monkeypatch.setenv("NARASI_POLISH_CHUNK", "1")
    monkeypatch.setenv("NARASI_POLISH_CHUNK_WORDS", "90")
    monkeypatch.setenv("NARASI_POLISH_MAX_WORDS", "5000")
    monkeypatch.setenv("NARASI_POLISH_BIG_MODEL", "")
    chunks = static._split_into_chunks(book, 90)
    assert chunks == [first, second]
    calls = 0

    async def fake_synthesize(_instruction, results, **_kwargs):
        nonlocal calls
        current = results[0]["output"]
        calls += 1
        if calls == 1:
            output = _retained_candidate(current, 79)
        elif calls == 3:
            output = _retained_candidate(current, 80)
        else:
            output = current
        return {"ok": True, "output": output, "telemetry": {}}

    monkeypatch.setattr(static, "synthesize", fake_synthesize)

    rejected, rejected_ok = _run_reduce(book, mode="heavy")
    accepted, accepted_ok = _run_reduce(book, mode="heavy")

    assert rejected == book
    assert rejected_ok is True  # chunk 2 is a successful no-op; chunk 1 was discarded
    assert accepted == f"{_retained_candidate(first, 80)}\n\n{second}"
    assert accepted_ok is True
    assert calls == 4


def test_chunked_heavy_final_rejoin_rejects_79_percent_and_accepts_80(monkeypatch):
    first = _chapter_with_word_count("## Chapter 1: First", 100)
    second = _chapter_with_word_count("## Chapter 2: Second", 100)
    book = f"{first}\n\n{second}"
    monkeypatch.setattr(static, "max_tokens_for", lambda _model: 150)
    monkeypatch.setenv("NARASI_POLISH_CHUNK", "1")
    monkeypatch.setenv("NARASI_POLISH_CHUNK_WORDS", "90")
    monkeypatch.setenv("NARASI_POLISH_MAX_WORDS", "5000")
    monkeypatch.setenv("NARASI_POLISH_BIG_MODEL", "")
    target = 79
    calls = 0

    async def fake_polish_one(text, **_kwargs):
        nonlocal calls
        calls += 1
        return _retained_candidate(text, target), True

    monkeypatch.setattr(static, "_polish_one", fake_polish_one)

    rejected, rejected_ok = _run_reduce(book, mode="heavy")
    target = 80
    accepted, accepted_ok = _run_reduce(book, mode="heavy")

    assert (rejected, rejected_ok) == (book, False)
    assert accepted == (
        f"{_retained_candidate(first, 80)}\n\n{_retained_candidate(second, 80)}"
    )
    assert accepted_ok is True
    assert calls == 4


def test_light_retains_legacy_75_percent_acceptance_floor(monkeypatch):
    book = _chapter_with_word_count("## Chapter 1: Retention", 100)
    candidates = [
        _retained_candidate(book, 74),
        _retained_candidate(book, 75),
    ]
    calls = 0

    async def fake_synthesize(_instruction, _results, **_kwargs):
        nonlocal calls
        output = candidates[calls]
        calls += 1
        return {"ok": True, "output": output, "telemetry": {}}

    monkeypatch.setattr(static, "synthesize", fake_synthesize)
    monkeypatch.setattr(static, "max_tokens_for", lambda _model: 100000)

    rejected, rejected_ok = _run_reduce(book, mode="light")
    accepted, accepted_ok = _run_reduce(book, mode="light")

    assert (rejected, rejected_ok) == (book, False)
    assert (accepted, accepted_ok) == (candidates[1], True)
    assert calls == 2


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
            # Total words clear the retention floor only because of the bridge;
            # the editable manuscript after its unchanged heading is far below it.
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
