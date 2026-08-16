"""F6 final scan — the delivered bytes are re-checked for ALL FIVE classes, not just the ones
that were broken before the repair.

🔴 THE HOLE THIS FILE CLOSES. Verification was per-VIOLATION: each targeted defect got its own
   verifier, and each verifier refused a repair that moved ITS OWN defect somewhere else. Nothing
   asked whether the repair had introduced a defect of a DIFFERENT class. A revise that fixed the
   teleport in chapter 3 and rewrote chapter 2 into present tense produced:

       teleport:3 → resolved · nothing else detected · delivery ALLOWED

   The book shipped with tense drift that the pre-repair census did not have. Same shape for a
   beat that regressed, a chapter pushed over its ceiling, and — the one that matters most,
   because it is the only lane that edits prose directly — anything the ceiling reducer breaks.

🔴 SO THE SCAN RUNS AFTER EVERY MODEL-BASED MUTATION, OVER THE FINAL BYTES, FOR ALL FIVE CLASSES.
   Anything it finds that nobody repaired enters the books as UNRESOLVED and blocks delivery. A
   violation cannot be `resolved` by a repair that never targeted it.

Every row drives `_run_narration_job_after_parity`. Scripted provider doubles throughout;
`make_narasi_client` raises, so a real network call fails the test rather than escaping.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest


def _live(name):
    import importlib
    return sys.modules.get(name, importlib.import_module(name))


# ---------------------------------------------------------------------------
# A deliberately CLEAN three-chapter book: every defect below is introduced by a
# repair, never present at detection time.
# ---------------------------------------------------------------------------
def _pad(text: str, total: int) -> str:
    words = text.split()
    return " ".join(words[:total]) if len(words) >= total else \
        " ".join(words + ["lagi"] * (total - len(words)))


TITLES = ("Satu", "Dua", "Tiga")
B1 = _pad("Eun-soo menutup pintu terakhir dan menyusuri koridor panjang itu.", 20)
B2 = _pad("Cahaya kota jatuh pelan di jendela sementara Min-jae menunggu kabar.", 20)
B3 = _pad("Tae-jun menandatangani deposisi lalu ruangan itu kehilangan suaranya.", 20)
#: 150 words against a ceiling of 110 — a chapter the REPAIR pushed over the contract.
B3_BLOATED = _pad("Tae-jun menandatangani deposisi lalu ruangan itu kehilangan suaranya.", 150)
#: 200 words: the one chapter that starts over its ceiling, for the reducer rows.
B1_LONG = _pad("Eun-soo menutup pintu terakhir dan menyusuri koridor panjang itu.", 200)
B1_SHORT = _pad("Eun-soo menutup pintu dan menyusuri koridor.", 100)


def _book(bodies=(B1, B2, B3), titles=TITLES) -> str:
    return "\n\n".join(f"## Chapter {i}: {t}\n\n{b}"
                       for i, (t, b) in enumerate(zip(titles, bodies), 1))


OUTLINE = {1: ["Eun-soo returns"], 2: ["Min-jae waits"], 3: ["Tae-jun signs"]}


def _packet(beats) -> str:
    return ("CURRENT ORDERED OUTLINE BEATS:\n"
            + "".join(f"  {i}. {b}\n" for i, b in enumerate(beats, 1)) + "\n")


AUTHORITY = {"text": "NARRATIVE AUTHORITY: accepted outline.",
             "outline_packets_by_chapter": {str(k): _packet(v) for k, v in OUTLINE.items()}}

BEATS_OK = [{"chapter": c, "beat": 1, "state": "executed"} for c in (1, 2, 3)]
TENSE_OK = ["past", "past", "past"]
TELE_OK = [0, 0, 0]


def _beats_with(chapter, state):
    return [dict(e, state=state) if e["chapter"] == chapter else e for e in BEATS_OK]


@pytest.fixture
def job(monkeypatch):
    live_lz, live_na = _live("laozhang_api"), _live("narration_api")

    def blocked(*_a, **_k):
        raise AssertionError("a provider client was built — this is not offline")

    def run(*, book=None, word_targets=(100, 100, 100), authority=AUTHORITY,
            tense_before=TENSE_OK, tense_after=TENSE_OK,
            tele_before=TELE_OK, tele_after=TELE_OK,
            beats_before=BEATS_OK, beats_after=BEATS_OK,
            revise=lambda text: text, reduce_returns=None, critic_enabled=True,
            f6_enabled=None, skip_gates=False, chapters_in_result=3,
            post_gates_mutation=None, critic_violations=()):
        seen = {"finalize": [], "refund": 0, "persisted": None, "settle": 0,
                "critique": 0, "revise": 0, "reduce": 0, "f6": {}, "payload": None}

        async def _anoop(*_a, **_k):
            return None

        async def _finalize(job_id, job_uuid, tenant_id, *, status, result=None, error=None):
            seen["finalize"].append({"status": status, "error": error})
            if result is not None:
                seen["payload"] = dict(result)

        async def _refund(*_a, **_k):
            seen["refund"] += 1

        async def _settle(*_a, **_k):
            seen["settle"] += 1

        async def _persist(_tenant, _job_uuid, res, *_a, **_k):
            seen["persisted"] = dict(res or {})

        async def critique(*_a, authority_text="", **_k):
            seen["critique"] += 1
            first = seen["critique"] == 1
            out = {"score": 8, "violations": [dict(v) for v in critic_violations] if first else []}
            for key, value in (("tense_by_chapter", tense_before if first else tense_after),
                               ("teleports_by_chapter", tele_before if first else tele_after),
                               ("beat_states", beats_before if first else beats_after)):
                if key == "beat_states" and not str(authority_text or "").strip():
                    continue
                if value is not None:
                    out[key] = list(value)
            return (out, 0)

        async def _revise(text, _request, *_a, **_k):
            seen["revise"] += 1
            return (revise(text), 0)

        async def _reduce(chapter_text, *, target_words, **_k):
            seen["reduce"] += 1
            return (reduce_returns if reduce_returns is not None else chapter_text, 0)

        async def cheap(*_a, **_k):
            return ("{}", 0)

        async def _gen(_req, **_kw):
            res = {"ok": True, "book": book if book is not None else _book(),
                   "chapters": [{"no": i} for i in range(1, chapters_in_result + 1)]}
            if authority is not None:
                res["_narrative_authority"] = dict(authority)
            return res

        monkeypatch.setenv("NARASI_DIET_MAX_LOOPS", "0")
        monkeypatch.setenv("NARASI_REGISTER_GATE", "0")
        monkeypatch.setenv("NARASI_THREAD_TRACKER", "0")
        if f6_enabled is None:
            monkeypatch.delenv("NARASI_F6_ENABLED", raising=False)
        else:
            monkeypatch.setenv("NARASI_F6_ENABLED", f6_enabled)
        monkeypatch.setattr(live_lz, "make_narasi_client", blocked)
        monkeypatch.setattr(live_lz, "_narasi_cheap_call", cheap)
        monkeypatch.setattr(live_lz, "_narasi_critique_enabled", lambda: critic_enabled)
        monkeypatch.setattr(live_lz, "_narasi_critique_revise_enabled", lambda: True)
        monkeypatch.setattr(live_lz, "NARASI_CRITIQUE_MIN_CHAPTERS", 1)
        monkeypatch.setattr(live_lz, "_narasi_consistency_critique", critique)
        monkeypatch.setattr(live_lz, "_narasi_consistency_revise", _revise)
        monkeypatch.setattr(live_lz, "_narasi_chapter_reduce", _reduce)
        monkeypatch.setattr(live_na, "generate_narration", _gen)
        monkeypatch.setattr(live_na, "_cancel_watcher", lambda *a, **k: asyncio.sleep(3600))
        monkeypatch.setattr(live_na, "_finalize", _finalize)
        monkeypatch.setattr(live_na, "_set_status", _anoop)
        monkeypatch.setattr(live_na, "_safe_progress", _anoop)
        monkeypatch.setattr(live_na, "_settle", _settle)
        monkeypatch.setattr(live_na, "_refund", _refund)
        monkeypatch.setattr(live_na, "_reconcile_checkboxes", _anoop)
        monkeypatch.setattr(live_na, "_persist_chapters", _persist)
        monkeypatch.setattr(live_na, "credits_lib", types.SimpleNamespace(touch_hold=_anoop))
        monkeypatch.setattr(live_na, "db", types.SimpleNamespace(
            get_known_bad_claims=_anoop, get_known_good_claims=_anoop, log_usage=_anoop,
            checkpoint_narasi_meter=_anoop))
        if post_gates_mutation is not None:
            # 🔴 THE POST-GATES DEDUP GUARD — the mutator the ordering comment in the job body
            # names first, and the only one that runs UNCONDITIONALLY (the F1 scrub and the
            # canon-lite assist repair are both gated on `mode == "assist"`). It can replace
            # the manuscript after detection has finished looking at it, which is exactly the
            # "changed by something the detection never saw" case.
            import orchestrator.static as _ostatic

            def _dedup(text):
                return post_gates_mutation(text), 1

            monkeypatch.setattr(_ostatic, "_dedup_chapter_blocks", _dedup)
        if skip_gates:
            # 🔴 THE JOB THAT NEVER PROVED ANYTHING. Every canon-lite suite stubs the gates
            # like this; with F6 on, such a job has no pre-repair state at all.
            monkeypatch.setattr(live_na, "_apply_v3_gates", _anoop)

        _real = live_na._f6_finalize

        async def _spy(result, body, **kw):
            out = await _real(result, body, **kw)
            seen["f6"] = dict(out or {})
            return out

        monkeypatch.setattr(live_na, "_f6_finalize", _spy)

        async def drive():
            before = set(asyncio.all_tasks())
            await live_na._run_narration_job_after_parity(
                body={"chapters": [{"word_target": w} for w in word_targets],
                      "style": "storytelling", "language": "id"},
                job_id="j-scan", job_uuid=None, tenant_id="t", user_id="u",
                total=len(word_targets), meter_op="op", model="m",
                executor="narration_worker")
            survivors = (set(asyncio.all_tasks()) - before) - {asyncio.current_task()}
            if survivors:
                await asyncio.gather(*survivors, return_exceptions=True)

        asyncio.run(drive())
        return seen

    return run


def _statuses(seen):
    return [f["status"] for f in seen["finalize"]]


def _blocked(seen):
    na = _live("narration_api")
    assert _statuses(seen) == [na._STATUS_FAILED], _statuses(seen)
    assert seen["refund"] == 1
    assert seen["persisted"] is None
    assert seen["settle"] == 0


def _delivered(seen):
    na = _live("narration_api")
    assert na._STATUS_DONE in _statuses(seen), _statuses(seen)
    assert na._STATUS_FAILED not in _statuses(seen)
    assert seen["persisted"] is not None
    assert seen["refund"] == 0


# ---------------------------------------------------------------------------
# Control: the clean book still ships, and costs nothing extra
# ---------------------------------------------------------------------------
def test_a_clean_book_still_delivers_and_spends_one_call(job):
    """🔴 THE TRIPWIRE FOR EVERYTHING BELOW. A final scan that blocks a clean book would make
    every row here pass for the wrong reason."""
    seen = job()
    assert seen["f6"]["violations_detected"] == 0, seen["f6"]
    assert seen["f6"]["delivery_blocked"] is False
    assert seen["critique"] == 1, "a verification pass was spent on a book nobody touched"
    assert seen["revise"] == 0 and seen["reduce"] == 0
    _delivered(seen)


def test_a_detected_defect_that_is_genuinely_repaired_still_delivers(job):
    """The other positive control: the scan must not turn a real repair into a refusal."""
    seen = job(tele_before=[0, 0, 1], tele_after=TELE_OK,
               revise=lambda text: text.replace(B3, B3 + " Ia berjalan keluar."))
    assert seen["f6"]["violations_resolved"] == 1, seen["f6"]
    assert seen["f6"]["delivery_blocked"] is False
    _delivered(seen)


# ---------------------------------------------------------------------------
# 🔴 A repair that introduces a defect of ANOTHER class
# ---------------------------------------------------------------------------
def test_a_repair_that_introduces_tense_drift_elsewhere_blocks(job):
    """Detected: one teleport in chapter 3, and it IS repaired. But the same revise rewrote
    chapter 2 into present tense — a class nobody was verifying."""
    seen = job(tele_before=[0, 0, 1], tele_after=TELE_OK,
               tense_after=["past", "present", "past"],
               revise=lambda text: text.replace(B3, B3 + " Ia keluar.").replace(
                   B2, B2 + " Ia menunggu."))
    assert "tense_drift:2" in seen["f6"]["unresolved_ids"], seen["f6"]
    assert seen["f6"]["delivery_blocked"] is True
    _blocked(seen)


def test_a_repair_that_introduces_a_teleport_blocks(job):
    seen = job(tense_before=["past", "present", "past"], tense_after=TENSE_OK,
               tele_after=[0, 0, 1],
               revise=lambda text: text.replace(B2, B2 + " Ia menunggu.").replace(
                   B3, B3 + " Ia pergi."))
    assert any(i.startswith("teleport:3") for i in seen["f6"]["unresolved_ids"]), seen["f6"]
    assert seen["f6"]["delivery_blocked"] is True
    _blocked(seen)


def test_a_repair_that_regresses_an_outline_beat_blocks(job):
    """Every beat was executed before the repair. The revise dropped one — and no beat
    violation was ever targeted, so no beat verifier was ever consulted."""
    seen = job(tense_before=["past", "present", "past"], tense_after=TENSE_OK,
               beats_after=_beats_with(1, "absent"),
               revise=lambda text: text.replace(B2, B2 + " Ia menunggu.").replace(
                   B1, B1 + " Ia diam."))
    assert any(i.startswith("final_beat:") or i.startswith("beat_execution:")
               for i in seen["f6"]["unresolved_ids"]), seen["f6"]
    assert seen["f6"]["delivery_blocked"] is True
    _blocked(seen)


def test_a_repair_that_pushes_another_chapter_over_its_ceiling_blocks(job):
    """🔴 MEASURED, NOT OBSERVED. Chapter 3 goes from 20 words to 150 against a ceiling of
    110. Nothing about this needs a model to notice — but nothing was looking."""
    seen = job(tense_before=["past", "present", "past"], tense_after=TENSE_OK,
               revise=lambda text: text.replace(B2, B2 + " Ia menunggu.").replace(
                   B3, B3_BLOATED))
    assert "chapter_ceiling:3" in seen["f6"]["unresolved_ids"], seen["f6"]
    assert seen["f6"]["delivery_blocked"] is True
    _blocked(seen)


# ---------------------------------------------------------------------------
# 🔴 ...including a defect introduced by the ceiling reducer itself
# ---------------------------------------------------------------------------
def test_a_ceiling_reduction_that_introduces_tense_drift_blocks(job):
    """The reducer is the one lane that edits prose directly, one chapter at a time. It
    brought chapter 1 inside its ceiling and rewrote it into present tense doing so."""
    seen = job(book=_book((B1_LONG, B2, B3)), reduce_returns=B1_SHORT,
               tense_after=["present", "past", "past"])
    assert "chapter_ceiling:1" in seen["f6"]["resolved_ids"], seen["f6"]
    assert "tense_drift:1" in seen["f6"]["unresolved_ids"], seen["f6"]
    assert seen["f6"]["delivery_blocked"] is True
    _blocked(seen)


def test_a_ceiling_reduction_that_introduces_a_teleport_blocks(job):
    seen = job(book=_book((B1_LONG, B2, B3)), reduce_returns=B1_SHORT,
               tele_after=[1, 0, 0])
    assert any(i.startswith("teleport:1") for i in seen["f6"]["unresolved_ids"]), seen["f6"]
    assert seen["f6"]["delivery_blocked"] is True
    _blocked(seen)


def test_a_ceiling_reduction_that_is_genuinely_clean_still_delivers(job):
    seen = job(book=_book((B1_LONG, B2, B3)), reduce_returns=B1_SHORT)
    assert seen["f6"]["violations_resolved"] == 1, seen["f6"]
    assert seen["f6"]["delivery_blocked"] is False
    _delivered(seen)


# ---------------------------------------------------------------------------
# 🔴 `no_majority` is a book WITHOUT a dominant tense — that IS the drift
# ---------------------------------------------------------------------------
def test_a_book_with_no_dominant_tense_is_unproved_not_clean(job):
    """🔴 `tense_census` answers `valid=True, majority=None, outliers=[]` when the book splits
    evenly — correctly, since there is no minority to name. Reading that as "no outliers, so
    nothing is wrong" delivers a book that is half past and half present."""
    seen = job(tense_before=["past", "present", "mixed"])
    assert seen["f6"].get("unproven", "").startswith("tense_census_no_majority"), seen["f6"]
    assert seen["f6"]["delivery_blocked"] is True
    _blocked(seen)


