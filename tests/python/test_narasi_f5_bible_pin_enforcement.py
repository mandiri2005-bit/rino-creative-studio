"""F5 — the Bible-pin ownership filter, reached the way production reaches it.

🔴 WHY AN IN-RUN TEST AND NOT ANOTHER DIRECT CALL. The pin-time filter had two kinds of
   witness: unit calls on `_f5_unowned_ledger_terms` itself, and an AST witness that the
   call site passes `bible=""`. Neither executes the branch. The branch lives deep inside
   `narrate_chapters`, behind a chain of conditions — story-bible enabled, at least two
   chapters, a fiction style, the ledger validator on, bible-level hits found, enforce on
   — and every one of those had to hold at once before the filter decided anything.

🔴 THE VACUUM THIS FILE WAS WRITTEN TO AVOID. The first attempt at an end-to-end test
   supplied the Bible through `SharedContext`. `narrate_chapters` only BUILDS a bible when
   `ctx.canonical_facts` is empty, so the entire block was skipped: `ledger_hits_scan` ran
   ZERO times and the test passed anyway, proving nothing. Every test here therefore
   asserts the scan was actually entered before it asserts anything about its result.

   The ledger scan and the bible builder are spies, not bypasses: they record that the
   real code path called them, and return controlled values so the assertions are about
   the FILTER's decision rather than about the ledger corpus of the day.

The contract under test: at pin time the candidate Bible is NOT an authority. A term the
accepted OUTLINE establishes is owned and must survive; a term the candidate Bible
invented for itself must not be able to exempt itself from the re-roll.
"""
from __future__ import annotations

import asyncio

import pytest

from orchestrator import dynamic
from orchestrator import static
from orchestrator.context_builder import SharedContext


#: Established by the accepted outline below — owned, must never be enforced against.
OUTLINE_TERM = "Tae-jun"
#: Invented by the candidate Bible and nothing else — unowned, must reach enforcement.
BIBLE_TERM = "Hanjin"


def _ctx() -> SharedContext:
    return SharedContext(
        topic="rooftop contract",
        chapters=[
            {"title": "Return", "summary": f"Kang {OUTLINE_TERM} returns as Lee Jin-woo."},
            {"title": "Reveal", "summary": "Cha Eun-soo proves the theft in chapter two."},
        ],
        # 🔴 EMPTY ON PURPOSE. A supplied Bible makes `narrate_chapters` skip the build,
        # the pin, the validator and the enforce branch in one go — that is exactly how
        # the first version of this test managed to assert nothing at all.
        canonical_facts="",
        facts_are_bible=True,
        style="storytelling",
    )


