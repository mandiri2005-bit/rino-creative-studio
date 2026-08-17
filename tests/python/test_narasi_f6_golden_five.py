"""F6 golden — ONE synthetic book carrying all FIVE hard-violation classes at once.

🔴 WHY A SINGLE BOOK WITH FIVE DEFECTS AND NOT FIVE BOOKS WITH ONE. Each class was provable on
   its own long before the loop was safe: what was not provable is that the classes SHARE one
   set of books. A per-class test cannot see a partition that adds up per class and loses a
   violation across classes, a change set that is right for the repair it was written for and
   collateral for the one beside it, or a verification pass spent five times because nobody
   asked whether the observations could come from one read. All of that only shows up when the
   five run together.

🔴 AND THIS DRIVES `_run_narration_job_after_parity`, NOT THE HELPERS. `_apply_v3_gates` plus
   `_f6_finalize` called by hand is a shape the production job does not have: between them sit
   the post-gates dedup guard, the canon-lite L3-assist repair and the F1 scrub, and the whole
   ordering defect this workstream kept re-introducing lives in that gap. The job body is
   entered here exactly as the worker enters it, and what is asserted is what the job DID —
   persisted or not, DONE or FAILED, refunded or settled.

Scripted provider doubles throughout; `make_narasi_client` raises, so a real network call fails
the test rather than escaping.
"""
from __future__ import annotations

import asyncio
import re
import sys
import types

import pytest


def _live(name):
    import importlib
    return sys.modules.get(name, importlib.import_module(name))


# ---------------------------------------------------------------------------
# The book: five chapters, five defects, one of them measured rather than observed
# ---------------------------------------------------------------------------
def _pad(text: str, total: int) -> str:
    """Exactly `total` whitespace-separated tokens, so word counts are arithmetic, not luck."""
    words = text.split()
    if len(words) >= total:
        return " ".join(words[:total])
    return " ".join(words + ["lagi"] * (total - len(words)))


#: Chapter 1 is 60 words against a 40-word target — a ceiling of 44. Nothing about this defect
#: involves a model: the server counts the words on both sides.
LONG1 = _pad("Eun-soo kembali ke kantor lama itu dan menghitung pintu yang sudah ia tutup.", 60)
SHORT1 = _pad("Eun-soo kembali ke kantor lama dan menghitung pintu yang ia tutup.", 40) + "."
STILL_LONG1 = _pad("Eun-soo kembali ke kantor lama itu dan menghitung pintu yang ia tutup.", 55) + "."

B2_DRIFT = _pad("Cahaya kota memenuhi jendela sementara Min-jae duduk dan menunggu kabar itu.", 30)
B2_FIXED = _pad("Cahaya kota memenuhi jendela sementara Min-jae duduk dan menunggu kabar tersebut.", 30)
B3_TELEPORT = _pad("Min-jae ada di lobi. Kemudian ia sudah berada di ruang dewan tanpa jeda.", 30)
B3_FIXED = _pad("Min-jae menyeberangi lobi, naik lift, lalu masuk ke ruang dewan itu.", 30)
B4_PROMISE = _pad("Tae-jun berkata ia akan memberikan deposisi begitu semua berkas siap nanti.", 30)
B4_FIXED = _pad("Tae-jun menandatangani deposisi di depan notaris dan Eun-soo memilih tinggal.", 30)
B5_CLEAN = _pad("Ruangan itu kosong dan hujan turun pelan di luar jendela kaca.", 30)

BODIES = (LONG1, B2_DRIFT, B3_TELEPORT, B4_PROMISE, B5_CLEAN)
TITLES = ("Kembali", "Jendela", "Dewan", "Deposisi", "Hujan")


def _book(bodies=BODIES) -> str:
    return "\n\n".join(f"## Chapter {i}: {t}\n\n{b}"
                       for i, (t, b) in enumerate(zip(TITLES, bodies), 1))


#: The ACCEPTED OUTLINE this process rendered — the keyspace the beat census is validated
#: against, and the only source of the two beat violations' identities. Outline chapter 4 is the
#: last one, so its last beat is the `final_beat` and everything else unexecuted is
#: `beat_execution`.
OUTLINE = {1: ["Eun-soo returns"], 2: ["Min-jae waits"], 3: ["The board convenes"],
           4: ["Tae-jun executes the deposition", "The recordings are ruled inadmissible",
               "Eun-soo decides to stay, as an equal partner"]}


def _packet(beats) -> str:
    return ("CURRENT ORDERED OUTLINE BEATS:\n"
            + "".join(f"  {i}. {b}\n" for i, b in enumerate(beats, 1)) + "\n")


AUTHORITY = {"text": "NARRATIVE AUTHORITY: accepted outline.",
             "outline_packets_by_chapter": {str(k): _packet(v) for k, v in OUTLINE.items()}}

