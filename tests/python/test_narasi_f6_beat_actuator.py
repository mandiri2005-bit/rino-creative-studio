"""F6 `beat_execution` — the DEDICATED actuator, driven through the REAL delivery path.

🔴 A TEST THAT ONLY CALLS PURE HELPERS PROVES NOTHING ABOUT DELIVERY. Every row here drives
   `_run_narration_job_after_parity` with scripted provider doubles and asks what the job
   PERSISTED and whether it finished DONE or FAILED.

🔴 THE PRODUCTION SHAPE — canary `7pucr0hs`. The beat census was SOUND: ten invented beat rows
   were ignored on the first read and nine more on the final read, exactly as the census fix
   intends, and one genuine finding survived — `beat_execution:2:outline_beat:2|2`, an outline
   beat that never happens on the page. It was routed to the GENERIC structural patch lane,
   which answered `unknown_operation → prose_unterminated`; the chunked revise fallback then
   returned two no-ops and one chapter truncated from 519 words to 45. The job blocked with
   "1 unresolved of 2 detected" and the UI went blank.

   That shape is reproduced here: an invented row the census must ignore, one genuine missing
   beat, and a lane that must ENACT it rather than audit it.

🔴 THE ACTUATOR PROVES NOTHING BY ITSELF. Its attribution is telemetry; the FINAL BEAT CENSUS
   over the delivered bytes is the only thing that can turn a detection into a resolution.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import types

import pytest


def _live(name):
    return sys.modules.get(name) or importlib.import_module(name)


# ---------------------------------------------------------------------------
# The book: three chapters, one outlined beat that never happens
# ---------------------------------------------------------------------------
BODIES = (
    "Eun-soo membuka pintu kantor lama itu dan menghitung setiap berkas yang masih "
    "tersisa di atas meja panjang berdebu itu.",
    "Min-jae menunggu di lobi sambil membaca daftar saksi yang belum satu pun bersedia "
    "memberi keterangan kepada tim hukum kantor itu.",
    "Tae-jun menutup rapat itu lalu berjalan keluar melewati koridor panjang yang sudah "
    "sepi sejak jam lima sore tadi.",
)
TITLES = ("Kembali", "Lobi", "Koridor")

#: Chapter 2's body is the one the actuator rewrites, and its length is the band every
#: candidate check below is measured against. Pinned here so an edit to the prose that
#: silently moves a candidate across a threshold fails loudly instead of changing what the
#: rejection tests actually test.
CH2_WORDS = len(BODIES[1].split())
CH2_FLOOR = int(CH2_WORDS * 0.9)
CH2_CEIL = int(CH2_WORDS * 1.6)

#: A candidate that ENACTS the beat: the chapter it was handed plus the deposition, actually
#: signed, terminally punctuated, inside the word band and sharing the original prose.
GOOD = " Ia lalu menandatangani deposisi itu di depan notaris."


def _enacted(chapter_body: str) -> str:
    return chapter_body.rstrip().rstrip(".") + "." + GOOD


#: The four shapes the canary produced, each of which must be REFUSED before it can be spliced.
def _noop(chapter_body: str) -> str:
    return chapter_body


def _truncated(chapter_body: str) -> str:
    """519 → 45 words: the compression editor deleting instead of adding."""
    return " ".join(chapter_body.split()[:5]) + "."


def _unterminated(chapter_body: str) -> str:
    """In band, but stopped mid-sentence — `prose_unterminated`, the lane's own error."""
    return chapter_body.rstrip().rstrip(".") + " lalu ia berjalan menuju ruang"


def _heading_leak(chapter_body: str) -> str:
    return "## Bab 2: Bocor\n\n" + _enacted(chapter_body)


def _book(bodies=BODIES) -> str:
    return "\n\n".join(f"## Bab {i}: {t}\n\n{b}"
                       for i, (t, b) in enumerate(zip(TITLES, bodies), 1)) + "\n"


#: The ACCEPTED OUTLINE this process rendered — the keyspace the beat census is validated
#: against. Chapter 3's only beat is the LAST commitment, so it is the `final_beat`; chapter 2
#: beat 2 is a `beat_execution`, which is the class under test.
OUTLINE = {1: ["Eun-soo returns to the office"],
           2: ["Min-jae waits for the witness list",
               "Tae-jun executes the deposition before a notary"],
           3: ["Tae-jun closes the hearing"]}


