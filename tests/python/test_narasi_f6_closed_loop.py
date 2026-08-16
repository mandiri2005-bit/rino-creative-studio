"""F6 closed loop — detect → route → repair → verify FINAL bytes → account → fail closed.

🔴 THE SHAPE THIS PROVES IMPOSSIBLE, END TO END.

        detected → repair failed / no-op → original still delivered

🔴 AND THE ORDERING DEFECT IT PROVES IMPOSSIBLE TOO. `_apply_v3_gates` is NOT the last thing
   that touches the manuscript: the post-gates dedup guard runs after it and can collapse
   chapter blocks. A verdict computed inside the gates would describe a manuscript nobody
   delivers — the same class as F1's `l3_proof_invalidated_late_mutation`. So the change set,
   the verification and the accounting all come from `_f6_finalize`, which reads `result` as it
   stands after every mutation. One test below mutates the book BETWEEN the two calls and
   proves the accounting followed the bytes.

Scripted provider doubles throughout; `make_narasi_client` raises, so a real network call fails
the test rather than escaping.
"""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

import laozhang_api as lz
import narration_api as na


def _live(name):
    import importlib
    return sys.modules.get(name, importlib.import_module(name))


CH = ("The Return", "The Reveal", "The Board")
PAST = ("Eun-soo walked the corridor and counted the doors she had closed.",
        "The last light of the city painted the window while Min-jae waited.",
        "Tae-jun signed the deposition and the room emptied of its noise.")
PRESENT_CH2 = "The last light of the city paints the window while Min-jae sits and waits."


def _book(bodies):
    return "\n\n".join(f"## Chapter {i}: {t}\n\n{b}"
                       for i, (t, b) in enumerate(zip(CH, bodies), 1))


DRIFTED = _book((PAST[0], PRESENT_CH2, PAST[2]))
REPAIRED = _book(PAST)


# Transforms a real revise could plausibly perform on the book it receives. Each edits the
# GIVEN text, so the server-owned framing the gates applied stays intact.
def _fix_ch2(book):
    return book.replace(PRESENT_CH2, PAST[1])


def _untouched(book):
    return book


def _reword_ch2(book):
    return book.replace(PRESENT_CH2, PRESENT_CH2 + " Again.")


def _rewrite_all(book):
    return (book.replace(PAST[0], "Eun-soo counted the doors.")
                .replace(PRESENT_CH2, PAST[1])
                .replace(PAST[2], "Tae-jun signed and left."))


def _break_heading(book):
    import re
    return re.sub(r"(?m)^##\s+(Chapter|Bab)\s+2\b[^\n]*$", "## Bab 99", _fix_ch2(book))