#: Chapter 1 carries a 40-word target so its ceiling is 44 and its floor 36; the rest are given
#: room, so `chapter_ceiling` names exactly one chapter.
BODY = {"chapters": [{"word_target": 40}] + [{"word_target": 100}] * 4,
        "style": "storytelling", "language": "id"}

BEATS_DIRTY = [{"chapter": 1, "beat": 1, "state": "executed"},
               {"chapter": 2, "beat": 1, "state": "executed"},
               {"chapter": 3, "beat": 1, "state": "executed"},
               {"chapter": 4, "beat": 1, "state": "promised"},    # beat_execution
               {"chapter": 4, "beat": 2, "state": "executed"},
               {"chapter": 4, "beat": 3, "state": "absent"}]      # final_beat
BEATS_CLEAN = [dict(e, state="executed") for e in BEATS_DIRTY]
TENSE_DIRTY = ["past", "present", "past", "past", "past"]
TENSE_CLEAN = ["past"] * 5
TELE_DIRTY = [0, 0, 1, 0, 0]
TELE_CLEAN = [0, 0, 0, 0, 0]


def _repair(book: str) -> str:
    """What a working revise does to the book it is HANDED — never a canned reply. The gates
    localise `## Chapter N` to `## Bab N` for `language="id"` before the repair runs, so a
    hardcoded English-heading answer is a rewritten heading, which the heading-identity rule
    correctly refuses."""
    return (book.replace(B2_DRIFT, B2_FIXED)
                .replace(B3_TELEPORT, B3_FIXED)
                .replace(B4_PROMISE, B4_FIXED))


