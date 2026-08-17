"""F8 — the seam loop driven through the REAL production delivery path.

🔴 A TEST THAT ONLY CALLS PURE HELPERS PROVES NOTHING ABOUT DELIVERY. Every row here drives
   `_run_narration_job_after_parity` with scripted provider doubles, and asks what the job
   PERSISTED and whether it finished DONE or FAILED. The pure contract lives in
   `test_narasi_f8_seam_contract.py`; this file is about whether it is wired to anything.

🔴 THE v9 SHAPE. Chapter 1→2 summarised the relationship thaw off-page instead of enacting it;
   chapter 2→3 jumped from the rooftop straight into the boardroom with the decision, the
   evidence gathering, the preparation, the journey and the elapsed time all missing. Both are
   reproduced synthetically here — no customer manuscript, no Downloads dependency.

🔴 THE SEAM CENSUS RIDES IN THE CONSISTENCY CALL THAT ALREADY EXISTS. F8's dedicated
   actuator does buy one bounded cheap-Claude call per seam (plus at most one corrective retry),
   but it must not buy a separate detection or verification read.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import types

import pytest


def _live(name):
    return sys.modules.get(name) or importlib.import_module(name)


f8 = _live("narasi_f8")

OUTLINE = {1: ["Eun-soo returns"], 2: ["Min-jae waits"], 3: ["Tae-jun signs"]}


def _packet(beats) -> str:
    return ("CURRENT ORDERED OUTLINE BEATS:\n"
            + "".join(f"  {i}. {b}\n" for i, b in enumerate(beats, 1)) + "\n")


AUTHORITY = {"text": "NARRATIVE AUTHORITY: accepted outline.",
             "outline_packets_by_chapter": {str(k): _packet(v) for k, v in OUTLINE.items()}}

BEATS_OK = [{"chapter": c, "beat": 1, "state": "executed"} for c in (1, 2, 3)]
TENSE_OK = ["past", "past", "past"]
TELE_OK = [0, 0, 0]


def seam(a, b, causal="explicit", location="explicit", time="explicit"):
    return {"chapter_a": a, "chapter_b": b,
            "causal": causal, "location": location, "time": time}


#: The two v9 elisions, as the critic would report them.
SEAMS_BROKEN = [seam(1, 2, causal="missing", time="missing"),
                seam(2, 3, causal="missing", location="missing", time="missing")]
SEAMS_CLEAN = [seam(1, 2), seam(2, 3)]


BODIES = ("satu dua tiga", "empat lima enam", "tujuh delapan sembilan")


def _book(count=3):
    """A book with EXACTLY `count` chapters — the server compares the delivered chapter count
    against the request, so a fixture that declares one chapter and writes three is refused by
    F6 before F8 is ever consulted."""
    return "\n\n".join(f"## Bab {i}: Judul {i}\n\n{b}"
                       for i, b in enumerate(BODIES[:count], 1)) + "\n"


def _rewrite(chapters):
    """A revise double that edits exactly the named chapters, byte-identical elsewhere."""
    def apply(text):
        out = []
        for index, block in enumerate(text.split("\n\n## "), 0):
            prefix = "" if index == 0 else "## "
            number = index + 1
            body = prefix + block
            if number in chapters:
                body = body.rstrip("\n") + " JEMBATAN\n"
            out.append(body)
        return "\n\n".join(out)
    return apply


@pytest.fixture
def job(monkeypatch):
    live_lz, live_na = _live("laozhang_api"), _live("narration_api")

    def blocked(*_a, **_k):
        raise AssertionError("a provider client was built — this is not offline")

    def run(*, seams_before=SEAMS_BROKEN, seams_after=SEAMS_CLEAN,
            revise=None, accepted_chapters=None, accepted_ids=None, chapters=3,
            critic_violations=(), seam_candidates=None,
            word_targets=(100, 100, 100)):
        seen = {"finalize": [], "persisted": None, "critique": 0, "revise": 0,
                "payload": None, "refund": 0, "routed": [], "seam_calls": []}

        async def _anoop(*_a, **_k):
            return None

        async def _finalize(job_id, job_uuid, tenant_id, *, status, result=None, error=None):
            seen["finalize"].append({"status": status, "error": error})
            if result is not None:
                seen["payload"] = dict(result)

        async def _refund(*_a, **_k):
            seen["refund"] += 1

        async def _persist(_tenant, _job_uuid, res, *_a, **_k):
            seen["persisted"] = dict(res or {})

        async def critique(*_a, authority_text="", **_k):
            seen["critique"] += 1
            first = seen["critique"] == 1
            # Every census must cover EXACTLY the chapters the server framed, or F6 refuses
            # the book before F8 is consulted — the censuses are indexed by chapter.
            out = {"score": 8,
                   "violations": [dict(v) for v in critic_violations] if first else [],
                   "tense_by_chapter": TENSE_OK[:chapters],
                   "teleports_by_chapter": TELE_OK[:chapters]}
            if str(authority_text or "").strip():
                out["beat_states"] = [dict(b) for b in BEATS_OK[:chapters]]
            rows = seams_before if first else seams_after
            if rows is not None:
                # Passed through EXACTLY as the model would send it — a malformed census is
                # the thing under test, so the double must not tidy it into a legal one.
                out["seam_states"] = ([dict(r) if isinstance(r, dict) else r for r in rows]
                                      if isinstance(rows, list) else rows)
            return (out, 0)

        async def _revise(text, _request, *_a, **_k):
            seen["revise"] += 1
            # Exactly what the structural lane would be handed.
            seen["routed"] = [dict(v) for v in (_request.get("violations") or [])
                              if isinstance(v, dict)]
            return ((revise or (lambda t: t))(text), 0)

        async def _seam_repair(chapter_b_body, *, chapter_a_tail, missing_dims,
                               required_beats, correction="", **_k):
            """Provider double for the REAL dedicated actuator.

            Attribution and counters are deliberately not scripted here: production publishes
            both only after it accepts and splices this candidate.  The fixture controls only
            provider bytes, which keeps the delivery test capable of catching broken wiring.
            """
            chapter_b = next((i for i, body_text in enumerate(BODIES, 1)
                              if body_text in chapter_b_body), None)
            identity = f"seam:{chapter_b - 1}|{chapter_b}" if chapter_b else ""
            seen["seam_calls"].append({
                "chapter_b": chapter_b,
                "identity": identity,
                "chapter_a_tail": chapter_a_tail,
                "missing_dims": list(missing_dims or ()),
                "required_beats": required_beats,
                "correction": correction,
            })
            if seam_candidates is not None:
                at = min(len(seen["seam_calls"]) - 1, len(seam_candidates) - 1)
                candidate = seam_candidates[at]
                return ((candidate(chapter_b_body) if callable(candidate) else candidate), 0)
            permitted = (set(accepted_ids) if accepted_ids is not None else {
                f"seam:{c - 1}|{c}" for c in (accepted_chapters or ())})
            if identity not in permitted:
                return (chapter_b_body, 0)
            # Four words: valid for this intentionally tiny fixture, meaningfully changed,
            # terminally punctuated, and still shares all three original words.
            return (f"JEMBATAN {BODIES[chapter_b - 1]}.", 0)

        async def _reduce(chapter_text, *, target_words, **_k):
            return (chapter_text, 0)

        async def cheap(*_a, **_k):
            return ("{}", 0)

        async def _gen(_req, **_kw):
            # The outline packet keyspace is what the beat census is validated against, so it
            # has to describe the same book the generator returned.
            _auth = {"text": AUTHORITY["text"],
                     "outline_packets_by_chapter": {
                         str(k): v for k, v in
                         AUTHORITY["outline_packets_by_chapter"].items()
                         if int(k) <= chapters}}
            return {"ok": True, "book": _book(chapters),
                    "chapters": [{"no": i} for i in range(1, chapters + 1)],
                    "_narrative_authority": _auth}

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
        monkeypatch.setattr(live_lz, "_narasi_seam_repair", _seam_repair)
        monkeypatch.setattr(live_lz, "_narasi_chapter_reduce", _reduce)
        monkeypatch.setattr(live_na, "generate_narration", _gen)
        monkeypatch.setattr(live_na, "_cancel_watcher", lambda *a, **k: asyncio.sleep(3600))
        monkeypatch.setattr(live_na, "_finalize", _finalize)
        monkeypatch.setattr(live_na, "_set_status", _anoop)
        monkeypatch.setattr(live_na, "_safe_progress", _anoop)
        monkeypatch.setattr(live_na, "_settle", _anoop)
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
                body={"chapters": [{"word_target": w} for w in word_targets],
                      "style": "storytelling", "language": "id"},
                job_id="j-f8", job_uuid=None, tenant_id="t", user_id="u",
                total=len(word_targets), meter_op="op", model="m",
                executor="narration_worker")
            survivors = (set(asyncio.all_tasks()) - before) - {asyncio.current_task()}
            if survivors:
                await asyncio.gather(*survivors, return_exceptions=True)

        asyncio.run(drive())
        return seen

    return run


def _f8(seen):
    payload = seen["payload"] or {}
    assert "f8" in payload, "F8 accounting never reached the durable result payload"
    return payload["f8"]


def _status(seen):
    return seen["finalize"][-1]["status"] if seen["finalize"] else None


# ---------------------------------------------------------------------------
# 1 — both v9 seams repair, and the job delivers
# ---------------------------------------------------------------------------
def test_both_v9_seams_are_detected_targeted_and_resolved(job):
    seen = job(revise=_rewrite({2, 3}), accepted_chapters={2, 3})
    block = _f8(seen)
    assert block["seams_detected"] == 2
    assert block["seams_targeted"] == 2
    assert block["seams_resolved"] == 2
    assert block["seams_unresolved"] == 0
    assert block["collateral_chapters"] == []
    assert block["new_defects"] == []
    assert block["delivery_blocked"] is False
    assert _status(seen) == "done"


def test_the_two_seams_target_chapters_two_and_three(job):
    seen = job(revise=_rewrite({2, 3}), accepted_chapters={2, 3})
    block = _f8(seen)
    assert sorted(block["detected_ids"]) == ["seam:1|2", "seam:2|3"]
    assert sorted(block["resolved_ids"]) == ["seam:1|2", "seam:2|3"]


# ---------------------------------------------------------------------------
# 2, 3 — either seam still missing blocks delivery
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("still,identity", [
    ([seam(1, 2, causal="missing"), seam(2, 3)], "seam:1|2"),
    ([seam(1, 2), seam(2, 3, location="missing")], "seam:2|3"),
])
def test_a_seam_that_is_still_missing_after_repair_blocks_delivery(job, still, identity):
    seen = job(seams_after=still, revise=_rewrite({2, 3}),
               accepted_chapters={2, 3})
    block = _f8(seen)
    assert identity in block["unresolved_ids"]
    assert block["delivery_blocked"] is True
    assert _status(seen) == "failed"


# ---------------------------------------------------------------------------
# 4 — a time label alone does not close a causal/location elision
# ---------------------------------------------------------------------------
def test_a_repair_that_only_supplies_a_time_label_still_blocks(job):
    seen = job(seams_after=[seam(1, 2, causal="missing"),
                            seam(2, 3, causal="missing", location="missing")],
               revise=_rewrite({2, 3}), accepted_chapters={2, 3})
    block = _f8(seen)
    assert block["seams_resolved"] == 0
    assert block["delivery_blocked"] is True


# ---------------------------------------------------------------------------
# 5 — a no-op patch keeps the original bytes and resolves nothing
# ---------------------------------------------------------------------------
def test_a_no_op_repair_keeps_the_bytes_and_resolves_nothing(job):
    seen = job(revise=lambda text: text, accepted_chapters=set())
    block = _f8(seen)
    assert block["seams_resolved"] == 0
    assert block["byte_changing_repairs"] == 0
    assert block["delivery_blocked"] is True
    assert _status(seen) == "failed"


@pytest.mark.parametrize("bad_candidate", [
    "Karena itu kemudian.",                       # bridge fragment, not the full body
    "## Bab 2: Sisipan\n\nJEMBATAN empat lima enam.",  # heading leaked by provider
    "JEMBATAN empat lima enam",                  # truncated / no terminal punctuation
])
def test_an_inadmissible_seam_candidate_retries_then_stays_blocked(job, bad_candidate):
    seen = job(seams_before=[seam(1, 2, causal="missing"), seam(2, 3)],
               seams_after=SEAMS_CLEAN, accepted_chapters={2},
               seam_candidates=[bad_candidate, bad_candidate])
    block = _f8(seen)
    assert len(seen["seam_calls"]) == 2, "one initial attempt plus one corrective retry"
    assert seen["seam_calls"][1]["correction"].startswith("Rejected because ")
    assert block["accepted_operations"] == 0
    assert block["seams_resolved"] == 0
    assert block["delivery_blocked"] is True
    assert _status(seen) == "failed"


def test_the_seam_provider_call_uses_the_dated_cheap_model_and_dedicated_phase(monkeypatch):
    lz = _live("laozhang_api")
    seen = {}

    async def cheap(system, user, **kwargs):
        seen.update({"system": system, "user": user, **kwargs})
        return ("repaired body.", 0)

    monkeypatch.delenv("NARASI_F8_REPAIR_MODEL", raising=False)
    monkeypatch.setattr(lz, "_narasi_cheap_call", cheap)
    asyncio.run(lz._narasi_seam_repair(
        "chapter B body.", chapter_a_tail="chapter A ending.",
        missing_dims=["causal", "time"], required_beats="  1. keep this beat\n",
        style="storytelling", language="id", tenant_id="t", user_id="u"))

    assert seen["model_override"] == "claude-haiku-4-5-20251001"
    assert seen["phase"] == "f8_repair"
    assert seen["require_complete"] is True
    assert "mandatory repair" in seen["system"]
    assert "chapter A ending." in seen["user"]
    assert "causal, time" in seen["user"]
    assert "1. keep this beat" in seen["user"]
    assert "chapter B body." in seen["user"]


@pytest.mark.parametrize(("candidate", "original", "reason"), [
    ("satu dua tiga empat lima enam.", "satu dua tiga empat lima enam.",
     "unchanged"),
    ("## Bab 2: Judul\n\nsatu dua tiga empat lima enam.",
     "satu dua tiga empat lima enam.", "chapter heading"),
    ("satu.", "satu dua tiga empat lima enam.", "chapter was cut"),
    ("satu dua tiga empat lima enam tujuh delapan sembilan sepuluh.",
     "satu dua tiga empat lima enam.", "rewrite, not a bridge"),
    ("satu dua tiga empat lima tujuh", "satu dua tiga empat lima enam.",
     "terminal punctuation"),
    ("alpha beta gamma delta epsilon zeta.", "satu dua tiga empat lima enam.",
     "too little of the original"),
])
def test_the_server_rejects_each_unsafe_seam_candidate_for_its_own_reason(
        candidate, original, reason):
    na = _live("narration_api")
    assert reason in na._f8_seam_candidate_reason(candidate, original=original)


# ---------------------------------------------------------------------------
# 6, 7 — the post-verifier census must be present and well formed
# ---------------------------------------------------------------------------
def test_a_post_verifier_that_omits_the_seam_census_is_unproved(job):
    seen = job(seams_after=None, revise=_rewrite({2, 3}),
               accepted_chapters={2, 3})
    block = _f8(seen)
    assert block["seams_resolved"] == 0
    assert block["delivery_blocked"] is True


@pytest.mark.parametrize("bad", [
    "not a list",
    [],
    [seam(1, 2)],
    [seam(1, 2), seam(1, 2)],
    [seam(1, 3), seam(2, 3)],
    [seam(0, 1), seam(2, 3)],
    [seam(1, 2, causal="unknown"), seam(2, 3)],
])
def test_a_malformed_post_census_is_unproved_and_blocks(job, bad):
    seen = job(seams_after=bad, revise=_rewrite({2, 3}),
               accepted_chapters={2, 3})
    block = _f8(seen)
    assert block["seams_resolved"] == 0
    assert block["delivery_blocked"] is True


# ---------------------------------------------------------------------------
# 8 — collateral
# ---------------------------------------------------------------------------
def test_another_lane_rewriting_chapter_one_is_collateral(job):
    seen = job(revise=_rewrite({1}), accepted_chapters={2, 3},
               critic_violations=[{"type": "timeline", "severity": "high", "chapter": 2,
                                   "evidence": "w0", "fix": "align the clock"}])
    block = _f8(seen)
    assert 1 in block["collateral_chapters"]
    assert block["seams_resolved"] == 0
    assert block["delivery_blocked"] is True


# ---------------------------------------------------------------------------
# 9 — a newly broken seam
# ---------------------------------------------------------------------------
def test_a_seam_broken_by_the_repair_itself_blocks_delivery(job):
    seen = job(seams_before=[seam(1, 2), seam(2, 3, causal="missing")],
               seams_after=[seam(1, 2, location="missing"), seam(2, 3)],
               revise=_rewrite({2}), accepted_chapters={3},
               critic_violations=[{"type": "timeline", "severity": "high", "chapter": 2,
                                   "evidence": "w0", "fix": "align the clock"}])
    block = _f8(seen)
    assert block["new_defects"]
    assert block["delivery_blocked"] is True


# ---------------------------------------------------------------------------
# 10 — the free-text finding and the census are ONE budget item
# ---------------------------------------------------------------------------
def test_a_free_text_boundary_finding_and_the_census_are_one_target(job):
    seen = job(revise=_rewrite({2, 3}), accepted_chapters={2, 3},
               critic_violations=[{"type": "chapter_boundary_break", "severity": "high",
                                   "chapter": 3, "evidence": "boardroom @ch3",
                                   "fix": "bridge the transition"}])
    block = _f8(seen)
    assert block["seams_detected"] == 2
    assert block["seams_targeted"] == 2
    # The census owns seam repair. Its free-text duplicate must not ride the generic lane,
    # while each canonical seam reaches the dedicated actuator exactly once.
    boundary = [v for v in seen["routed"] if v.get("type") == "chapter_boundary_break"]
    assert boundary == [], f"a seam leaked back into the generic lane: {boundary}"
    assert [c["identity"] for c in seen["seam_calls"]] == ["seam:1|2", "seam:2|3"]


# ---------------------------------------------------------------------------
# 11 — not applicable
# ---------------------------------------------------------------------------
def test_a_single_chapter_job_is_not_applicable_and_buys_nothing(job):
    seen = job(chapters=1, word_targets=(100,), seams_before=None, seams_after=None)
    payload = seen["payload"] or {}
    block = payload.get("f8") or {}
    assert block.get("applicable") is False
    assert _status(seen) == "done"


# ---------------------------------------------------------------------------
# 12 — provider-call accounting
# ---------------------------------------------------------------------------
def test_f8_reuses_the_existing_census_reads_and_bounds_its_actuator_calls(job):
    seen = job(revise=_rewrite({2, 3}), accepted_chapters={2, 3})
    assert seen["critique"] == 2, "F8 must ride the existing detection + post-repair reads"
    assert len(seen["seam_calls"]) == 2
    assert _f8(seen)["provider_calls"] == 2


# ---------------------------------------------------------------------------
# 13, 14 — structure preserved, and the accounting describes the DELIVERED bytes
# ---------------------------------------------------------------------------
def test_chapter_count_and_heading_order_survive_the_repair(job):
    ng = _live("narasi_gate")
    seen = job(revise=_rewrite({2, 3}), accepted_chapters={2, 3})
    delivered = (seen["payload"] or {}).get("markdown") or ""
    assert ng.chapter_ordinal_sequence(delivered) == [1, 2, 3]
    assert len(ng.chapter_word_counts(delivered)) == 3


def test_the_accounting_describes_the_final_delivered_bytes(job):
    """🔴 THE ACCOUNTING MUST BIND TO WHAT WAS SENT, not to a pre-dedup snapshot. Chapter 1 is
    never authorised, so a mutator that touches it after detection has to surface as
    collateral rather than pass unnoticed."""
    seen = job(revise=_rewrite({2, 3}), accepted_chapters={2, 3})
    delivered = (seen["payload"] or {}).get("markdown") or ""
    assert "JEMBATAN" in delivered
    # Chapter 1 was never authorised, so its body must be byte-identical. Asserted on the
    # BODY rather than on a block split: the delivered text carries the gates' own
    # `> **Gaya:** …` header, which is framing, not a chapter.
    assert "satu dua tiga JEMBATAN" not in delivered, "chapter 1 was touched"
    assert "satu dua tiga" in delivered
    assert _f8(seen)["delivery_blocked"] is False


# ---------------------------------------------------------------------------
# round-2 audit: the production rules the earlier suite never reached
# ---------------------------------------------------------------------------
def test_a_seam_broken_by_another_gates_repair_is_caught_even_when_none_was_detected(job):
    """🔴 P1 — THE POST-SCAN USED TO BE SKIPPED WHENEVER DETECTION FOUND NOTHING. The census was
    fetched and thrown away, so F1–F7 could break a seam after F8 had stopped looking. The rule
    is the manuscript, not the finding list: if the bytes changed, the seams are re-read."""
    # A non-seam finding is what makes a repair happen at all: with a clean seam census F8
    # routes nothing, so the manuscript has to be changed by SOME other lane for this to be
    # the case under test.
    seen = job(seams_before=SEAMS_CLEAN,
               seams_after=[seam(1, 2, location="missing"), seam(2, 3)],
               revise=_rewrite({2, 3}), accepted_chapters=set(),
               critic_violations=[{"type": "timeline", "severity": "high", "chapter": 2,
                                   "evidence": "w0", "fix": "align the clock"}])
    block = _f8(seen)
    assert "seam:1|2:location" in (block.get("new_defects") or [])
    assert block["delivery_blocked"] is True
    assert _status(seen) == "failed"


def test_a_clean_book_that_nothing_touched_still_delivers(job):
    seen = job(seams_before=SEAMS_CLEAN, revise=lambda t: t, accepted_chapters=set())
    assert _f8(seen)["delivery_blocked"] is False
    assert _status(seen) == "done"


def test_a_free_text_finding_that_contradicts_the_census_blocks(job):
    """🔴 P1 — DEDUP REMOVED A DISAGREEMENT, NOT A DUPLICATE. Dropping every free-text
    `chapter_boundary_break` whenever the census parsed let a census that said "clean" delete a
    hard finding that said "broken", and the clean path then permitted delivery."""
    seen = job(seams_before=SEAMS_CLEAN, revise=lambda t: t, accepted_chapters=set(),
               critic_violations=[{"type": "chapter_boundary_break", "severity": "high",
                                   "chapter": 3, "evidence": "boardroom @ch3",
                                   "fix": "bridge it"}])
    assert _f8(seen)["delivery_blocked"] is True
    assert _status(seen) == "failed"


def test_the_actuator_receives_a_directive_naming_the_missing_dimensions(job):
    """🔴 P1 — WHAT THE PROVIDER ACTUALLY GETS through the dedicated seam lane."""
    seen = job(revise=_rewrite({2, 3}), accepted_chapters={2, 3})
    by_seam = {v["identity"]: v for v in seen["seam_calls"]}
    assert sorted(by_seam) == ["seam:1|2", "seam:2|3"]

    one_two = by_seam["seam:1|2"]
    assert one_two["chapter_b"] == 2
    assert "satu dua tiga" in one_two["chapter_a_tail"]
    assert one_two["missing_dims"] == ["causal", "time"]
    assert "Min-jae waits" in one_two["required_beats"]

    two_three = by_seam["seam:2|3"]
    assert two_three["chapter_b"] == 3
    assert two_three["missing_dims"] == ["causal", "location", "time"]
    assert "Tae-jun signs" in two_three["required_beats"]


def test_attribution_is_per_seam_not_per_chapter(job):
    """🔴 P1 — A CHAPTER-LEVEL RECORD SAYS ONLY THAT SOMETHING WAS ACCEPTED THERE.

    The two readings have to DISAGREE for this to prove anything: the lane accepted an
    operation in BOTH chapters 2 and 3, but only the 1→2 seam's own violation was among them —
    chapter 3's accepted operation was something else entirely. Chapter-level attribution
    resolves both seams; per-seam attribution resolves one."""
    seen = job(revise=_rewrite({2, 3}), accepted_chapters={2, 3},
               accepted_ids={"seam:1|2"})
    block = _f8(seen)
    assert "seam:1|2" in (block.get("resolved_ids") or [])
    assert "seam:2|3" in (block.get("unresolved_ids") or []), \
        "a foreign accepted operation in chapter 3 attributed the 2->3 seam repair"
    assert block["delivery_blocked"] is True


def test_the_counters_come_from_the_seam_lane_not_from_the_target_list(job):
    """🔴 P1 — `attempts=len(targeted)` AND `provider_calls=1` WERE ASSUMPTIONS. Two seam
    targets are not one physical call, and a target that was capped or never attempted was still
    reported as attempted."""
    seen = job(revise=_rewrite({2, 3}), accepted_chapters={2, 3})
    block = _f8(seen)
    assert block["repair_attempts"] == 2
    assert block["provider_calls"] == 2, "one call for two structural targets is a fiction"
    assert block["accepted_operations"] == 2
    assert block["byte_changing_repairs"] == 2


def test_the_conflict_reason_is_truthful_not_a_malformed_row(job):
    """🔴 A DISAGREEMENT IS NOT AN UNPARSEABLE CENSUS."""
    seen = job(seams_before=SEAMS_CLEAN, revise=lambda t: t, accepted_chapters=set(),
               critic_violations=[{"type": "chapter_boundary_break", "severity": "high",
                                   "chapter": 3, "evidence": "boardroom @ch3",
                                   "fix": "bridge it"}])
    block = _f8(seen)
    assert block["reason"] == f8.BOUNDARY_CONFLICT == "boundary_observation_conflict"
    assert block["delivery_blocked"] is True