def test_a_repair_that_leaves_the_book_with_no_dominant_tense_blocks(job):
    seen = job(tele_before=[0, 0, 1], tele_after=TELE_OK,
               tense_after=["past", "present", "mixed"],
               revise=lambda text: text.replace(B3, B3 + " Ia keluar."))
    assert seen["f6"]["delivery_blocked"] is True, seen["f6"]
    _blocked(seen)


# ---------------------------------------------------------------------------
# 🔴 The kill switch wraps the WHOLE lifecycle, not one call inside it
# ---------------------------------------------------------------------------
def test_the_kill_switch_disables_detection_routing_and_the_block(job):
    """🔴 AN EMERGENCY SWITCH THAT ONLY SILENCES PART OF THE MACHINE IS NOT A SWITCH. With
    `NARASI_F6_ENABLED=0` a book carrying every class must ship untouched: no detection, no
    pending state, no repair routed on F6's account, no accounting, no refusal."""
    seen = job(f6_enabled="0", book=_book((B1_LONG, B2, B3)),
               tense_before=["past", "present", "past"], tele_before=[0, 0, 1],
               beats_before=_beats_with(3, "absent"))
    assert seen["f6"] == {}, seen["f6"]
    assert seen["reduce"] == 0, "the ceiling reducer ran with F6 switched off"
    assert "f6" not in (seen["persisted"] or {}), "F6 published an accounting while off"
    _delivered(seen)