def _packet(beats) -> str:
    return ("CURRENT ORDERED OUTLINE BEATS:\n"
            + "".join(f"  {i}. {b}\n" for i, b in enumerate(beats, 1)) + "\n")


AUTHORITY = {"text": "NARRATIVE AUTHORITY: accepted outline.",
             "outline_packets_by_chapter": {str(k): _packet(v) for k, v in OUTLINE.items()}}

#: 🔴 THE INVENTED ROW. A beat the accepted outline does not have. The census must COUNT it as
#: ignored and stay valid — the `lyjzgd69` / `7pucr0hs` fix — so it never becomes a violation
#: and never buys a repair. Nothing in this file may change that behaviour.
INVENTED = {"chapter": 9, "beat": 1, "state": "absent"}

BEATS_DIRTY = [{"chapter": 1, "beat": 1, "state": "executed"},
               {"chapter": 2, "beat": 1, "state": "executed"},
               {"chapter": 2, "beat": 2, "state": "promised"},   # the genuine finding
               {"chapter": 3, "beat": 1, "state": "executed"},
               dict(INVENTED)]
BEATS_CLEAN = [dict(e, state="executed") for e in BEATS_DIRTY if e != INVENTED]
BEATS_CLEAN.append(dict(INVENTED))

TENSE_OK = ["past", "past", "past"]
TELE_OK = [0, 0, 0]
SEAMS_OK = [{"chapter_a": i, "chapter_b": i + 1, "causal": "explicit",
             "location": "continuous", "time": "explicit"} for i in (1, 2)]

BODY = {"chapters": [{"word_target": 100}] * 3, "style": "storytelling", "language": "id"}


# ---------------------------------------------------------------------------
# The harness: the real job body, the real gates, the real finaliser
# ---------------------------------------------------------------------------
@pytest.fixture
def job(monkeypatch):
    live_lz, live_na = _live("laozhang_api"), _live("narration_api")

    def blocked(*_a, **_k):
        raise AssertionError("a provider client was built — this is not offline")

    def run(*, beats_before=BEATS_DIRTY, beats_after=BEATS_CLEAN,
            beat_candidates=(_enacted,), beat_raises=False,
            critic_violations=(), revise=None, tense_after=TENSE_OK):
        seen = {"finalize": [], "persisted": None, "critique": 0, "revise": 0,
                "payload": None, "refund": 0, "routed": [], "beat_calls": [],
                "final_book": None, "f6": None}

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
            out = {"score": 8,
                   "violations": [dict(v) for v in critic_violations] if first else [],
                   "tense_by_chapter": list(TENSE_OK if first else tense_after),
                   "teleports_by_chapter": list(TELE_OK),
                   "seam_states": [dict(s) for s in SEAMS_OK]}
            # Section 6d only exists when a NARRATIVE AUTHORITY is supplied, so a critic asked
            # without one cannot answer about beats.
            if str(authority_text or "").strip():
                out["beat_states"] = [dict(b) for b in (beats_before if first else beats_after)]
            return (out, 0)

        async def _revise(text, _request, *_a, **_k):
            seen["revise"] += 1
            seen["routed"] = [dict(v) for v in (_request.get("violations") or [])
                              if isinstance(v, dict)]
            return ((revise or (lambda t: t))(text), 0)

        async def _beat_repair(chapter_body, *, missing_beat, beat_number, chapter_number,
                               required_beats="", correction="", previous_tail="",
                               word_ceiling=0, **_k):
            seen["beat_calls"].append({
                "body": chapter_body, "words": len(chapter_body.split()),
                "missing_beat": missing_beat, "beat_number": beat_number,
                "chapter_number": chapter_number, "required_beats": required_beats,
                "correction": correction, "previous_tail": previous_tail,
                "word_ceiling": word_ceiling})
            if beat_raises:
                raise RuntimeError("beat actuator provider exploded")
            at = min(len(seen["beat_calls"]) - 1, len(beat_candidates) - 1)
            candidate = beat_candidates[at]
            return ((candidate(chapter_body) if callable(candidate) else candidate), 0)

        async def _reduce(chapter_text, **_k):
            raise AssertionError("the ceiling reducer ran — no chapter is over its ceiling")

        async def cheap(*_a, **_k):
            return ("{}", 0)

        async def _gen(_req, **_kw):
            return {"ok": True, "book": _book(),
                    "chapters": [{"no": i} for i in range(1, 4)],
                    "_narrative_authority": dict(AUTHORITY)}

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
        monkeypatch.setattr(live_lz, "_narasi_beat_execution_repair", _beat_repair)
        monkeypatch.setattr(live_lz, "_narasi_chapter_reduce", _reduce)

        # 🔴 A SPY, NOT A SUBSTITUTE. The REAL finaliser runs and the real hard block reads its
        # real return value; a blocked job persists nothing, so reading the accounting off the
        # persisted row would make every blocked case unobservable.
        _real = live_na._f6_finalize

        async def _spy(result, body, **kw):
            out = await _real(result, body, **kw)
            seen["f6"] = dict(out or {})
            seen["final_book"] = result.get("book") or result.get("output")
            return out

        monkeypatch.setattr(live_na, "_f6_finalize", _spy)
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
                body=dict(BODY), job_id="j-beat", job_uuid=None, tenant_id="t", user_id="u",
                total=3, meter_op="op", model="m", executor="narration_worker")
            survivors = (set(asyncio.all_tasks()) - before) - {asyncio.current_task()}
            if survivors:
                await asyncio.gather(*survivors, return_exceptions=True)

        asyncio.run(drive())
        return seen

    return run