# ---------------------------------------------------------------------------
# The harness: the real job body, the real gates, the real finaliser
# ---------------------------------------------------------------------------
@pytest.fixture
def golden(monkeypatch):
    live_lz, live_na = _live("laozhang_api"), _live("narration_api")

    def blocked(*_a, **_k):
        raise AssertionError("a provider client was built — this is not offline")

    def run(*, tense_after=TENSE_CLEAN, tele_after=TELE_CLEAN, beats_after=BEATS_CLEAN,
            revise=_repair, reduce_to=SHORT1, revise_raises=False, reduce_raises=False,
            reduce_returns=None, reduce_sequence=None, book=None, tense_before=TENSE_DIRTY,
            tele_before=TELE_DIRTY, beats_before=BEATS_DIRTY, authority=AUTHORITY,
            body=None, beat_returns=None, beat_sequence=None, beat_raises=False):
        seen = {"finalize": [], "refund": 0, "persisted": None, "settle": 0,
                "critique": 0, "revise": 0, "reduce": 0, "reduce_targets": [],
                "reduce_corrections": [], "reduce_required_beats": [],
                "beat": 0, "beat_calls": [], "final_book": None}

        async def _anoop(*_a, **_k):
            return None

        async def _finalize(job_id, job_uuid, tenant_id, *, status, result=None, error=None):
            seen["finalize"].append({"status": status, "error": error, "result": result})

        async def _refund(*_a, **_k):
            seen["refund"] += 1

        async def _settle(*_a, **_k):
            seen["settle"] += 1

        async def _persist(_tenant, _job_uuid, res, *_a, **_k):
            seen["persisted"] = dict(res or {})

        async def critique(*_a, authority_text="", **_k):
            seen["critique"] += 1
            first = seen["critique"] == 1
            # F8: a clean SEAM census. These fixtures drive the real job, and F8 refuses a
            # multi-chapter book whose seam census is absent — UNPROVED blocks, exactly as
            # every other F6 census does. Declaring a sound census is what these rows always
            # did for tense and teleports; F8 is simply the fourth of them.
            _n_seams = max(len(tense_before or ()) - 1, 0)
            out = {"score": 8, "violations": [],
                   "seam_states": [{"chapter_a": _i, "chapter_b": _i + 1,
                                    "causal": "explicit", "location": "continuous",
                                    "time": "explicit"}
                                   for _i in range(1, _n_seams + 1)]}
            for key, value in (("tense_by_chapter", tense_before if first else tense_after),
                               ("teleports_by_chapter", tele_before if first else tele_after),
                               ("beat_states", beats_before if first else beats_after)):
                # 🔴 THE DOUBLE HONOURS THE PROMPT CONTRACT IT IS STANDING IN FOR. Section 6d
                # only exists when a NARRATIVE AUTHORITY is supplied, so a critic asked without
                # one cannot answer about beats. A double that returned them anyway would make
                # the authority argument unobservable — and a call that quietly stopped passing
                # it would verify every beat violation against an absent census while every
                # test stayed green.
                if key == "beat_states" and not str(authority_text or "").strip():
                    continue
                if value is not None:
                    out[key] = list(value)
            return (out, 0)

        async def _revise(text, _request, *_a, **_k):
            seen["revise"] += 1
            if revise_raises:
                raise RuntimeError("revise provider exploded")
            return (revise(text), 0)

        async def _reduce(chapter_text, *, target_words, minimum_words=0,
                          correction="", **_k):
            seen["reduce"] += 1
            seen["reduce_targets"].append(
                (len(chapter_text.split()), minimum_words, target_words))
            seen["reduce_corrections"].append(correction)
            seen["reduce_required_beats"].append(_k.get("required_beats"))
            if reduce_raises:
                raise RuntimeError("reducer provider exploded")
            if reduce_sequence is not None:
                return (reduce_sequence[seen["reduce"] - 1], 0)
            if reduce_returns is not None:
                return (reduce_returns, 0)
            return (reduce_to, 0)

        async def _beat_repair(chapter_body, *, missing_beat, beat_number, chapter_number,
                               required_beats="", correction="", previous_tail="",
                               word_ceiling=0, **_k):
            """Provider double for the DEDICATED `beat_execution` actuator.

            🔴 NEVER A CANNED REPLY. It enacts the beat inside the body it was HANDED, so the
            splice, the word band and the fidelity check are exercised against real bytes —
            and so a lane that stopped passing the current chapter would be observable."""
            seen["beat"] += 1
            seen["beat_calls"].append({
                "words": len(chapter_body.split()), "missing_beat": missing_beat,
                "beat_number": beat_number, "chapter_number": chapter_number,
                "required_beats": required_beats, "correction": correction,
                "previous_tail": previous_tail, "word_ceiling": word_ceiling})
            if beat_raises:
                raise RuntimeError("beat actuator provider exploded")
            if beat_sequence is not None:
                return (beat_sequence[min(seen["beat"] - 1, len(beat_sequence) - 1)], 0)
            if beat_returns is not None:
                return (beat_returns, 0)
            return (chapter_body.rstrip().rstrip(".")
                    + " Tae-jun menandatangani deposisi itu di depan notaris.", 0)

        async def cheap(*_a, **_k):
            return ("{}", 0)

        async def _gen(_req, **_kw):
            res = {"ok": True, "book": book if book is not None else _book(),
                   "chapters": [{"no": i} for i in range(1, 6)]}
            if authority is not None:
                res["_narrative_authority"] = dict(authority)
            return res

        monkeypatch.setenv("NARASI_DIET_MAX_LOOPS", "0")
        monkeypatch.setenv("NARASI_REGISTER_GATE", "0")
        monkeypatch.setenv("NARASI_THREAD_TRACKER", "0")
        monkeypatch.delenv("NARASI_F6_ENABLED", raising=False)
        monkeypatch.setattr(live_lz, "make_narasi_client", blocked)
        monkeypatch.setattr(live_lz, "_narasi_cheap_call", cheap)
        monkeypatch.setattr(live_lz, "_narasi_critique_enabled", lambda: True)
        monkeypatch.setattr(live_lz, "_narasi_critique_revise_enabled", lambda: True)
        monkeypatch.setattr(live_lz, "NARASI_CRITIQUE_MIN_CHAPTERS", 1)
        monkeypatch.setattr(live_lz, "_narasi_consistency_critique", critique)
        monkeypatch.setattr(live_lz, "_narasi_consistency_revise", _revise)
        monkeypatch.setattr(live_lz, "_narasi_chapter_reduce", _reduce)
        monkeypatch.setattr(live_lz, "_narasi_beat_execution_repair", _beat_repair)
        # 🔴 A SPY, NOT A SUBSTITUTE. The REAL finaliser runs and the real hard block reads its
        # real return value; this only records what it produced. Reading the accounting off the
        # persisted row instead would make every blocked case unobservable — a blocked job
        # persists nothing, which is the point.
        _real_f6_finalize = live_na._f6_finalize

        async def _spy_f6_finalize(result, body, **kw):
            out = await _real_f6_finalize(result, body, **kw)
            seen["f6"] = dict(out or {})
            seen["final_book"] = result.get("book") or result.get("output")
            return out

        monkeypatch.setattr(live_na, "_f6_finalize", _spy_f6_finalize)
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

        async def drive():
            before = set(asyncio.all_tasks())
            await live_na._run_narration_job_after_parity(
                body=dict(body if body is not None else BODY), job_id="j-golden",
                job_uuid=None, tenant_id="t", user_id="u", total=5, meter_op="op",
                model="m", executor="narration_worker")
            survivors = (set(asyncio.all_tasks()) - before) - {asyncio.current_task()}
            if survivors:
                await asyncio.gather(*survivors, return_exceptions=True)

        seen["f6"] = {}
        asyncio.run(drive())
        return seen

    return run


def _statuses(seen):
    return [f["status"] for f in seen["finalize"]]