def test_the_kill_switch_spends_no_provider_call_of_its_own(job):
    """🔴 THE DISCRIMINATING MEASURE. `_f6_pending` is popped by the finaliser either way, so
    its absence proves nothing. What the switch must buy is that F6 stops COSTING anything: with
    the legacy critic off, an F6 that is off asks for no census at all."""
    seen = job(f6_enabled="0", critic_enabled=False,
               tense_before=["past", "present", "past"])
    assert seen["critique"] == 0, "F6 bought a census while switched off"
    assert "_f6_pending" not in (seen["persisted"] or {})
    _delivered(seen)


def test_f6_is_on_when_the_switch_is_absent(job):
    seen = job(tense_before=["past", "present", "past"], tense_after=TENSE_OK)
    assert seen["f6"].get("violations_detected") == 1, seen["f6"]


# ---------------------------------------------------------------------------
# 🔴 A one-chapter book is a book
# ---------------------------------------------------------------------------
def test_a_single_chapter_book_is_censused_and_delivered(job):
    """🔴 THE OWN-CENSUS CALL USED TO REQUIRE TWO CHAPTERS, so a one-chapter book got no
    census, which is UNPROVED, which blocks. Every single-chapter job refused.

    🔴 AND THE LEGACY CRITIC IS OFF HERE, WHICH IS ITS DEFAULT. With it on, its census covers
    for the missing own-census call and the floor is invisible — the row would pass against the
    unfixed code and prove nothing."""
    seen = job(critic_enabled=False, book=_book((B1,), titles=("Satu",)), word_targets=(100,),
               tense_before=["past"], tense_after=["past"], tele_before=[0], tele_after=[0],
               beats_before=[{"chapter": 1, "beat": 1, "state": "executed"}],
               beats_after=[{"chapter": 1, "beat": 1, "state": "executed"}],
               authority={"text": "NARRATIVE AUTHORITY: accepted outline.",
                          "outline_packets_by_chapter": {"1": _packet(["Eun-soo returns"])}},
               chapters_in_result=1)
    assert seen["f6"]["violations_detected"] == 0, seen["f6"]
    assert seen["f6"]["delivery_blocked"] is False
    _delivered(seen)