@pytest.fixture
def loop(monkeypatch):
    """Drive the real gates and the real finaliser with scripted provider doubles."""
    live_lz, live_na = _live("laozhang_api"), _live("narration_api")
    seen = {"clients": 0, "critique_calls": 0, "revise_calls": 0}

    def blocked(*_a, **_k):
        seen["clients"] += 1
        raise RuntimeError("network blocked in test")

    def run(*, census_before, census_after, revise, mutate_between=None,
            mutate_between_fn=None, critic_enabled=True,
            teleports_before=(0, 0, 0), teleports_after=(0, 0, 0)):
        async def critique(*_a, **_k):
            seen["critique_calls"] += 1
            first = seen["critique_calls"] == 1
            census = census_before if first else census_after
            out = {"score": 8, "violations": []}
            if census is not None:
                out["tense_by_chapter"] = census
            # 🔴 EVERY OBSERVED CLASS IS ANSWERED ON EVERY CALL. F6 reads all three censuses
            # out of ONE critic answer, and a census it does not get is UNPROVED — so a
            # fixture that supplied only the tense list would block every case below for a
            # reason none of them is about.
            teleports = teleports_before if first else teleports_after
            if teleports is not None:
                out["teleports_by_chapter"] = list(teleports)
            return (out, 0)

        async def _revise(book, _request, *_a, **_k):
            # 🔴 THE STUB EDITS THE BOOK IT IS GIVEN, as a real revise does. Returning a
            # hardcoded string made every case a STRUCTURAL failure instead of the case it
            # meant to test: the gates localise headings (`## Chapter N` -> `## Bab N` for
            # `id`) before the repair runs, so a canned English-heading reply is a rewritten
            # heading — which the heading-identity rule correctly refuses.
            seen["revise_calls"] += 1
            return (revise(book), 0)

        async def cheap(*_a, **_k):
            return ("{}", 0)

        monkeypatch.setenv("NARASI_DIET_MAX_LOOPS", "0")
        monkeypatch.setenv("NARASI_REGISTER_GATE", "0")
        monkeypatch.setenv("NARASI_THREAD_TRACKER", "0")
        monkeypatch.setattr(live_lz, "make_narasi_client", blocked)
        monkeypatch.setattr(live_lz, "_narasi_cheap_call", cheap)
        monkeypatch.setattr(live_lz, "_narasi_critique_enabled", lambda: critic_enabled)
        monkeypatch.setattr(live_lz, "_narasi_critique_revise_enabled", lambda: True)
        monkeypatch.setattr(live_lz, "NARASI_CRITIQUE_MIN_CHAPTERS", 1)
        monkeypatch.setattr(live_lz, "_narasi_consistency_critique", critique)
        monkeypatch.setattr(live_lz, "_narasi_consistency_revise", _revise)

        result = {"ok": True, "book": DRIFTED,
                  "chapters": [{"no": i} for i in range(3)]}
        body = {"chapters": [{"word_target": 400}] * 3,
                "style": "storytelling", "language": "id"}
        asyncio.run(live_na._apply_v3_gates(result, body, tenant_id="t", user_id="u",
                                            job_uuid=None, sink=None, job_id="j"))
        assert "_f6_pending" in result, "F6 never recorded a pre-repair state"
        seen["blocks_before"] = list(result["_f6_pending"].get("blocks_before") or [])
        if mutate_between is not None:
            result["book"] = mutate_between
        if mutate_between_fn is not None:
            result["book"] = mutate_between_fn(result["book"])
        acct = asyncio.run(live_na._f6_finalize(result, body, tenant_id="t", user_id="u",
                                                job_uuid=None, sink=None, job_id="j"))
        assert seen["clients"] == 0, "a provider client was built — this is not offline"
        seen["result"], seen["acct"] = result, acct
        return seen

    return run


# ---------------------------------------------------------------------------
# Detection and routing
# ---------------------------------------------------------------------------
def test_the_tense_outlier_is_detected_and_routed_to_the_repair(loop):
    """🔴 THE CENSUS MUST BECOME A TARGETED VIOLATION. Chapter 2 is the defect, and the repair
    has to be told which chapter — a prose finding that names none cannot be pointed at."""
    seen = loop(census_before=["past", "present", "past"],
                census_after=["past", "past", "past"], revise=_fix_ch2)
    assert seen["revise_calls"] == 1, "the drift never reached a repair"
    assert seen["acct"]["violations_detected"] == 1
    assert seen["acct"]["violations_targeted"] == 1


def test_a_consistent_book_detects_nothing_and_costs_no_verification(loop):
    """The positive-negative pair's other half: no violation, no verification call, no block."""
    seen = loop(census_before=["past", "past", "past"],
                census_after=["past", "past", "past"], revise=_untouched)
    assert seen["acct"]["violations_detected"] == 0
    assert seen["acct"]["delivery_blocked"] is False
    assert seen["critique_calls"] == 1, "a verification pass was spent with nothing to verify"


# ---------------------------------------------------------------------------
# The positive path
# ---------------------------------------------------------------------------
def test_a_real_repair_resolves_and_delivery_is_allowed(loop):
    seen = loop(census_before=["past", "present", "past"],
                census_after=["past", "past", "past"], revise=_fix_ch2)
    acct = seen["acct"]
    assert acct["violations_resolved"] == 1
    assert acct["violations_unresolved"] == 0
    assert acct["delivery_blocked"] is False
    assert acct["chapters_changed"] == 1
    assert acct["collateral_chapters"] == []
    assert acct["provider_calls"] == 2, "one repair call + one bounded verification pass"