@pytest.fixture
def pin_run(monkeypatch):
    """Drive the real `narrate_chapters` to the Bible-pin enforcement branch."""
    import narasi_counters as nc

    seen = {"scans": [], "bible_calls": [], "filter_calls": []}

    async def fake_build_story_bible(topic, chapters, **kwargs):
        seen["bible_calls"].append(kwargs.get("extra_negative"))
        if len(seen["bible_calls"]) == 1:
            return (f"FACT-SHEET: {OUTLINE_TERM} leads. The {BIBLE_TERM} group owns the "
                    f"tower. Kang {OUTLINE_TERM} signs on the roof.")
        return f"FACT-SHEET: {OUTLINE_TERM} leads. The consortium owns the tower."

    def fake_scan(manuscript, *, bible="", style_key=None):
        """Report a bible-level hit for each seeded term still present in the text."""
        seen["scans"].append(bible)
        hits = [{"term": f"name:{term}", "where": "bible", "count": 1, "snippet": term}
                for term in (OUTLINE_TERM, BIBLE_TERM) if term in (bible or "")]
        return {"status": "FLAG" if hits else "PASS", "hits": hits,
                "bible_hits": len(hits)}

    real_filter = static._f5_unowned_ledger_terms

    def spy_filter(terms, **kwargs):
        out = real_filter(terms, **kwargs)
        seen["filter_calls"].append({"terms": list(terms), "kwargs": kwargs, "out": out})
        return out

    async def fake_write(*, no, **kwargs):
        return {"ok": True, "output": f"Chapter body {no + 1}.", "no": no,
                "model": "test-model"}

    async def fake_polish_reduce(*, book, **kwargs):
        return book, False

    monkeypatch.setenv("NARASI_STORY_BIBLE", "1")
    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "off")
    monkeypatch.setenv("NARASI_LEDGER_VALIDATOR", "1")
    monkeypatch.setenv("NARASI_LEDGER_ENFORCE", "1")
    monkeypatch.setenv("NARASI_LEDGER_ENFORCE_SURGICAL", "0")
    monkeypatch.setenv("NARASI_LEDGER_VALUE_DEMOTE", "0")
    monkeypatch.setenv("NARASI_BIBLE_BEST_OF", "0")
    monkeypatch.setattr(dynamic, "build_story_bible", fake_build_story_bible)
    monkeypatch.setattr(nc, "ledger_hits_scan", fake_scan)
    monkeypatch.setattr(static, "_f5_unowned_ledger_terms", spy_filter)
    monkeypatch.setattr(static, "_write_chapter", fake_write)
    monkeypatch.setattr(static, "_polish_reduce", fake_polish_reduce)

    def run():
        ctx = _ctx()
        result = asyncio.run(static.narrate_chapters(
            ctx.topic, ctx.chapters, style="storytelling", language="id",
            polish="light", worker_model="test-model", manager_model="test-model",
            shared_context=ctx, max_parallel=2))
        # 🔴 THE ANTI-VACUUM GATE. Everything below is only evidence if the branch ran.
        assert seen["scans"], (
            "ledger_hits_scan was never called — the pin-time branch was not reached "
            "and nothing here is evidence of anything")
        assert seen["filter_calls"], (
            "the ownership filter was never called — enforce did not run")
        seen["ctx"] = ctx
        seen["result"] = result
        return seen

    return run


# ---------------------------------------------------------------------------
# The branch is genuinely entered
# ---------------------------------------------------------------------------
def test_the_pin_time_ledger_scan_runs_on_a_bible_built_in_this_run(pin_run):
    seen = pin_run()
    assert len(seen["bible_calls"]) >= 1, "no Bible was built in-run"
    assert BIBLE_TERM in seen["scans"][0], (
        "the first scan must be of the candidate Bible this run just built")


# ---------------------------------------------------------------------------
# ...and the ownership decision it exists to make is the F5 one
# ---------------------------------------------------------------------------
def test_the_accepted_outline_owns_its_term_and_it_never_reaches_enforcement(pin_run):
    """`Tae-jun` is established by the accepted outline and merely adopted by the
    Bible. Enforcing against it would re-roll a Bible that had just done the right
    thing — the canary v9 defect."""
    seen = pin_run()
    call = seen["filter_calls"][0]
    assert f"name:{OUTLINE_TERM}" in call["terms"], "the ledger did flag it"
    assert OUTLINE_TERM not in call["out"], (
        "an outline-established term was sent to enforcement")


def test_a_term_the_candidate_bible_invented_cannot_exempt_itself(pin_run):
    """🔴 THE WHOLE POINT OF `bible=""` AT THIS CALL SITE. The candidate has not been
    accepted yet. If it counted as authority, every term it just invented would be
    self-exempt and this enforcement would be switched off entirely."""
    seen = pin_run()
    call = seen["filter_calls"][0]
    assert f"name:{BIBLE_TERM}" in call["terms"]
    assert BIBLE_TERM in call["out"], (
        "the candidate Bible exempted its own invention — enforcement is a no-op")


def test_the_filter_is_never_handed_the_candidate_bible(pin_run):
    """Behavioural, not structural: whatever the call site is spelled like, the filter
    must not receive the candidate text in any keyword."""
    seen = pin_run()
    for call in seen["filter_calls"]:
        assert "bible" not in call["kwargs"], "the candidate Bible was passed in"
        for value in call["kwargs"].values():
            assert BIBLE_TERM not in str(value), (
                f"the candidate Bible's own term reached the filter via {call['kwargs']}")