def _blocked(seen):
    """The four things that together constitute a refusal — asserted as one, because a job that
    fails three of them and persists anyway has not refused."""
    na = _live("narration_api")
    assert _statuses(seen) == [na._STATUS_FAILED], _statuses(seen)
    assert seen["finalize"][0]["error"] == "f6_unresolved_hard_violation"
    assert seen["refund"] == 1
    assert seen["persisted"] is None
    assert seen["settle"] == 0


# ---------------------------------------------------------------------------
# Detection: all five, each targeted, each identified exactly once
# ---------------------------------------------------------------------------
def test_all_five_classes_are_detected_on_one_book(golden):
    seen = golden(tense_after=TENSE_DIRTY)      # blocks; detection is what is asserted
    assert seen["f6"]["violations_detected"] == 5, seen["f6"]
    assert seen["f6"]["violations_unidentifiable"] == 0


def test_two_teleports_in_one_chapter_are_two_violations(golden):
    """🔴 AUDIT FINDING #2, ON THE JOB PATH. `class + chapter` collapsed two different teleports
    in chapter 3 into one `teleport:3`: `detected` fell from two to one, the second defect left
    the books entirely, and resolving the first opened delivery. The census reports a COUNT, so
    the server enumerates it with its own ordinal — six detections, not five."""
    seen = golden(tele_before=[0, 0, 2, 0, 0], tense_after=TENSE_DIRTY)
    assert seen["f6"]["violations_detected"] == 6, seen["f6"]
    assert sum(1 for i in seen["f6"]["unresolved_ids"] + seen["f6"]["resolved_ids"]
               if i.startswith("teleport:")) == 2, seen["f6"]


def test_both_teleports_in_a_chapter_stand_or_fall_together(golden):
    """A count that merely fell leaves a teleport in the delivered book, so neither instance
    may resolve — the conservative reading, and the only one that cannot ship a defect."""
    seen = golden(tele_before=[0, 0, 2, 0, 0], tele_after=[0, 0, 1, 0, 0])
    assert [i for i in seen["f6"]["unresolved_ids"] if i.startswith("teleport:")] == [
        "teleport:3:teleport_instance:3|1", "teleport:3:teleport_instance:3|2"], seen["f6"]
    _blocked(seen)


#: The same book with TWO promised beats in outline chapter 4 — two violations of the SAME
#: class, which is the only shape where a collapsed claim is visible. Two violations of
#: DIFFERENT classes keep distinct identities even when their claims are identical, so a
#: mutant that guts the claim survives a mixed pair while still losing a real violation.
BEATS_TWO_PROMISED = [dict(e, state="promised") if (e["chapter"], e["beat"]) == (4, 2) else e
                      for e in BEATS_DIRTY]


def test_two_unexecuted_beats_in_one_chapter_are_two_violations(golden):
    """🔴 THE BEAT CLAIM MUST COME FROM THE BOUNDED REFERENCE, NOT FROM PROSE. Both violations
    sit in outline chapter 4 and both are `beat_execution`; only `outline_beat:4|1` versus
    `outline_beat:4|2` tells them apart. Build that token from an evidence field instead and
    the two collapse into one — `detected` falls, the second beat leaves the books, and
    resolving the first opens delivery."""
    seen = golden(beats_before=BEATS_TWO_PROMISED, tense_after=TENSE_DIRTY)
    assert seen["f6"]["violations_detected"] == 6, seen["f6"]
    ids = seen["f6"]["unresolved_ids"] + seen["f6"]["resolved_ids"]
    assert sorted(i for i in ids if i.startswith("beat_execution:")) == [
        "beat_execution:4:outline_beat:4|1",
        "beat_execution:4:outline_beat:4|2"], seen["f6"]


def test_all_five_classes_are_targeted_not_merely_counted(golden):
    seen = golden(tense_after=TENSE_DIRTY)
    assert seen["f6"]["violations_targeted"] == 5, seen["f6"]


def test_the_five_identities_name_the_five_classes(golden):
    """🔴 EACH CLASS IS ITS OWN VIOLATION. Five detections that collapsed into three identities
    would still add up — and would ship two defects."""
    seen = golden(tense_after=TENSE_DIRTY, tele_after=TELE_DIRTY, beats_after=BEATS_DIRTY,
                  reduce_returns=STILL_LONG1)
    classes = {i.split(":")[0] for i in seen["f6"]["unresolved_ids"]}
    assert classes == {"tense_drift", "teleport", "final_beat", "beat_execution",
                       "chapter_ceiling"}, seen["f6"]["unresolved_ids"]