def test_the_untouched_chapters_come_back_byte_identical(loop):
    seen = loop(census_before=["past", "present", "past"],
                census_after=["past", "past", "past"], revise=_fix_ch2)
    ng = _live("narasi_gate")
    # Chapter blocks only: the gates prepend a `> **Gaya:** …` metadata header, which is
    # server-owned framing rather than a chapter.
    def chapters(text):
        blocks = ng.split_chapter_blocks(text)
        return blocks[1:] if blocks and not ng.chapter_heading_line(blocks[0]) else blocks
    def only_chapters(blocks):
        return blocks[1:] if blocks and not ng.chapter_heading_line(blocks[0]) else blocks
    before = only_chapters(seen["blocks_before"])
    after = chapters(seen["result"]["book"])
    assert len(before) == len(after) == 3
    assert before[0] == after[0] and before[2] == after[2]
    assert before[1] != after[1]


# ---------------------------------------------------------------------------
# The negative paths — each must block
# ---------------------------------------------------------------------------
def test_a_no_op_repair_is_unresolved_and_blocks_delivery(loop):
    """🔴 v9's EXACT FAILURE. The lane reports a revise; the bytes are the original."""
    seen = loop(census_before=["past", "present", "past"],
                census_after=["past", "present", "past"], revise=_untouched)
    acct = seen["acct"]
    assert acct["chapters_changed"] == 0
    assert acct["violations_resolved"] == 0
    assert acct["violations_unresolved"] == 1
    assert acct["delivery_blocked"] is True


def test_a_repair_that_changed_the_chapter_but_not_the_tense_still_blocks(loop):
    """"The chapter changed" is not "the defect is gone"."""
    seen = loop(census_before=["past", "present", "past"],
                census_after=["past", "present", "past"],
                revise=_reword_ch2)
    assert seen["acct"]["chapters_changed"] == 1
    assert seen["acct"]["violations_unresolved"] == 1
    assert seen["acct"]["delivery_blocked"] is True


def test_a_collateral_rewrite_blocks_even_though_the_target_reads_clean(loop):
    seen = loop(census_before=["past", "present", "past"],
                census_after=["past", "past", "past"],
                revise=_rewrite_all)
    assert seen["acct"]["collateral_chapters"] == [1, 3]
    assert seen["acct"]["violations_resolved"] == 0
    assert seen["acct"]["delivery_blocked"] is True


def test_a_repair_that_destroys_a_heading_blocks(loop):
    seen = loop(census_before=["past", "present", "past"],
                census_after=["past", "past", "past"],
                revise=_break_heading)
    assert seen["acct"]["chapters_changed"] == 0, "a structural break is not a chapter edit"
    assert seen["acct"]["delivery_blocked"] is True


def test_an_unreadable_verification_census_blocks(loop):
    seen = loop(census_before=["past", "present", "past"],
                census_after=None, revise=_fix_ch2)
    assert seen["acct"]["violations_unresolved"] == 1
    assert seen["acct"]["delivery_blocked"] is True


# ---------------------------------------------------------------------------
# The ordering requirement
# ---------------------------------------------------------------------------
def test_the_accounting_follows_a_mutation_made_after_the_gates_returned(loop):
    """🔴 THE POST-GATES DEDUP HAZARD, MADE CONCRETE. The gates hand back a book that looks
    repaired; something after them — in production, the dedup guard — changes it again. The
    accounting must describe the FINAL bytes, so this must block even though the repair the
    gates saw was perfect."""
    seen = loop(census_before=["past", "present", "past"],
                census_after=["past", "past", "past"], revise=_fix_ch2,
                mutate_between=DRIFTED)
    assert seen["acct"]["chapters_changed"] == 0, (
        "the change set was computed against the gates' book, not the delivered one")
    assert seen["acct"]["delivery_blocked"] is True