def test_a_single_chapter_book_over_its_ceiling_is_repaired_and_delivered(job):
    seen = job(critic_enabled=False, book=_book((B1_LONG,), titles=("Satu",)),
               word_targets=(100,),
               tense_before=["past"], tense_after=["past"], tele_before=[0], tele_after=[0],
               beats_before=[{"chapter": 1, "beat": 1, "state": "executed"}],
               beats_after=[{"chapter": 1, "beat": 1, "state": "executed"}],
               authority={"text": "NARRATIVE AUTHORITY: accepted outline.",
                          "outline_packets_by_chapter": {"1": _packet(["Eun-soo returns"])}},
               reduce_returns=B1_SHORT, chapters_in_result=1)
    assert seen["f6"]["violations_resolved"] == 1, seen["f6"]
    assert seen["f6"]["delivery_blocked"] is False
    _delivered(seen)


def test_a_single_chapter_book_with_a_defect_still_blocks(job):
    seen = job(critic_enabled=False, book=_book((B1,), titles=("Satu",)), word_targets=(100,),
               tense_before=["past"], tense_after=["past"], tele_before=[1], tele_after=[1],
               beats_before=[{"chapter": 1, "beat": 1, "state": "executed"}],
               beats_after=[{"chapter": 1, "beat": 1, "state": "executed"}],
               authority={"text": "NARRATIVE AUTHORITY: accepted outline.",
                          "outline_packets_by_chapter": {"1": _packet(["Eun-soo returns"])}},
               chapters_in_result=1)
    assert seen["f6"]["delivery_blocked"] is True, seen["f6"]
    _blocked(seen)


