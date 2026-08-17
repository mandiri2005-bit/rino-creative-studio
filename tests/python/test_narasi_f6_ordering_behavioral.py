"""F6 ordering — BEHAVIOURAL, not a call-order read of the source.

🔴 THE WITNESS THIS FILE REPLACES. `test_the_finaliser_runs_after_every_manuscript_mutator`
   parses `_run_narration_job_after_parity` and compares line numbers. That is real evidence
   about WHERE the call sits, and a mutant that moved it did die — but it never RUNS the
   L3-assist repair, so it cannot show that a manuscript mutated after the gates is actually
   caught. A structural witness proves a call site; it cannot prove a consequence.

🔴 WHAT MAKES THESE ROWS DISCRIMINATING. The critic double here is an HONEST OBSERVER: it reads
   the bytes it is handed and derives the censuses from them, instead of replaying a script.
   That is the whole design. With a scripted census, finalising early and finalising late
   return the same answer and no assertion can separate them. Reading the text, the two
   diverge:

       finaliser AFTER  the L3-assist repair → sees the RE-DRIFTED book  → blocks   ✅
       finaliser BEFORE the L3-assist repair → sees the REPAIRED  book   → delivers ❌

   So moving the call — or deleting it — turns these rows red for the right reason: the job
   shipped a manuscript carrying the defect it had already accounted `resolved`.

This is F1's `l3_proof_invalidated_late_mutation` one layer down, and it is the failure this
workstream re-introduced three rounds running.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

import canon_lite as cl                                   # noqa: E402
import canon_lite_qc_contract as qcc                      # noqa: E402
import canon_lite_qc_meter as meter                       # noqa: E402

CANARY = "t-canary"
PLACEHOLDER_KEY = "placeholder-not-a-credential"

#: Every variable the assist activation gate reads, all correct — the same set
#: `test_canon_lite_l3_assist_activation.py` uses. Assist has to genuinely ARM here, because
#: the seam that re-drifts the manuscript only runs when it does.
CLEARED_ENV = {
    cl.MODE_ENV_VAR: "assist",
    cl.ASSIST_TENANTS_ENV_VAR: CANARY,
    meter.EXTRACTOR_CONCURRENCY_ENV: "4",
    "L2B_MAX_INFLIGHT": "8",
    qcc.QC_API_KEY_ENV: PLACEHOLDER_KEY,
}


def _live(name):
    import importlib
    return sys.modules.get(name, importlib.import_module(name))


TITLES = ("Kembali", "Jendela", "Dewan")
PAST = ("Eun-soo menyusuri koridor dan menghitung pintu yang sudah ia tutup rapat.",
        "Cahaya terakhir kota jatuh di jendela sementara Min-jae menunggu kabar itu.",
        "Tae-jun menandatangani deposisi dan ruangan itu kehilangan seluruh suaranya.")
#: The defect. Its presence in a chapter is what the honest critic double reports as `present`.
DRIFT2 = "Cahaya terakhir kota jatuh di jendela sementara Min-jae duduk dan menunggu."


def _book(bodies):
    return "\n\n".join(f"## Chapter {i}: {t}\n\n{b}"
                       for i, (t, b) in enumerate(zip(TITLES, bodies), 1))


DRIFTED = _book((PAST[0], DRIFT2, PAST[2]))


@pytest.fixture
def metered_host():
    meter.reset_host_role_for_tests()
    meter.declare_host_role(meter.HOST_ROLE_NARRATION_WORKER)
    yield
    meter.reset_host_role_for_tests()


@pytest.fixture
def job(monkeypatch, metered_host):
    """The real job body, with assist ARMED and an honest, byte-reading critic."""
    live_lz, live_na, live_ng = (_live("laozhang_api"), _live("narration_api"),
                                 _live("narasi_gate"))

    def run(*, late_mutation=None, scrub_mutation=None):
        seen = {"finalize": [], "refund": 0, "persisted": None, "settle": 0,
                "critique": 0, "f6": {}, "assist_ran": 0, "observed": []}

        async def _anoop(*_a, **_k):
            return None

        async def _finalize(job_id, job_uuid, tenant_id, *, status, result=None, error=None):
            seen["finalize"].append({"status": status, "error": error})

        async def _refund(*_a, **_k):
            seen["refund"] += 1

        async def _settle(*_a, **_k):
            seen["settle"] += 1

        async def _persist(_tenant, _job_uuid, res, *_a, **_k):
            seen["persisted"] = dict(res or {})

        def _census_of(text):
            """🔴 THE HONEST OBSERVER. Derived from the bytes handed in — never a script. This
            is the only reason the two orderings give different answers."""
            blocks = [b for b in live_ng.split_chapter_blocks(text or "")
                      if live_ng.chapter_heading_line(b)]
            return ["present" if DRIFT2 in b else "past" for b in blocks]

        async def critique(text, *_a, **_k):
            seen["critique"] += 1
            census = _census_of(text)
            seen["observed"].append(list(census))
            # F8: a clean SEAM census. These fixtures drive the real job, and F8 refuses a
            # multi-chapter book whose seam census is absent — UNPROVED blocks, exactly as
            # every other F6 census does. Declaring a sound census is what these rows always
            # did for tense and teleports; F8 is simply the fourth of them.
            return ({"score": 8, "violations": [], "tense_by_chapter": census,
                     "teleports_by_chapter": [0] * len(census),
                     "seam_states": [{"chapter_a": _i, "chapter_b": _i + 1,
                                      "causal": "explicit", "location": "continuous",
                                      "time": "explicit"}
                                     for _i in range(1, len(census))]}, 0)

        async def _revise(text, _request, *_a, **_k):
            return (text.replace(DRIFT2, PAST[1]), 0)

        async def cheap(*_a, **_k):
            return ("{}", 0)

        async def _assist_repair(result, *_a, **_k):
            """Stands in for the canon-lite L3-assist repair, and does what that seam is
            architecturally allowed to do: REPLACE THE MANUSCRIPT. Here it puts the drift
            back, which is the whole hazard — the gates' repair was real, and the bytes that
            reach the customer no longer contain it."""
            seen["assist_ran"] += 1
            if late_mutation is not None:
                result["book"] = late_mutation(result.get("book") or "")
            return {"ok": True}

        def _scrub(result, _canon):
            if scrub_mutation is not None:
                result["book"] = scrub_mutation(result.get("book") or "")
            return (True, ())

        async def _gen(_req, **_kw):
            return {"ok": True, "book": DRIFTED, "chapters": [{"no": i} for i in (1, 2, 3)]}

        for name, value in CLEARED_ENV.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("NARASI_DIET_MAX_LOOPS", "0")
        monkeypatch.setenv("NARASI_REGISTER_GATE", "0")
        monkeypatch.setenv("NARASI_THREAD_TRACKER", "0")
        # F6 arms ONLY on the literal "1" — absent no longer means ON, so a fixture
        # that wants the gate running has to say so.
        monkeypatch.setenv("NARASI_F6_ENABLED", "1")
        monkeypatch.delenv("NARASI_OBSERVABILITY_ENABLED", raising=False)
        monkeypatch.setattr(live_lz, "make_narasi_client",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("a provider client was built")))
        monkeypatch.setattr(live_lz, "_narasi_cheap_call", cheap)
        monkeypatch.setattr(live_lz, "_narasi_critique_enabled", lambda: True)
        monkeypatch.setattr(live_lz, "_narasi_critique_revise_enabled", lambda: True)
        monkeypatch.setattr(live_lz, "NARASI_CRITIQUE_MIN_CHAPTERS", 1)
        monkeypatch.setattr(live_lz, "_narasi_consistency_critique", critique)
        monkeypatch.setattr(live_lz, "_narasi_consistency_revise", _revise)
        monkeypatch.setattr(live_na, "_canon_lite_l3_assist_repair", _assist_repair)
        monkeypatch.setattr(live_na, "scrub_and_verify_generation_leak", _scrub)
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

        _real = live_na._f6_finalize

        async def _spy(result, body, **kw):
            out = await _real(result, body, **kw)
            seen["f6"] = dict(out or {})
            return out

        monkeypatch.setattr(live_na, "_f6_finalize", _spy)

        async def drive():
            before = set(asyncio.all_tasks())
            await live_na._run_narration_job_after_parity(
                body={"chapters": [{"word_target": 400}] * 3, "style": "storytelling",
                      "language": "id"},
                job_id="j-order", job_uuid=None, tenant_id=CANARY, user_id="u", total=3,
                meter_op="op", model="m", executor="narration_worker")
            survivors = (set(asyncio.all_tasks()) - before) - {asyncio.current_task()}
            if survivors:
                await asyncio.gather(*survivors, return_exceptions=True)

        asyncio.run(drive())
        return seen

    return run


def _statuses(seen):
    return [f["status"] for f in seen["finalize"]]


# ---------------------------------------------------------------------------
# The seam actually runs — without this every row below would pass vacuously
# ---------------------------------------------------------------------------
def test_the_l3_assist_seam_really_runs_on_this_job(job):
    """🔴 THE TRIPWIRE FOR THE TRIPWIRE. If assist never arms, the "late mutation" never
    happens and the rows below would prove nothing while staying green."""
    seen = job()
    assert seen["assist_ran"] == 1, (
        "the L3-assist seam never ran — this file's whole premise is unarmed")


def test_the_ordinary_assist_job_still_delivers(job):
    """The positive half: assist armed, the gates' repair genuine, nothing mutated late."""
    seen = job()
    na = _live("narration_api")
    assert seen["f6"]["violations_resolved"] == 1, seen["f6"]
    assert seen["f6"]["delivery_blocked"] is False
    assert na._STATUS_DONE in _statuses(seen)
    assert seen["persisted"] is not None