def test_the_fixture_bodies_put_every_candidate_where_this_file_claims(monkeypatch):
    """🔴 THE ARITHMETIC IS THE TEST. Each rejection row below is only meaningful if its
    candidate fails the check it is named for and passes the others; prose edits move word
    counts silently, so the bands are pinned here."""
    na = _live("narration_api")
    base = BODIES[1]
    assert (CH2_WORDS, CH2_FLOOR, CH2_CEIL) == (20, 18, 32)
    assert na._f6_beat_candidate_reason(_enacted(base), original=base, ceiling=110) == ""
    assert CH2_FLOOR <= len(_enacted(base).split()) <= CH2_CEIL
    # each refusal names its own cause, and only its own
    assert "unchanged" in na._f6_beat_candidate_reason(_noop(base), original=base)
    assert "cut, not extended" in na._f6_beat_candidate_reason(_truncated(base), original=base)
    assert "terminal punctuation" in na._f6_beat_candidate_reason(
        _unterminated(base), original=base)
    assert CH2_FLOOR <= len(_unterminated(base).split()) <= CH2_CEIL, \
        "the unterminated row must fail on punctuation, not on the word band"
    assert "chapter heading" in na._f6_beat_candidate_reason(_heading_leak(base), original=base)
    assert "empty" in na._f6_beat_candidate_reason("", original=base)


def _status(seen):
    return seen["finalize"][-1]["status"] if seen["finalize"] else None


def _chapters(book: str) -> list:
    ngate = _live("narasi_gate")
    return [b for b in ngate.split_chapter_blocks(book or "")
            if ngate.chapter_heading_line(b)]


# ---------------------------------------------------------------------------
# 1 — the canary shape, repaired: the beat is enacted and the job delivers
# ---------------------------------------------------------------------------
def test_the_missing_beat_is_enacted_and_the_job_delivers(job):
    seen = job()
    na = _live("narration_api")
    assert seen["f6"]["violations_detected"] == 1, seen["f6"]
    assert seen["f6"]["violations_resolved"] == 1, seen["f6"]
    assert seen["f6"]["violations_unresolved"] == 0
    assert seen["f6"]["resolved_ids"] == ["beat_execution:2:outline_beat:2|2"]
    assert seen["f6"]["delivery_blocked"] is False
    assert _status(seen) == na._STATUS_DONE
    assert seen["refund"] == 0
    assert seen["persisted"] is not None


def test_only_the_target_chapter_changed(job):
    seen = job()
    chapters = _chapters(seen["final_book"])
    assert len(chapters) == 3
    assert BODIES[0] in chapters[0] and BODIES[2] in chapters[2]
    assert GOOD.strip() in chapters[1], "the beat was never enacted in the target chapter"
    assert GOOD.strip() not in chapters[0] and GOOD.strip() not in chapters[2]
    assert seen["f6"]["chapters_changed"] == 1
    assert seen["f6"]["collateral_chapters"] == []


def test_the_server_owned_heading_survives_the_splice(job):
    seen = job()
    ngate = _live("narasi_gate")
    headings = [ngate.chapter_heading_line(b).strip()
                for b in _chapters(seen["final_book"])]
    assert headings == [f"## Bab {i}: {t}" for i, t in enumerate(TITLES, 1)]