# ---------------------------------------------------------------------------
# 🔴 No pre-repair state, F6 on → refuse. F6 off → carry on.
# ---------------------------------------------------------------------------
def test_a_late_mutation_on_a_book_with_NOTHING_detected_still_blocks(job):
    """🔴 THE ROW THAT SEPARATES "a mutation happened" FROM "we already knew about a defect".
    Detection finds nothing — the book is clean — and then a post-gates mutator rewrites
    chapter 2 into present tense. Nothing was targeted, so a trigger that waits for a targeted
    violation never re-reads the manuscript and the drift ships. The trigger is the MUTATION."""
    seen = job(tense_after=["past", "present", "past"],
               post_gates_mutation=lambda text: text.replace(B2, B2 + " Ia menunggu."))
    assert seen["f6"]["violations_detected"] == 1, seen["f6"]
    assert "tense_drift:2" in seen["f6"]["unresolved_ids"], seen["f6"]
    assert seen["f6"]["delivery_blocked"] is True
    _blocked(seen)


def test_a_late_mutation_that_breaks_nothing_still_delivers(job):
    """Its positive control: a post-gates edit that leaves every class clean is not a refusal."""
    seen = job(post_gates_mutation=lambda text: text.replace(B2, B2 + " Ia menunggu."))
    assert seen["f6"]["violations_detected"] == 0, seen["f6"]
    assert seen["f6"]["delivery_blocked"] is False
    _delivered(seen)