# ---------------------------------------------------------------------------
# The behavioural ordering witness
# ---------------------------------------------------------------------------
def test_a_drift_reintroduced_by_the_l3_assist_repair_blocks_delivery(job):
    """🔴 THE ROW THE STRUCTURAL WITNESS COULD NOT WRITE. The gates repaired chapter 2 and the
    accounting could honestly have called it resolved. Then the L3-assist seam replaced the
    manuscript and the drift is back in the bytes headed for the customer's row.

    Finalising after that seam, the census read from the DELIVERED text still says `present`,
    the verifier refuses, and the job fails. Finalising before it, the same census says `past`,
    the violation resolves, and the defect ships — which is what this asserts against."""
    seen = job(late_mutation=lambda book: book.replace(PAST[1], DRIFT2))
    na = _live("narration_api")
    assert seen["assist_ran"] == 1
    assert seen["observed"][-1] == ["past", "present", "past"], (
        "the verification pass did not read the manuscript the assist seam left behind — "
        "the finaliser ran too early")
    assert seen["f6"]["violations_resolved"] == 0, seen["f6"]
    assert seen["f6"]["violations_unresolved"] == 1
    assert seen["f6"]["delivery_blocked"] is True
    assert _statuses(seen) == [na._STATUS_FAILED]
    assert seen["finalize"][0]["error"] == "f6_unresolved_hard_violation"
    assert seen["refund"] == 1
    assert seen["persisted"] is None
    assert seen["settle"] == 0