# ---------------------------------------------------------------------------
# The positive path — all five repaired, and the book is delivered
# ---------------------------------------------------------------------------
def test_all_five_repaired_persists_and_finalises_DONE(golden):
    seen = golden()
    na = _live("narration_api")
    assert seen["f6"]["violations_resolved"] == 5, seen["f6"]
    assert seen["f6"]["violations_unresolved"] == 0
    assert seen["f6"]["delivery_blocked"] is False
    assert na._STATUS_DONE in _statuses(seen)
    assert na._STATUS_FAILED not in _statuses(seen)
    assert seen["persisted"] is not None
    assert seen["refund"] == 0
    assert seen["settle"] == 1


def test_the_positive_path_spends_exactly_one_verification_pass(golden):
    """One detection read and one bounded verification for all three censuses.

    Repair itself now has three bounded dispatches: the mixed F6/F8 lane, the isolated cheap
    Claude teleport lane, and the dedicated `beat_execution` actuator.  Splitting the actuator
    must never multiply the expensive final read.
    """
    seen = golden()
    assert seen["critique"] == 2, "detection + verification, not one call per class"
    assert seen["revise"] == 2, "one mixed repair plus one cheap teleport repair"
    assert seen["reduce"] == 1
    assert seen["beat"] == 1, "one bounded dispatch for the one beat_execution violation"
    assert seen["f6"]["provider_calls"] == 5, \
        ("mixed repair + teleport repair + beat repair + ceiling reduction + "
         "final verification")


def test_the_untargeted_chapter_comes_back_byte_identical(golden):
    seen = golden()
    assert B5_CLEAN in (seen["persisted"] or {}).get("book", "")
    assert seen["f6"]["collateral_chapters"] == []


def test_the_private_pending_state_is_never_persisted(golden):
    seen = golden()
    assert "_f6_pending" not in (seen["persisted"] or {})


def test_a_clean_book_finishes_without_a_repair_or_a_false_block(golden):
    """🔴 THE OTHER HALF OF EVERY GATE. Blocking everything satisfies the invariant and helps
    nobody; a book with no defects must reach DONE having spent no repair at all."""
    seen = golden(book=_book((SHORT1, B2_FIXED, B3_FIXED, B4_FIXED, B5_CLEAN)),
                  tense_before=TENSE_CLEAN, tele_before=TELE_CLEAN, beats_before=BEATS_CLEAN)
    na = _live("narration_api")
    assert seen["f6"]["violations_detected"] == 0, seen["f6"]
    assert seen["f6"]["delivery_blocked"] is False
    assert na._STATUS_DONE in _statuses(seen)
    assert seen["revise"] == 0 and seen["reduce"] == 0
    assert seen["critique"] == 1, "a verification pass was spent with nothing to verify"


#: The exact live shape from job `lyjzgd69` (2026-08-17): complete, correct coverage PLUS one
#: pair the accepted outline never had. The model naming a beat that does not exist says nothing
#: about the beats that do.
_INVENTED = {"chapter": 9, "beat": 1, "state": "executed"}


def test_an_invented_beat_beside_complete_coverage_never_blocks_a_sound_book(golden):
    """🔴 THE REGRESSION THAT COST A CUSTOMER A BOOK, ON THE PRODUCTION PATH.

    Live job `lyjzgd69` came back with every outlined beat reported correctly and ONE invented
    pair alongside. `beat_census` returned `unknown_beat`, `_f6_scan` recorded
    `beat_census_unknown_beat` as UNPROVED, and UNPROVED blocks — a finished three-chapter
    manuscript was refused and refunded.

    This drives the REAL job body, the REAL gates and the REAL finaliser, not the census
    helper: the unknown row is present in BOTH the pre-repair and post-repair observations,
    exactly as a live critic would emit it, and the book must still reach DONE."""
    na = _live("narration_api")
    seen = golden(book=_book((SHORT1, B2_FIXED, B3_FIXED, B4_FIXED, B5_CLEAN)),
                  tense_before=TENSE_CLEAN, tele_before=TELE_CLEAN,
                  beats_before=BEATS_CLEAN + [dict(_INVENTED)],
                  beats_after=BEATS_CLEAN + [dict(_INVENTED)])

    assert seen["f6"].get("unproven") in (None, ""), seen["f6"]
    assert seen["f6"]["violations_detected"] == 0, seen["f6"]
    assert seen["f6"]["delivery_blocked"] is False, seen["f6"]
    assert na._STATUS_DONE in _statuses(seen), _statuses(seen)
    assert seen["refund"] == 0, "a sound book was refunded over a row about no real beat"
    assert seen["persisted"] is not None, "a sound book never reached persistence"