def test_the_filter_receives_the_accepted_outline_as_authority(pin_run):
    seen = pin_run()
    call = seen["filter_calls"][0]
    assert OUTLINE_TERM in str(call["kwargs"].get("outline") or ""), (
        "the accepted outline never reached the ownership decision")


# ---------------------------------------------------------------------------
# ...and the enforcement it drives quotes only the unowned term
# ---------------------------------------------------------------------------
def test_the_reroll_quotes_back_only_the_unowned_term(pin_run):
    """The observable consequence of the whole filter: what the re-roll is told to
    avoid. One owned term in, one unowned term out."""
    seen = pin_run()
    assert len(seen["bible_calls"]) == 2, "the enforce re-roll never happened"
    negative = seen["bible_calls"][1] or ""
    assert BIBLE_TERM in negative
    assert OUTLINE_TERM not in negative, (
        "the re-roll was told to avoid a term the accepted outline owns")


def test_supplying_the_bible_skips_the_branch_entirely(monkeypatch):
    """🔴 THE VACUUM, PINNED AS A FACT RATHER THAN A WARNING IN A COMMENT.

    This is the shape the first end-to-end attempt had. With `canonical_facts` already
    set, `narrate_chapters` never builds a Bible, so the pin, the validator and the
    enforce branch are all skipped and `ledger_hits_scan` is called ZERO times — while
    a test written this way still goes green. Asserting the zero here means the day
    someone "simplifies" the fixture by supplying the Bible, THIS test fails and names
    the reason, instead of six tests quietly ceasing to test anything."""
    import narasi_counters as nc

    scans: list = []

    async def fake_build_story_bible(*_a, **_k):     # pragma: no cover - must not run
        raise AssertionError("a Bible was built despite canonical_facts being supplied")

    def fake_scan(manuscript, *, bible="", style_key=None):
        scans.append(bible)
        return {"status": "PASS", "hits": [], "bible_hits": 0}

    async def fake_write(*, no, **kwargs):
        return {"ok": True, "output": f"Chapter body {no + 1}.", "no": no,
                "model": "test-model"}

    async def fake_polish_reduce(*, book, **kwargs):
        return book, False

    monkeypatch.setenv("NARASI_STORY_BIBLE", "1")
    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "off")
    monkeypatch.setenv("NARASI_LEDGER_VALIDATOR", "1")
    monkeypatch.setenv("NARASI_LEDGER_ENFORCE", "1")
    monkeypatch.setattr(dynamic, "build_story_bible", fake_build_story_bible)
    monkeypatch.setattr(nc, "ledger_hits_scan", fake_scan)
    monkeypatch.setattr(static, "_write_chapter", fake_write)
    monkeypatch.setattr(static, "_polish_reduce", fake_polish_reduce)

    ctx = _ctx()
    ctx.canonical_facts = f"FACT-SHEET: the {BIBLE_TERM} group owns the tower."
    asyncio.run(static.narrate_chapters(
        ctx.topic, ctx.chapters, style="storytelling", language="id",
        polish="light", worker_model="test-model", manager_model="test-model",
        shared_context=ctx, max_parallel=2))

    assert scans == [], (
        "the pin-time scan ran on a supplied Bible — if this ever becomes true, the "
        "fixture above may supply one too; today it must not, or those tests are vacuous")


def test_the_reroll_is_pinned_only_because_it_strictly_reduced_hits(pin_run):
    """The re-roll replaces the pinned Bible only on a strict improvement; the second
    scan is what decides that, so it too must have really run."""
    seen = pin_run()
    assert len(seen["scans"]) == 2, "the re-rolled Bible was never re-scanned"
    assert BIBLE_TERM not in seen["scans"][1]
    assert BIBLE_TERM not in (seen["ctx"].canonical_facts or ""), (
        "the improved re-roll was not pinned")