def test_a_structural_break_with_a_detected_violation_blocks(job):
    """A chapter appears during the repair while a teleport was also being fixed. The change set
    is "cannot be determined", so nothing can be vouched for and the teleport stays unresolved.

    🔴 THIS ROW DOES NOT PROVE THE STRUCTURAL RULE. It has a pre-existing violation, so it would
    block on that alone. The row below is the one that isolates the structure."""
    seen = job(tele_before=[0, 0, 1], tele_after=[0, 0, 0, 0],
               tense_after=["past"] * 4, beats_after=BEATS_OK,
               revise=lambda text: text + "\n\n## Chapter 4: Empat\n\nsatu dua tiga\n")
    assert seen["f6"]["delivery_blocked"] is True, seen["f6"]
    _blocked(seen)


def test_a_structural_break_with_GENUINELY_nothing_detected_blocks(job):
    """🔴 THE FAIL-OPEN THE AUDIT FOUND. `changed is None` only TRIGGERED the scan; it did not
    refuse. A clean three-chapter book, a late mutator that appends a fourth, and a post-repair
    census that reads the four-chapter book as perfectly clean produced:

        detected=0 · unresolved=0 · delivery_blocked=False · status=DONE

    Nothing was wrong with any chapter — what was wrong was that the book delivered is not the
    book that was measured, and no per-chapter verdict can express that. A change set that
    cannot be determined is a STRUCTURAL failure in its own right."""
    seen = job(tense_after=["past"] * 4, tele_after=[0, 0, 0, 0],
               post_gates_mutation=lambda text: text + "\n\n## Chapter 4: Empat\n\nsatu dua tiga\n")
    assert seen["f6"]["delivery_blocked"] is True, seen["f6"]
    _blocked(seen)


def test_a_manuscript_short_of_the_requested_chapter_count_blocks(job):
    """🔴 THE REQUEST IS SERVER-OWNED AND NOBODY CHECKED IT. Four chapters were asked for and
    paid for; three arrived. Every per-chapter census agrees the three are clean, so the
    accounting said `0 detected` and the job finished DONE."""
    seen = job(word_targets=(100, 100, 100, 100), chapters_in_result=4)
    assert seen["f6"]["delivery_blocked"] is True, seen["f6"]
    _blocked(seen)


def test_an_outline_chapter_beyond_the_manuscript_is_never_remapped(job):
    """🔴 THE WORST OF THE FIVE. `min(outline_chapter, chapter_count)` mapped outline chapter 4
    onto manuscript chapter 3, so a `final_beat` the outline places in a chapter that does not
    exist got REPAIRED — and RESOLVED — by an edit to a different chapter:

        resolved_ids = ['final_beat:3:outline_beat:4|1'] · status=DONE

    A beat belongs to the chapter the outline puts it in. If the manuscript has no such chapter
    the violation cannot be repaired there, cannot be verified there, and must not be moved."""
    outline4 = {"text": "NARRATIVE AUTHORITY: accepted outline.",
                "outline_packets_by_chapter": {
                    **{str(k): _packet(v) for k, v in OUTLINE.items()},
                    "4": _packet(["Eun-soo decides, as an equal partner"])}}
    beats = BEATS_OK + [{"chapter": 4, "beat": 1, "state": "absent"}]
    seen = job(authority=outline4, beats_before=beats,
               beats_after=[dict(e, state="executed") for e in beats],
               revise=lambda text: text.replace(B3, B3 + " Ia memutuskan tinggal."))
    assert not any("outline_beat:4|1" in i for i in seen["f6"].get("resolved_ids") or []), (
        "an outline beat was resolved in a chapter the outline never placed it in", seen["f6"])
    assert seen["f6"]["delivery_blocked"] is True, seen["f6"]
    _blocked(seen)