def test_an_invented_beat_cannot_hide_a_beat_the_outline_owns(golden):
    """🔴 FILTERING IS NOT FORGIVENESS, PROVED ON THE SAME PATH. Swap a real beat OUT and the
    invented one IN: dropping the unknown row leaves the real beat unreported, coverage is
    incomplete, and the job must still refuse. Without this row, "ignore unknown keys" would be
    indistinguishable from "accept partial coverage"."""
    na = _live("narration_api")
    substituted = [e for e in BEATS_CLEAN
                   if (e["chapter"], e["beat"]) != (4, 3)] + [dict(_INVENTED)]
    seen = golden(book=_book((SHORT1, B2_FIXED, B3_FIXED, B4_FIXED, B5_CLEAN)),
                  tense_before=TENSE_CLEAN, tele_before=TELE_CLEAN,
                  beats_before=substituted, beats_after=substituted)

    assert seen["f6"]["delivery_blocked"] is True, seen["f6"]
    assert "incomplete_coverage" in str(seen["f6"].get("unproven") or ""), seen["f6"]
    assert na._STATUS_DONE not in _statuses(seen)
    assert seen["refund"] == 1


# ---------------------------------------------------------------------------
# Partial repair — ONE failure out of five is still a refusal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("broken,kwargs", [
    ("tense_drift", {"tense_after": TENSE_DIRTY}),
    ("teleport", {"tele_after": TELE_DIRTY}),
    ("final_beat", {"beats_after": [dict(e, state="executed") if (e["chapter"], e["beat"])
                                    != (4, 3) else dict(e, state="absent")
                                    for e in BEATS_DIRTY]}),
    ("beat_execution", {"beats_after": [dict(e, state="executed") if (e["chapter"], e["beat"])
                                        != (4, 1) else dict(e, state="promised")
                                        for e in BEATS_DIRTY]}),
    ("chapter_ceiling", {"reduce_returns": STILL_LONG1}),
])
def test_one_failed_class_out_of_five_blocks_the_whole_delivery(golden, broken, kwargs):
    """🔴 FOUR OUT OF FIVE IS A FAILED JOB. A partition that resolves the easy classes and
    ships the hard one is the exact shape F6 exists to make unrepresentable."""
    seen = golden(**kwargs)
    assert seen["f6"]["violations_resolved"] == 4, seen["f6"]
    assert seen["f6"]["violations_unresolved"] == 1
    assert [i.split(":")[0] for i in seen["f6"]["unresolved_ids"]] == [broken]
    _blocked(seen)


def test_a_promise_is_not_an_execution(golden):
    """🔴 THE DISTINCTION `beat_execution` EXISTS FOR. "I will give a deposition" is a beat that
    has not happened, and a repair that leaves it a promise has repaired nothing."""
    seen = golden(beats_after=[dict(e, state="promised") if (e["chapter"], e["beat"]) == (4, 1)
                               else dict(e, state="executed") for e in BEATS_DIRTY])
    assert "outline_beat:4|1" in " ".join(seen["f6"]["unresolved_ids"])
    _blocked(seen)


def test_an_out_of_order_execution_is_not_the_outlined_beat(golden):
    """🔴 THE CHAIN THE BRIEF NAMES: surrender → recordings inadmissible → deposition executed.
    A deposition that happens while the beat it depends on is still absent is a different scene
    wearing the outline's name, so `beat_execution` refuses it."""
    seen = golden(beats_after=[{"chapter": 1, "beat": 1, "state": "absent"},
                               {"chapter": 2, "beat": 1, "state": "executed"},
                               {"chapter": 3, "beat": 1, "state": "executed"},
                               {"chapter": 4, "beat": 1, "state": "executed"},
                               {"chapter": 4, "beat": 2, "state": "executed"},
                               {"chapter": 4, "beat": 3, "state": "executed"}])
    assert seen["f6"]["delivery_blocked"] is True
    _blocked(seen)


# ---------------------------------------------------------------------------
# The failure modes that must never read as a repair
# ---------------------------------------------------------------------------
def test_a_no_op_repair_blocks_every_class(golden):
    """🔴 v9's EXACT FAILURE, FIVE TIMES OVER. The lanes report a repair; the bytes are the
    original."""
    seen = golden(revise=lambda book: book, reduce_returns=LONG1,
                  beat_returns=B4_PROMISE,
                  tense_after=TENSE_DIRTY, tele_after=TELE_DIRTY, beats_after=BEATS_DIRTY)
    assert seen["f6"]["chapters_changed"] == 0, seen["f6"]
    assert seen["f6"]["violations_resolved"] == 0
    assert seen["f6"]["violations_unresolved"] == 5
    _blocked(seen)


def test_a_repair_provider_exception_blocks(golden):
    seen = golden(revise_raises=True, tense_after=TENSE_DIRTY, tele_after=TELE_DIRTY,
                  beats_after=BEATS_DIRTY)
    assert seen["f6"]["violations_unresolved"] >= 3, seen["f6"]
    _blocked(seen)


def test_a_reducer_exception_blocks(golden):
    seen = golden(reduce_raises=True)
    assert "chapter_ceiling:1" in seen["f6"]["unresolved_ids"], seen["f6"]
    _blocked(seen)