def test_a_post_gates_edit_to_another_chapter_shows_up_as_collateral(loop):
    """🔴 THE DISCRIMINATING WITNESS FOR "WHICH BYTES". Reverting the book between the calls
    blocks either way — a change set computed from the gates' book and one computed from the
    delivered book both come out empty — so it cannot tell the two apart. Here the repair is
    genuine AND something afterwards edits chapter 3: reading the final bytes reports
    `chapters_changed=2, collateral=[3]`; reading the gates' book reports `0` and `[]`. Same
    verdict, different books, and only the accounting says which one was measured."""
    seen = loop(census_before=["past", "present", "past"],
                census_after=["past", "past", "past"], revise=_fix_ch2,
                mutate_between_fn=lambda book: book.replace(
                    PAST[2], "Tae-jun signed and walked out into the rain."))
    acct = seen["acct"]
    assert acct["chapters_changed"] == 2, (
        "the accounting did not measure the delivered bytes")
    assert acct["collateral_chapters"] == [3]
    assert acct["delivery_blocked"] is True


# ---------------------------------------------------------------------------
# Default-on, and "we could not tell" is never "nothing was wrong"
# ---------------------------------------------------------------------------
def test_f6_is_on_unless_it_is_explicitly_switched_off(monkeypatch):
    """🔴 THE ORIGINAL F6 FINDING WAS "ENFORCEMENT FLAG DEFAULT OFF". Reading the legacy
    `NARASI_CRITIQUE_ENABLED` — itself defaulting to "0" — reproduced it exactly."""
    live_na = _live("narration_api")
    monkeypatch.delenv("NARASI_F6_ENABLED", raising=False)
    assert live_na._f6_enabled() is True
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("NARASI_F6_ENABLED", off)
        assert live_na._f6_enabled() is False, off
    monkeypatch.setenv("NARASI_F6_ENABLED", "1")
    assert live_na._f6_enabled() is True


def test_f6_does_not_inherit_the_legacy_critic_flag(monkeypatch):
    """The legacy critic can be off — it is, by default — and F6 must still run."""
    live_lz = _live("laozhang_api")
    monkeypatch.delenv("NARASI_CRITIQUE_ENABLED", raising=False)
    monkeypatch.delenv("NARASI_F6_ENABLED", raising=False)
    assert live_lz._narasi_critique_enabled() is False, (
        "this test is about the case where the legacy critic is OFF")
    assert _live("narration_api")._f6_enabled() is True


def test_f6_obtains_its_own_census_when_the_legacy_critic_never_ran(loop):
    """🔴 THE DEFAULT CONFIGURATION. `NARASI_CRITIQUE_ENABLED` defaults to "0", so on a stock
    deployment the legacy critic produces no verdict at all — and F6 inheriting that flag was
    the original "enforcement default OFF" finding. F6 asks for its own census with ONE bounded
    call, so the drift is still detected, repaired and verified with the critic switched off."""
    seen = loop(census_before=["past", "present", "past"],
                census_after=["past", "past", "past"], revise=_fix_ch2,
                critic_enabled=False)
    acct = seen["acct"]
    assert acct["violations_detected"] == 1, "F6 saw nothing without the legacy critic"
    assert acct["violations_resolved"] == 1
    assert acct["delivery_blocked"] is False
    assert acct["provider_calls"] == 3, (
        "F6's own census + the repair + the bounded verification pass")


def test_a_missing_census_blocks_instead_of_reading_as_a_clean_book(loop):
    """🔴 AUDIT REPRO. `census_before=None` produced `detected=0, unresolved=0,
    delivery_blocked=False` — a book nobody measured, delivered as if measured and clean."""
    seen = loop(census_before=None, census_after=None, revise=_untouched)
    acct = seen["acct"]
    assert acct["delivery_blocked"] is True
    assert acct["unproven"].startswith("tense_census_")


def test_a_malformed_census_blocks_too(loop):
    seen = loop(census_before=["past", "future", "past"], census_after=None,
                revise=_untouched)
    assert seen["acct"]["delivery_blocked"] is True
    assert seen["acct"]["unproven"] == "tense_census_invalid_value"