# ---------------------------------------------------------------------------
# 2 — the census fix stays exactly as it is
# ---------------------------------------------------------------------------
def test_the_invented_beat_row_is_ignored_and_never_buys_a_repair(job):
    """🔴 DO NOT REGRESS THE CENSUS. The invented row is outside the accepted outline's
    keyspace: it is COUNTED as ignored and never becomes a violation, so exactly one beat is
    detected and exactly one repair is dispatched."""
    seen = job()
    assert seen["f6"]["violations_detected"] == 1, seen["f6"]
    assert len(seen["beat_calls"]) == 1
    assert "9" not in str(seen["f6"].get("unresolved_ids") or [])


# ---------------------------------------------------------------------------
# 3 — routing: the generic lanes never see this class again
# ---------------------------------------------------------------------------
def test_beat_execution_never_reaches_the_generic_revise_request(job):
    """The merged structural/chunked lane is what canary `7pucr0hs` answered
    `unknown_operation → prose_unterminated` from. It must never be handed this class again —
    while an unrelated finding still routes to it normally."""
    seen = job(critic_violations=[{
        "type": "timeline", "severity": "high", "chapter": 1,
        "evidence": "an unrelated finding @ch1", "fix": "fix it"}],
        revise=lambda text: text)
    assert seen["revise"] == 1, "the unrelated finding still uses the generic lane"
    classes = {v.get("f6_class") for v in seen["routed"]}
    assert "beat_execution" not in classes, seen["routed"]
    assert {v.get("type") for v in seen["routed"]} == {"timeline"}


def test_the_actuator_is_dispatched_even_when_no_other_lane_runs(job):
    """A book whose ONLY defect is a missing beat used to depend on the merged revise running
    at all. The dedicated lane is dispatched on its own account."""
    seen = job()
    assert seen["revise"] == 0, "nothing else was wrong — no merged revise should run"
    assert len(seen["beat_calls"]) == 1


# ---------------------------------------------------------------------------
# 4 — what the actuator is handed
# ---------------------------------------------------------------------------
def test_the_actuator_receives_the_exact_beat_the_outline_committed_to(job):
    seen = job()
    call = seen["beat_calls"][0]
    assert call["chapter_number"] == 2
    assert call["beat_number"] == 2
    assert call["missing_beat"] == OUTLINE[2][1]
    assert call["words"] == CH2_WORDS, "the actuator must be handed the CURRENT chapter body"
    assert BODIES[1] in call["body"]
    assert BODIES[0] not in call["body"] and BODIES[2] not in call["body"]


def test_the_actuator_receives_the_chapters_own_outline_packet_and_ceiling(job):
    seen = job()
    call = seen["beat_calls"][0]
    assert call["required_beats"] == _packet(OUTLINE[2])
    assert OUTLINE[2][0] in call["required_beats"], "the OTHER beats must survive the rewrite"
    # word_target 100 → the server's own ceiling of 110, so the repair cannot manufacture a
    # fresh `chapter_ceiling` violation nothing downstream is targeting.
    assert call["word_ceiling"] == 110


def test_the_previous_chapter_travels_as_bounded_read_only_context(job):
    seen = job()
    call = seen["beat_calls"][0]
    assert BODIES[0] in call["previous_tail"]
    assert len(call["previous_tail"]) <= 1200
    assert "## Bab" not in call["previous_tail"], "a heading is server-owned, never context"


# ---------------------------------------------------------------------------
# 5 — the rejection ladder: two attempts, then BLOCKED
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("candidate,label", [
    (_noop, "an audit that returned the chapter unchanged"),
    (_truncated, "the 519→45 truncation"),
    (_unterminated, "prose_unterminated"),
    (_heading_leak, "a leaked chapter heading"),
    ("", "an empty response"),
])
def test_an_inadmissible_candidate_is_refused_twice_and_then_blocks(job, candidate, label):
    na = _live("narration_api")
    seen = job(beat_candidates=(candidate, candidate), beats_after=BEATS_DIRTY)
    assert len(seen["beat_calls"]) == 2, f"{label}: the retry cap was not honoured"
    assert seen["f6"]["violations_resolved"] == 0, label
    assert seen["f6"]["unresolved_ids"] == ["beat_execution:2:outline_beat:2|2"]
    assert seen["f6"]["delivery_blocked"] is True, label
    assert _status(seen) == na._STATUS_FAILED
    assert seen["refund"] == 1