def test_an_empty_reducer_candidate_is_never_spliced_into_the_book(golden):
    """🔴 AND IT IS NOT SPLICED IN EITHER. Blocking is the easy half — an empty candidate that
    got written into the manuscript would ALSO block, so "it blocked" cannot tell the two
    apart. What separates them is whether chapter 1 was touched at all: a provider that
    returned nothing must leave the server's own bytes exactly where they were."""
    seen = golden(reduce_returns="   ")
    assert "chapter_ceiling:1" in seen["f6"]["unresolved_ids"], seen["f6"]
    assert seen["f6"]["chapters_changed"] == 3, (
        "an empty candidate was written into the book; chapter 1 must be untouched")
    assert 1 not in seen["f6"]["collateral_chapters"]
    _blocked(seen)


def test_a_gutted_reducer_candidate_blocks(golden):
    """A two-word candidate is rejected before it can replace the original chapter."""
    seen = golden(reduce_returns="Eun-soo pulang.")
    assert "chapter_ceiling:1" in seen["f6"]["unresolved_ids"], seen["f6"]
    assert seen["f6"]["chapters_changed"] == 3
    failed_book = seen["final_book"]
    assert LONG1 in failed_book and "Eun-soo pulang." not in failed_book
    _blocked(seen)


def test_an_oversized_reducer_candidate_blocks(golden):
    """"Shorter" is not "short enough": 55 words against a ceiling of 44 is still over."""
    seen = golden(reduce_returns=STILL_LONG1)
    assert "chapter_ceiling:1" in seen["f6"]["unresolved_ids"], seen["f6"]
    assert LONG1 in seen["final_book"]
    _blocked(seen)


def test_the_reduction_gets_one_bounded_corrective_retry(golden):
    """The actuator spends at most two calls: the first attempt plus one corrective retry."""
    seen = golden(reduce_returns="")
    assert seen["reduce"] == 2, "the reducer exceeded its one-retry budget"
    assert not seen["reduce_corrections"][0]
    assert seen["reduce_corrections"][1]
    _blocked(seen)


def test_a_truncated_first_reduction_can_recover_without_shipping_it(golden):
    # In-band but unterminated: only the final-sentence guard can reject this candidate.
    truncated = _pad("Eun-soo pulang tanpa menyelesaikan kalimat karena", 40)
    seen = golden(reduce_sequence=[truncated, SHORT1])
    assert seen["reduce"] == 2
    assert seen["f6"]["delivery_blocked"] is False, seen["f6"]
    delivered = seen["persisted"]["book"]
    assert SHORT1 in delivered and truncated not in delivered


def test_a_reducer_echoed_heading_is_rejected_before_splice(golden):
    with_heading = "## Chapter 9: Palsu\n\n" + SHORT1
    seen = golden(reduce_sequence=[with_heading, SHORT1])
    assert seen["reduce"] == 2
    assert seen["f6"]["delivery_blocked"] is False, seen["f6"]
    assert "Chapter 9" not in seen["persisted"]["book"]


def test_two_invalid_reductions_keep_the_original_chapter(golden):
    seen = golden(reduce_sequence=["Eun-soo pulang", "Eun-soo pulang."])
    assert seen["reduce"] == 2
    failed_book = seen["final_book"]
    assert LONG1 in failed_book
    assert "Eun-soo pulang." not in failed_book
    _blocked(seen)


def test_the_reducer_is_handed_one_chapter_and_the_server_owned_ceiling(golden):
    """It never sees the book: the other chapters are unreachable by construction, not by
    inspection afterwards. 44 is `word_target 40 × 1.1`, the contract the generator had."""
    seen = golden()
    assert seen["reduce_targets"] == [(60, 36, 44)], seen["reduce_targets"]
    assert len(seen["reduce_required_beats"]) == 1
    assert "Eun-soo returns" in seen["reduce_required_beats"][0]


@pytest.mark.parametrize("census", [
    {"tense_after": ["past", "future", "past", "past", "past"]},
    {"tense_after": ["past", "past"]},
    {"tele_after": [0, 0, -3, 0, 0]},
    {"tele_after": [0, 0, 0]},
    {"beats_after": [{"chapter": 9, "beat": 9, "state": "executed"}]},
    {"beats_after": [{"chapter": 4, "beat": 1, "state": "definitely"}]},
    {"tense_after": None},
    {"tele_after": None},
    {"beats_after": None},
])
def test_an_invalid_verification_response_blocks(golden, census):
    """A verifier that cannot see is a verifier that must not vouch — whichever census is the
    one that came back unreadable."""
    seen = golden(**census)
    assert seen["f6"]["delivery_blocked"] is True, (census, seen["f6"])
    _blocked(seen)