def test_a_drift_reintroduced_by_the_f1_scrub_stage_blocks_delivery(job):
    """The SECOND mutator after the gates. F1's scrub rewrites `result` too, and a finaliser
    placed between the assist repair and the scrub would pass the row above while still
    shipping this one."""
    seen = job(scrub_mutation=lambda book: book.replace(PAST[1], DRIFT2))
    na = _live("narration_api")
    assert seen["observed"][-1] == ["past", "present", "past"], (
        "the verification pass did not read the manuscript the F1 scrub left behind")
    assert seen["f6"]["delivery_blocked"] is True
    assert _statuses(seen) == [na._STATUS_FAILED]
    assert seen["persisted"] is None


def test_a_late_mutation_to_another_chapter_is_collateral_not_a_resolution(job):
    """🔴 THE DISCRIMINATING CASE FOR "WHICH BYTES". Reverting chapter 2 blocks under either
    ordering once the observer is honest; here the repair is genuine AND the assist seam edits
    chapter 3. Reading the delivered bytes reports `chapters_changed=2, collateral=[3]`;
    reading the gates' book reports `1` and `[]`. Same refusal, different books — only the
    accounting says which one was measured."""
    seen = job(late_mutation=lambda book: book.replace(
        PAST[2], "Tae-jun menandatangani deposisi lalu melangkah keluar menembus hujan."))
    assert seen["f6"]["chapters_changed"] == 2, seen["f6"]
    assert seen["f6"]["collateral_chapters"] == [3], seen["f6"]
    assert seen["f6"]["delivery_blocked"] is True
    assert seen["persisted"] is None


def test_the_delivered_bytes_are_the_ones_that_were_accounted(job):
    """The positive counterpart: what persistence received is exactly what the verification
    pass read, with no room between them for another mutator."""
    seen = job()
    ng = _live("narasi_gate")
    delivered = (seen["persisted"] or {}).get("book") or ""
    blocks = [b for b in ng.split_chapter_blocks(delivered) if ng.chapter_heading_line(b)]
    assert [("present" if DRIFT2 in b else "past") for b in blocks] == seen["observed"][-1]
    assert DRIFT2 not in delivered