# ---------------------------------------------------------------------------
# 🔴 The preamble: only the server's OWN framing may be normalised away
# ---------------------------------------------------------------------------
def test_an_injected_preamble_is_a_structural_failure(job):
    """🔴 THE COMPARATOR DROPPED THE WHOLE PREAMBLE ON BOTH SIDES. It exists to absorb the
    `> **Gaya:** …` header the gates add after the snapshot — but it absorbed ANY leading
    non-heading block, so a repair could prepend arbitrary text to the manuscript and the
    comparison would not see it. The tense repair resolved and the injected text shipped."""
    seen = job(tense_before=["past", "present", "past"], tense_after=TENSE_OK,
               revise=lambda text: "UNAUTHORIZED PREAMBLE\n\n" + text.replace(
                   B2, B2 + " Ia menunggu."))
    assert seen["f6"]["delivery_blocked"] is True, seen["f6"]
    assert "UNAUTHORIZED PREAMBLE" not in ((seen["persisted"] or {}).get("book") or "")
    _blocked(seen)


def test_the_server_owned_style_header_is_still_normalised(job):
    """🔴 THE POSITIVE CONTROL, AND THE REASON THE RULE IS NOT SIMPLY "ANY PREAMBLE BLOCKS".
    The gates prepend `> **Gaya:** …` to the assembled book AFTER the pre-repair snapshot is
    taken, so that header legitimately exists on one side only. Refusing it would block every
    real job — which is exactly what the first version of this comparator did."""
    seen = job(tense_before=["past", "present", "past"], tense_after=TENSE_OK,
               revise=lambda text: text.replace(B2, B2 + " Ia menunggu."))
    assert seen["f6"]["violations_resolved"] == 1, seen["f6"]
    assert seen["f6"]["delivery_blocked"] is False
    _delivered(seen)


# ---------------------------------------------------------------------------
# 🔴 Heading grammar: a keyword is not a heading
# ---------------------------------------------------------------------------
def test_prose_that_begins_with_the_chapter_keyword_is_not_a_heading(job):
    """🔴 `"Bab ini dimulai dengan tenang."` MATCHED THE HEADING PATTERN. The grammar asked for
    a keyword and then accepted free text, so an ordinary Indonesian sentence opening with
    "Bab ini" split a chapter in half — every count, every census index and every repair target
    downstream of it pointing at the wrong place."""
    body = "Bab ini dimulai dengan tenang dan tidak ada yang berubah sama sekali di sana."
    seen = job(book=_book((B1, body, B3)))
    assert seen["f6"]["violations_detected"] == 0, seen["f6"]
    assert seen["f6"]["delivery_blocked"] is False, seen["f6"]
    _delivered(seen)


def test_a_heading_with_no_ordinal_blocks(job):
    """🔴 THE DEFECT IS IN THE BOOK AS GENERATED, and that is what makes this row discriminating.
    A malformed heading INTRODUCED by a repair blocks anyway — the heading-identity rule sees it
    change — so it proves nothing about the grammar. Here nothing changes at all: the book
    arrives with `## Bab nonsense` and every census reads clean, so only a grammar that demands
    a real ordinal can refuse it."""
    seen = job(book=_book((B1, B2, B3)).replace("## Chapter 3: Tiga", "## Chapter nonsense"))
    assert seen["f6"]["delivery_blocked"] is True, seen["f6"]
    _blocked(seen)


def test_headings_out_of_sequence_block(job):
    """Same shape: the book is DELIVERED with its chapters numbered 1, 3, 2. Nothing is
    repaired, nothing changes, every chapter reads clean — and the manuscript is still
    structurally broken."""
    seen = job(book=_book((B1, B2, B3)).replace("## Chapter 2: Dua", "## Chapter 3: Dua")
                                       .replace("## Chapter 3: Tiga", "## Chapter 2: Tiga"))
    assert seen["f6"]["delivery_blocked"] is True, seen["f6"]
    _blocked(seen)