def test_a_collateral_edit_blocks_even_though_every_target_reads_clean(golden):
    """🔴 CHAPTER 5 IS THE ONE NOBODY TARGETED. A repair that rewrote it has produced a book
    nobody can vouch for, so no resolutions at all — not four out of five."""
    seen = golden(revise=lambda book: _repair(book).replace(B5_CLEAN, B5_CLEAN + " Lagi."))
    assert seen["f6"]["collateral_chapters"] == [5], seen["f6"]
    assert seen["f6"]["violations_resolved"] == 0
    _blocked(seen)


def test_a_corrupted_heading_blocks(golden):
    """A repair may rewrite prose and nothing else. A destroyed heading is a structural failure,
    not an edit of the chapter it used to name."""
    seen = golden(revise=lambda book: re.sub(r"(?m)^##\s+(?:Chapter|Bab)\s+3\b[^\n]*$",
                                             "## Bab 99", _repair(book)))
    assert seen["f6"]["chapters_changed"] == 0, seen["f6"]
    _blocked(seen)


def test_a_missing_census_blocks_instead_of_reading_as_a_clean_book(golden):
    seen = golden(tele_before=None, tense_after=TENSE_DIRTY)
    assert seen["f6"].get("unproven", "").startswith("teleport_census_"), seen["f6"]
    _blocked(seen)


def test_a_missing_beat_census_blocks(golden):
    """🔴 THE CLASS THAT IS ABOUT SOMETHING MISSING CANNOT TREAT A MISSING REPORT AS CLEAN. With
    an accepted outline in hand and no beat census, nobody knows whether the ending happened."""
    seen = golden(beats_before=None)
    assert seen["f6"].get("unproven", "").startswith("beat_census_"), seen["f6"]
    _blocked(seen)


def test_a_book_with_no_accepted_outline_detects_no_beats_and_still_delivers(golden):
    """🔴 AND THE OTHER DIRECTION. You cannot miss a commitment nobody made, so an empty outline
    keyspace detects nothing rather than blocking every job that has no outline."""
    seen = golden(authority=None, beats_before=None, beats_after=None,
                  # With no outline, chapter 4 is not a target — so a repair that touched it
                  # would be collateral, correctly. The stub repairs only what was routed.
                  revise=lambda book: (book.replace(B2_DRIFT, B2_FIXED)
                                           .replace(B3_TELEPORT, B3_FIXED)))
    assert seen["f6"]["violations_detected"] == 3, seen["f6"]
    assert seen["f6"]["delivery_blocked"] is False, seen["f6"]
    assert _live("narration_api")._STATUS_DONE in _statuses(seen)


def test_a_detection_failure_blocks_rather_than_being_swallowed(golden, monkeypatch):
    monkeypatch.setattr(_live("narasi_gate"), "teleport_census",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    seen = golden()
    assert seen["f6"].get("unproven", "").startswith("detection_error:"), seen["f6"]
    _blocked(seen)


# ---------------------------------------------------------------------------
# The accounting closes, on every path above
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", [
    {},
    {"tense_after": TENSE_DIRTY},
    {"reduce_returns": STILL_LONG1},
    {"revise": lambda book: book, "tense_after": TENSE_DIRTY, "tele_after": TELE_DIRTY,
     "beats_after": BEATS_DIRTY, "reduce_returns": LONG1},
    {"revise": lambda book: _repair(book).replace(B5_CLEAN, B5_CLEAN + " Lagi.")},
    {"beats_after": None},
])
def test_the_books_balance_on_every_outcome(golden, case):
    """🔴 `resolved + unresolved == detected`, ALWAYS. The accounting raises rather than publish
    a total that looks complete, so a number that adds up here is the invariant holding rather
    than a coincidence — and it has to hold on the failing paths too, which is where a violation
    quietly leaves the books."""
    seen = golden(**case)
    f6 = seen["f6"]
    if "unproven" in f6:
        assert f6["delivery_blocked"] is True
        return
    assert f6["violations_resolved"] + f6["violations_unresolved"] == f6["violations_detected"]
    assert f6["violations_detected"] == 5
    assert len(set(f6["resolved_ids"]) & set(f6["unresolved_ids"])) == 0


def test_no_class_is_verified_by_a_default_pass(golden):
    """🔴 A CLASS WITH NO VERIFIER MUST NOT RESOLVE. If a sixth class is ever added to
    `F6_CLASSES` and nowhere else, the finaliser's fallback marks it unresolved rather than
    letting it through unchecked."""
    nf6 = _live("narasi_f6")
    monkey = list(nf6.F6_CLASSES)
    assert set(monkey) == {"tense_drift", "teleport", "final_beat", "beat_execution",
                           "chapter_ceiling"}, (
        "a class was added to F6_CLASSES — give it a verifier and a golden defect above")