@pytest.mark.parametrize("candidate", [_noop, _truncated, _unterminated, _heading_leak])
def test_a_refused_candidate_never_reaches_the_manuscript(job, candidate):
    seen = job(beat_candidates=(candidate, candidate), beats_after=BEATS_DIRTY)
    chapters = _chapters(seen["final_book"])
    assert chapters[1].rstrip().endswith(BODIES[1]), "a refused candidate was spliced"
    assert seen["f6"]["chapters_changed"] == 0


def test_the_retry_cap_is_one_initial_attempt_plus_one_correction(job):
    """🔴 BOUNDED MEANS BOUNDED. A second rejection ends the lane; it never becomes a ladder
    that spends the job's budget arguing with a model."""
    seen = job(beat_candidates=(_noop, _noop, _enacted), beats_after=BEATS_DIRTY)
    assert len(seen["beat_calls"]) == 2
    assert seen["f6"]["delivery_blocked"] is True


def test_the_corrective_retry_carries_the_reason_the_first_was_refused(job):
    seen = job(beat_candidates=(_truncated, _enacted))
    assert len(seen["beat_calls"]) == 2
    assert seen["beat_calls"][0]["correction"] == ""
    correction = seen["beat_calls"][1]["correction"]
    assert "Rejected because" in correction
    assert "cut, not extended" in correction
    assert seen["f6"]["violations_resolved"] == 1
    assert _status(seen) == _live("narration_api")._STATUS_DONE


def test_a_provider_exception_leaves_the_beat_unexecuted_and_blocks(job):
    seen = job(beat_raises=True, beats_after=BEATS_DIRTY)
    assert seen["f6"]["violations_resolved"] == 0
    assert seen["f6"]["delivery_blocked"] is True
    assert _status(seen) == _live("narration_api")._STATUS_FAILED


# ---------------------------------------------------------------------------
# 6 — attribution is telemetry, and the final census is the only proof
# ---------------------------------------------------------------------------
def test_the_lane_reports_on_its_own_channel(job):
    seen = job()
    lane = seen["f6"]["beat_repair"]
    assert lane == {"accepted_ids": ["outline_beat:2|2"], "attempted": 1,
                    "provider_calls": 1, "accepted": 1}
    # 🔴 NOT MIXED WITH THE STRUCTURAL OR LEGACY LANES' COUNTERS.
    assert "legacy_revise" not in seen["f6"]
    assert "_f8_structural_counters" not in seen["f6"]


def test_attempted_counts_beats_not_retries(job):
    seen = job(beat_candidates=(_truncated, _enacted))
    lane = seen["f6"]["beat_repair"]
    assert lane["attempted"] == 1, "one beat was repaired, not two"
    assert lane["provider_calls"] == 2, "the corrective retry is a physical call"


def test_an_accepted_candidate_does_not_make_an_unresolved_beat_resolved(job):
    """🔴 THE WHOLE POINT. The lane spliced a candidate and says so on its own channel — and
    the final census still reads the beat as `promised`, so delivery is refused. Provider
    success is not evidence."""
    na = _live("narration_api")
    seen = job(beats_after=BEATS_DIRTY)
    assert seen["f6"]["beat_repair"]["accepted"] == 1
    assert seen["f6"]["violations_resolved"] == 0
    assert seen["f6"]["delivery_blocked"] is True
    assert _status(seen) == na._STATUS_FAILED


def test_a_beat_executed_out_of_order_is_not_the_outlined_beat(job):
    """`beat_execution` verifies with `require_order=True`: a deposition that happens before
    the witness list it depends on is a different scene wearing its name."""
    out_of_order = [{"chapter": 1, "beat": 1, "state": "executed"},
                    {"chapter": 2, "beat": 1, "state": "promised"},
                    {"chapter": 2, "beat": 2, "state": "executed"},
                    {"chapter": 3, "beat": 1, "state": "executed"}]
    seen = job(beats_after=out_of_order)
    assert seen["f6"]["violations_resolved"] == 0
    assert seen["f6"]["delivery_blocked"] is True


def test_the_private_lane_channel_never_reaches_the_persisted_row(job):
    seen = job()
    assert "_f6_beat_repair" not in (seen["persisted"] or {})
    assert "_f6_pending" not in (seen["persisted"] or {})