def test_a_detection_failure_blocks_rather_than_being_swallowed(loop, monkeypatch):
    """It used to log "non-fatal" and leave no pending state at all — and no pending state
    meant no accounting, which meant delivery."""
    live_ng = _live("narasi_gate")
    monkeypatch.setattr(live_ng, "tense_census",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    seen = loop(census_before=["past", "present", "past"],
                census_after=["past", "past", "past"], revise=_fix_ch2)
    assert seen["acct"]["delivery_blocked"] is True
    assert seen["acct"]["unproven"].startswith("detection_error:")


# ---------------------------------------------------------------------------
# The finaliser must be the LAST thing before the delivery decision
# ---------------------------------------------------------------------------
def test_the_finaliser_runs_after_every_manuscript_mutator():
    """🔴 STRUCTURAL, AND SAID SO. This reads the call order in the job function rather than
    executing it — the behavioural tests above prove what the finaliser DOES, this proves
    WHERE it is called, which no unit test of the finaliser can see.

    An earlier version ran it straight after the post-gates dedup guard, which is still two
    mutators too early: the canon-lite L3-assist repair replaces the manuscript and the F1
    scrub rewrites `result` again. A book accounted `resolved` there could have the drift
    reintroduced before persistence."""
    import ast
    import inspect
    live_na = _live("narration_api")
    tree = ast.parse(inspect.getsource(live_na._run_narration_job_after_parity))

    calls: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in ("_dedup_final", "_canon_lite_l3_assist_repair",
                        "scrub_and_verify_generation_leak", "_f6_finalize",
                        "_persist_chapters"):
                calls.setdefault(name, node.lineno)

    for mutator in ("_dedup_final", "_canon_lite_l3_assist_repair",
                    "scrub_and_verify_generation_leak"):
        assert mutator in calls, f"{mutator} vanished — this witness no longer guards anything"
        assert calls["_f6_finalize"] > calls[mutator], (
            f"F6 finalises BEFORE {mutator}, which can still change the manuscript")
    assert calls["_f6_finalize"] < calls["_persist_chapters"]


def test_the_private_pending_state_never_survives_the_finaliser(loop):
    """`_f6_pending` carries the whole pre-repair book; it must not reach persistence."""
    seen = loop(census_before=["past", "present", "past"],
                census_after=["past", "past", "past"], revise=_fix_ch2)
    assert "_f6_pending" not in seen["result"]
    assert "f6" in seen["result"]


def test_an_accounting_that_cannot_be_computed_blocks_rather_than_permits(loop, monkeypatch):
    """Fail closed: "we could not tell" is not "nothing was wrong"."""
    live_na = _live("narration_api")

    # 🔴 THIS ROW CALLS THE FINALISER BARE, so it must supply its own double. A structural
    # break counts as a mutation now — the final scan has to read the DELIVERED bytes, since a
    # book that lost a chapter with nothing detected used to sail straight through — so this
    # path reaches the critic where it previously did not.
    async def _offline_critique(*_a, **_k):
        # 🔴 THE CENSUSES MUST COME BACK CLEAN AND WELL-FORMED, or the final scan refuses on
        # its own account and this row stops being about the ACCOUNTING failing. `chapter_count`
        # below is 0, which is what makes `resolution_accounting` raise — the condition this
        # test exists for — and that line is only reached once the scan has nothing to say.
        return ({"score": 8, "violations": [], "tense_by_chapter": ["past"] * 3,
                 "teleports_by_chapter": [0] * 3}, 0)

    monkeypatch.setattr(_live("laozhang_api"), "_narasi_consistency_critique",
                        _offline_critique)
    result = {"ok": True, "book": DRIFTED,
              "_f6_pending": {"detected": [{"f6_class": "tense_drift", "chapter": 2}],
                              "targeted": [{"f6_class": "tense_drift", "chapter": 2}],
                              # 🔴 THE SNAPSHOT MATCHES THE DELIVERED BYTES, so the change set
                              # is a real (empty) set and the structural rule above does NOT
                              # fire. `chapter_count=0` is what `resolution_accounting` refuses,
                              # which is the raise this row is about — with `blocks_before=None`
                              # the structural refusal answers first and the except path is
                              # never reached.
                              "blocks_before": _live("narasi_gate").split_chapter_blocks(DRIFTED),
                              "census_before": None,
                              "chapter_count": 0, "repair_attempts": 0, "provider_calls": 0}}
    acct = asyncio.run(live_na._f6_finalize(result, {}, job_id="j"))
    assert acct["delivery_blocked"] is True
    assert "_f6_pending" not in result