# ---------------------------------------------------------------------------
# 🔴 One revise, several gates: another gate's edit is not collateral
# ---------------------------------------------------------------------------
def test_a_chapter_another_gate_targeted_is_not_collateral(job):
    """🔴 F6 SHARES THE MERGED REVISE WITH F1–F5, AND THE ALLOWED SET CAME ONLY FROM F6's OWN
    TARGETS. So a legitimate edit the consistency critic asked for in chapter 3 read as
    collateral, no resolution was permitted, and a job where BOTH repairs worked was refused.
    A chapter some gate asked for is not a chapter nobody asked for."""
    seen = job(tense_before=["past", "present", "past"], tense_after=TENSE_OK,
               critic_violations=[{"type": "timeline", "severity": "high", "chapter": 3,
                                   "evidence": "the deposition date contradicts chapter 1",
                                   "fix": "make the date agree with chapter 1"}],
               revise=lambda text: text.replace(B2, B2 + " Ia menunggu.").replace(
                   B3, B3 + " Tanggalnya diperbaiki."))
    assert seen["f6"]["collateral_chapters"] == [], seen["f6"]
    assert seen["f6"]["violations_resolved"] == 1, seen["f6"]
    assert seen["f6"]["delivery_blocked"] is False, seen["f6"]
    _delivered(seen)


def test_a_job_that_never_recorded_a_pre_repair_state_is_refused(job):
    """🔴 "NOBODY MEASURED THIS BOOK" IS NOT "THIS BOOK IS CLEAN". The finaliser used to return
    an empty accounting when `_f6_pending` was absent, and an empty accounting has no
    `delivery_blocked`, so a job that skipped the gates entirely delivered with no F6 proof at
    all — the same fail-open the UNPROVED rule exists to close, one level up."""
    seen = job(skip_gates=True)
    assert seen["f6"].get("delivery_blocked") is True, seen["f6"]
    assert "pending" in str(seen["f6"].get("unproven", "")), seen["f6"]
    _blocked(seen)


def test_a_job_with_no_pre_repair_state_is_fine_when_f6_is_off(job):
    seen = job(skip_gates=True, f6_enabled="0")
    assert seen["f6"] == {}, seen["f6"]
    _delivered(seen)


# ---------------------------------------------------------------------------
# 🔴 The accounting has to survive into the durable payload
# ---------------------------------------------------------------------------
def test_the_accounting_reaches_the_durable_result_payload(job):
    """🔴 `_result_payload` IS AN EXPLICIT ALLOWLIST. The accounting was published on `result`
    and dropped on the way to the jobs row, so an operator asking "why was this refused?" had
    the answer computed, logged, and then discarded."""
    seen = job(tense_before=["past", "present", "past"], tense_after=TENSE_OK,
               revise=lambda text: text.replace(B2, B2 + " Ia menunggu."))
    f6 = (seen["payload"] or {}).get("f6")
    assert f6, seen["payload"]
    assert f6["violations_detected"] == 1
    assert f6["violations_resolved"] == 1
    assert f6["delivery_blocked"] is False


def test_the_refusal_reaches_the_durable_result_payload(job):
    seen = job(tense_before=["past", "present", "past"],
               tense_after=["past", "present", "past"])
    f6 = (seen["payload"] or {}).get("f6")
    assert f6, seen["payload"]
    assert f6["delivery_blocked"] is True
    assert f6["violations_unresolved"] == 1


def test_the_persisted_accounting_is_bounded(job):
    """🔴 BOUNDED, LIKE EVERY OTHER BLOCK IN THAT PAYLOAD. The in-process accounting carries a
    list per verdict; a pathological book must not put an unbounded list on the jobs row."""
    na = _live("narration_api")
    huge = {"violations_detected": 900, "violations_targeted": 900, "repair_attempts": 1,
            "provider_calls": 2, "chapters_changed": 900, "violations_resolved": 0,
            "violations_unresolved": 900, "violations_unidentifiable": 0,
            "collateral_chapters": list(range(900)), "delivery_blocked": True,
            "resolved_ids": [f"tense_drift:{i}" for i in range(900)],
            "unresolved_ids": [f"teleport:{i}:x" * 50 for i in range(900)],
            "unproven": "x" * 5000}
    payload = na._result_payload({"book": "b", "f6": huge})
    f6 = payload["f6"]
    assert f6["violations_detected"] == 900, "the counters must survive"
    assert len(f6.get("unresolved_ids", [])) <= 20, f6
    assert len(f6.get("collateral_chapters", [])) <= 20, f6
    assert len(str(f6.get("unproven", ""))) <= 200, f6
    assert all(len(i) <= 120 for i in f6.get("unresolved_ids", [])), f6


def test_no_accounting_means_no_f6_key_in_the_payload(job):
    """Same "added only when there is one" rule its neighbours follow: a flag-off job must not
    grow a `f6: null` on its persisted row."""
    na = _live("narration_api")
    assert "f6" not in na._result_payload({"book": "b"})
    assert "f6" not in na._result_payload({"book": "b", "f6": {}})
