"""F6 `observe` — measured, reported, and byte-for-byte inert.

🔴 THE PROPERTY THE WHOLE MODE RESTS ON. `observe` must be indistinguishable from `off` in
   everything the customer can see: the same manuscript bytes, the same durable payload, the same
   terminal status — while producing a record an operator can adjudicate. A mode that reports AND
   changes something is not a reporting mode, it is an enforcement mode with a quieter log.

🔴 AND A REPORT IS NOT A VERDICT. `would_block` may only be filled from a final census that
   actually read the delivered bytes. An unsampled job, or one whose census call failed, has
   measured nothing after the mutators and must say so rather than guess.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
import types

import pytest


def _live(name):
    return sys.modules.get(name) or importlib.import_module(name)


BODIES = ("Eun-soo membuka pintu kantor lama itu dan menghitung berkas yang tersisa.",
          "Min-jae menunggu di lobi sambil membaca daftar saksi yang belum bersedia.",
          "Tae-jun menutup rapat itu lalu berjalan keluar melewati koridor sepi.")
TITLES = ("Kembali", "Lobi", "Koridor")
OUTLINE = {1: ["Eun-soo returns"], 2: ["Min-jae waits", "Tae-jun signs the deposition"],
           3: ["Tae-jun closes the hearing"]}


def _packet(beats):
    return ("CURRENT ORDERED OUTLINE BEATS:\n"
            + "".join(f"  {i}. {b}\n" for i, b in enumerate(beats, 1)) + "\n")


AUTHORITY = {"text": "NARRATIVE AUTHORITY: accepted outline.",
             "outline_packets_by_chapter": {str(k): _packet(v) for k, v in OUTLINE.items()}}

#: chapter 2 beat 2 never happens — a `beat_execution`, the canary's class.
BEATS_DIRTY = [{"chapter": 1, "beat": 1, "state": "executed"},
               {"chapter": 2, "beat": 1, "state": "executed"},
               {"chapter": 2, "beat": 2, "state": "promised"},
               {"chapter": 3, "beat": 1, "state": "executed"}]
TENSE_OK = ["past", "past", "past"]
TELE_OK = [0, 0, 0]
SEAMS_OK = [{"chapter_a": i, "chapter_b": i + 1, "causal": "explicit",
             "location": "continuous", "time": "explicit"} for i in (1, 2)]
BODY = {"chapters": [{"word_target": 100}] * 3, "style": "storytelling", "language": "id"}

#: An unrelated finding the OTHER gates repair, so the manuscript really is mutated and a final
#: census is genuinely warranted — otherwise `observe` would be trivially inert.
CRITIC_FINDING = {"type": "timeline", "severity": "high", "chapter": 1,
                  "evidence": "unrelated finding @ch1", "fix": "fix it"}


def _book():
    return "\n\n".join(f"## Bab {i}: {t}\n\n{b}"
                       for i, (t, b) in enumerate(zip(TITLES, BODIES), 1)) + "\n"


@pytest.fixture
def job(monkeypatch):
    live_lz, live_na = _live("laozhang_api"), _live("narration_api")

    def blocked(*_a, **_k):
        raise AssertionError("a provider client was built — this is not offline")

    def run(*, mode=None, enabled="1", sample_rate="1.0", census_fails=False,
            revise_changes=True, job_uuid="11111111-1111-4111-8111-111111111111",
            critic_on=True):
        seen = {"finalize": [], "persisted": None, "payload": None, "refund": 0,
                "critique_texts": [], "revise": 0, "beat": 0, "reduce": 0, "teleport": 0,
                "seam": 0, "usage_rows": 0, "sink_credits": 0, "final_book": None}

        async def _anoop(*_a, **_k):
            return None

        async def _finalize(job_id, job_uuid_, tenant_id, *, status, result=None, error=None):
            seen["finalize"].append({"status": status, "error": error})
            if result is not None:
                seen["payload"] = dict(result)

        async def _refund(*_a, **_k):
            seen["refund"] += 1

        async def _persist(_t, _j, res, *_a, **_k):
            seen["persisted"] = dict(res or {})

        async def critique(text, *_a, authority_text="", usage_row=True, usage_out=None, **_k):
            seen["critique_texts"].append(text)
            first = len(seen["critique_texts"]) == 1
            if usage_row:
                seen["usage_rows"] += 1
            if census_fails and not first:
                return ({}, 0)          # the helper never raises; a failure is an empty verdict
            if isinstance(usage_out, dict):
                usage_out.update({"served_provider": "kie", "served_model": "m",
                                  "tokens_in": 900, "tokens_out": 120, "cost_usd": "0.002"})
            out = {"score": 8, "violations": [dict(CRITIC_FINDING)] if first else [],
                   "tense_by_chapter": list(TENSE_OK), "teleports_by_chapter": list(TELE_OK),
                   "seam_states": [dict(s) for s in SEAMS_OK]}
            if str(authority_text or "").strip():
                out["beat_states"] = [dict(b) for b in BEATS_DIRTY]
            return (out, 11)

        async def _revise(text, _req, *_a, **_k):
            seen["revise"] += 1
            return ((text.replace("koridor sepi", "koridor yang sepi") if revise_changes
                     else text), 3)

        async def _beat(chapter_body, **_k):
            seen["beat"] += 1
            return (chapter_body + " Ia menandatangani deposisi itu.", 0)

        async def _reduce(chapter_text, **_k):
            seen["reduce"] += 1
            return (chapter_text, 0)

        async def _seam(chapter_b_body, **_k):
            seen["seam"] += 1
            return (chapter_b_body + " JEMBATAN.", 0)

        async def cheap(*_a, **_k):
            return ("{}", 0)

        async def _gen(_req, **_kw):
            return {"ok": True, "book": _book(),
                    "chapters": [{"no": i} for i in range(1, 4)],
                    "_narrative_authority": dict(AUTHORITY)}

        # 🔴 THE SETTLEMENT TOTAL IS THE THING THE CUSTOMER PAYS, so the test has to be able to
        # read it. Without capturing the real sink, "observe never bills its census" was an
        # assertion about a number nothing in this fixture could see.
        _real_sink_cls = live_na._UsageSink
        sinks = []

        class _SpySink(_real_sink_cls):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                sinks.append(self)

        monkeypatch.setattr(live_na, "_UsageSink", _SpySink)

        for key, value in (("NARASI_F6_ENABLED", enabled), ("NARASI_F6_MODE", mode),
                           ("NARASI_F6_OBSERVE_SAMPLE_RATE", sample_rate)):
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        monkeypatch.setenv("NARASI_DIET_MAX_LOOPS", "0")
        monkeypatch.setenv("NARASI_REGISTER_GATE", "0")
        monkeypatch.setenv("NARASI_THREAD_TRACKER", "0")
        monkeypatch.setattr(live_lz, "make_narasi_client", blocked)
        monkeypatch.setattr(live_lz, "_narasi_cheap_call", cheap)
        # 🔴 PRODUCTION'S DEFAULT IS OFF. `_narasi_critique_enabled()` defaults to "0", so the
        # branch where F6 has no critic census to reuse is the NORMAL one — and it is the branch
        # where F6 used to buy its own read and bill it. A fixture that only ever runs with the
        # critic ON cannot see that, which is exactly how it went unnoticed.
        monkeypatch.setattr(live_lz, "_narasi_critique_enabled", lambda: critic_on)
        monkeypatch.setattr(live_lz, "_narasi_critique_revise_enabled", lambda: True)
        monkeypatch.setattr(live_lz, "NARASI_CRITIQUE_MIN_CHAPTERS", 1)
        monkeypatch.setattr(live_lz, "_narasi_consistency_critique", critique)
        monkeypatch.setattr(live_lz, "_narasi_consistency_revise", _revise)
        monkeypatch.setattr(live_lz, "_narasi_beat_execution_repair", _beat)
        monkeypatch.setattr(live_lz, "_narasi_chapter_reduce", _reduce)
        monkeypatch.setattr(live_lz, "_narasi_seam_repair", _seam)
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
                body=dict(BODY), job_id="j-obs", job_uuid=job_uuid, tenant_id="t", user_id="u",
                total=3, meter_op="op", model="m", executor="narration_worker")
            for t in (set(asyncio.all_tasks()) - before) - {asyncio.current_task()}:
                await asyncio.gather(t, return_exceptions=True)

        asyncio.run(drive())
        seen["final_book"] = (seen["persisted"] or {}).get("book") or \
                             (seen["payload"] or {}).get("book")
        seen["sink_credits"] = sum(int(getattr(s_, "credits", 0) or 0) for s_ in sinks)
        return seen

    return run


def _status(seen):
    return seen["finalize"][-1]["status"] if seen["finalize"] else None


def _record(caplog):
    """The one observe telemetry record, parsed back out of the log."""
    for rec in caplog.records:
        msg = rec.getMessage()
        if msg.startswith("F6 observe: "):
            return json.loads(msg[len("F6 observe: "):])
    return None


# ---------------------------------------------------------------------------
# 1, 3 — observe never publishes a block; the finding survives as would_block
# ---------------------------------------------------------------------------
def test_observe_never_publishes_delivery_blocked(job, caplog):
    caplog.set_level(logging.WARNING)
    na = _live("narration_api")
    seen = job(mode="observe")
    assert _status(seen) == na._STATUS_DONE, "observe refused a delivery"
    assert seen["refund"] == 0
    for key in ("f6", "f8"):
        assert (seen["payload"] or {}).get(key) is None, \
            f"a {key} verdict reached the durable payload"
        assert (seen["persisted"] or {}).get(key) is None


def test_the_finding_is_reported_as_would_block_not_as_a_verdict(job, caplog):
    caplog.set_level(logging.WARNING)
    seen = job(mode="observe")
    rec = _record(caplog)
    assert rec is not None, "observe produced no telemetry record"
    assert rec["would_block"] is True, "the beat_execution finding was not reported"
    assert rec["violations_unresolved"] >= 1
    assert rec["accounting_status"] == "telemetry_only_unbooked"
    assert _status(seen) == _live("narration_api")._STATUS_DONE


def test_the_record_carries_producer_locator_and_routability(job, caplog):
    caplog.set_level(logging.WARNING)
    job(mode="observe")
    rec = _record(caplog)
    beat = next(f for f in rec["findings"] if f["class"] == "beat_execution")
    assert beat["violation_id"] == "beat_execution:2:outline_beat:2|2"
    assert beat["producer"] == "f6_scan"
    assert beat["locator"] == {"chapter": 2, "outline_chapter": 2, "outline_beat": 2}
    assert beat["lane"] == "f6_beat_execution_actuator"
    assert beat["routable"] is True


# ---------------------------------------------------------------------------
# 2 — no telemetry in anything the customer receives
# ---------------------------------------------------------------------------
def test_no_internal_telemetry_reaches_the_user_payload(job):
    seen = job(mode="observe")
    for payload in (seen["payload"] or {}, seen["persisted"] or {}):
        # `f8` belongs here as much as `f6` does: it is F8's internal accounting, and in
        # `observe` it must be as absent as it is under `off`.
        for key in ("f6", "f8", "_f6_pending", "_f6_beat_repair", "_f8_pending",
                    "_f8_post_observation", "_f8_seam_repair"):
            assert key not in payload, f"{key} leaked into a delivered payload"


# ---------------------------------------------------------------------------
# 4 — enforce is untouched
# ---------------------------------------------------------------------------
def test_enforce_still_repairs_and_still_blocks(job):
    na = _live("narration_api")
    seen = job(mode="enforce")
    assert seen["beat"] == 1, "enforce stopped running the beat actuator"
    assert (seen["payload"] or {}).get("f6") is not None, "enforce stopped publishing f6"
    assert _status(seen) == na._STATUS_FAILED, "the unresolved beat stopped blocking"
    assert seen["refund"] == 1


def test_legacy_enabled_without_a_mode_is_still_enforce(job):
    na = _live("narration_api")
    seen = job(enabled="1", mode=None)
    assert seen["beat"] == 1
    assert _status(seen) == na._STATUS_FAILED


# ---------------------------------------------------------------------------
# 5 — off runs no census at all
# ---------------------------------------------------------------------------
def test_off_runs_no_f6_census_and_no_actuator(job):
    seen = job(enabled="0", mode="observe")
    assert seen["beat"] == 0 and seen["reduce"] == 0 and seen["seam"] == 0
    assert (seen["payload"] or {}).get("f6") is None
    # the ONLY critique is the legacy critic's own detection pass; F6 buys nothing and
    # verifies nothing.
    assert len(seen["critique_texts"]) == 1, "F6 bought a census with the brake on"


# ---------------------------------------------------------------------------
# observe changes NOTHING a customer can see
# ---------------------------------------------------------------------------
def test_observe_delivers_the_same_bytes_as_off(job):
    off = job(enabled="0", mode="observe")
    obs = job(mode="observe")
    assert off["final_book"], "the fixture delivered no manuscript"
    assert obs["final_book"] == off["final_book"], "observe changed the delivered bytes"
    assert _status(obs) == _status(off)


def test_observe_runs_no_actuator_repair_splice_or_resync(job):
    seen = job(mode="observe")
    assert seen["beat"] == 0, "the beat actuator ran in observe"
    assert seen["reduce"] == 0, "the ceiling reducer ran in observe"
    assert seen["seam"] == 0, "the F8 seam actuator ran in observe"
    # the merged revise still runs for the OTHER gates' own finding — F6 must not add to it
    assert seen["revise"] == 1


def test_observe_never_folds_its_census_into_the_settlement(job, caplog):
    """🔴 READ THE NUMBER THE CUSTOMER PAYS. The census double returns cr=11 on every call, so
    an `observe` run that folded its census into the sink would show it here — and `enforce`,
    which legitimately does fold, is the control that proves the assertion can move."""
    caplog.set_level(logging.WARNING)
    off = job(enabled="0", mode="observe")
    seen = job(mode="observe")
    rec = _record(caplog)
    assert seen["usage_rows"] == 1, "the observation census wrote a usage row"
    # 🔴 THE COMPARISON IS AGAINST `off`, NOT AGAINST ZERO. The other gates legitimately bill —
    # the legacy critic and the merged revise are the customer's work. What must not move is the
    # total, because F6's observation is the only thing `observe` adds.
    assert off["sink_credits"] > 0, "the control is inert — no gate billed anything at all"
    assert seen["sink_credits"] == off["sink_credits"], \
        "the observation census reached the settlement total"
    assert rec["usage"]["served_provider"] == "kie"
    assert rec["usage"]["tokens_in"] == 900


# ---------------------------------------------------------------------------
# 6 — sampled buys exactly one final census; unsampled buys none
# ---------------------------------------------------------------------------
def test_a_sampled_job_buys_exactly_one_final_census(job, caplog):
    caplog.set_level(logging.WARNING)
    seen = job(mode="observe", sample_rate="1.0")
    # one detection read (the critic's own) + one final census, and no more
    assert len(seen["critique_texts"]) == 2
    assert _record(caplog)["final_census_status"] == "ok"


def test_an_unsampled_job_buys_no_provider_call_and_claims_no_verdict(job, caplog):
    caplog.set_level(logging.WARNING)
    na = _live("narration_api")
    seen = job(mode="observe", sample_rate="0.0")
    assert len(seen["critique_texts"]) == 1, "an unsampled job bought a final census"
    rec = _record(caplog)
    assert rec["sampled"] is False
    assert rec["final_census_status"] == "not_sampled"
    assert rec["would_block"] is None, "an unsampled job claimed a verdict"
    assert _status(seen) == na._STATUS_DONE


def test_observe_never_buys_its_own_initial_census(job, caplog):
    """🔴 THE BRANCH PRODUCTION ACTUALLY TAKES. With no critic to reuse, F6 used to buy its own
    detection read on EVERY job and fold the cost into `sink.credits` — a reporting mode billing
    every customer for a measurement, with sampling applying only to the other call."""
    caplog.set_level(logging.WARNING)
    na = _live("narration_api")
    seen = job(mode="observe", critic_on=False, sample_rate="0.0")
    assert seen["critique_texts"] == [], "observe bought a census with no critic running"
    rec = _record(caplog)
    assert rec["initial_census_status"] == "unavailable_no_critic"
    assert rec["would_block"] is None, "a verdict was claimed with no census at all"
    assert _status(seen) == na._STATUS_DONE, "observe refused a delivery it never measured"
    assert seen["refund"] == 0


def test_observe_reuses_the_critics_read_when_there_is_one(job, caplog):
    caplog.set_level(logging.WARNING)
    job(mode="observe", critic_on=True, sample_rate="0.0")
    assert _record(caplog)["initial_census_status"] == "reused_critic"


def test_enforce_still_buys_its_own_initial_census(job):
    """The gate that refuses deliveries still pays to measure them — unchanged."""
    seen = job(mode="enforce", critic_on=False)
    assert len(seen["critique_texts"]) >= 1, "enforce stopped buying its detection census"


def test_sampling_is_deterministic_for_the_same_job_uuid(job, caplog):
    na = _live("narration_api")
    uuid_a = "22222222-2222-4222-8222-222222222222"
    first = na._f6_observe_sampled(uuid_a, rate=0.5)
    assert all(na._f6_observe_sampled(uuid_a, rate=0.5) is first for _ in range(20))


# ---------------------------------------------------------------------------
# 7 — the record is pinned to the bytes that were delivered
# ---------------------------------------------------------------------------
def test_the_censused_bytes_are_the_delivered_bytes(job, caplog):
    caplog.set_level(logging.WARNING)
    na = _live("narration_api")
    seen = job(mode="observe")
    rec = _record(caplog)
    assert rec["final_census_bytes_digest"] == na._f6_bytes_digest(seen["final_book"]), \
        "the final census read bytes the customer did not receive"
    # and the text handed to the census really is the delivered manuscript
    assert seen["critique_texts"][-1] == seen["final_book"]


# ---------------------------------------------------------------------------
# 8 — a provider failure neither blocks nor changes the delivery
# ---------------------------------------------------------------------------
def test_no_critic_but_sampled_buys_exactly_one_call_and_it_is_the_final_one(job, caplog):
    """🔴 PRODUCTION'S ACTUAL SHAPE: no critic to reuse, and this job is in the sample.

    The unsampled test proves zero calls, which is also what a mode that silently stopped
    working would show. This is the row that proves the one call it DOES buy is the right one:
    the final census, over the delivered bytes, unbilled."""
    caplog.set_level(logging.WARNING)
    na = _live("narration_api")
    seen = job(mode="observe", critic_on=False, sample_rate="1.0")

    assert len(seen["critique_texts"]) == 1, "expected exactly one census call"
    # …and it is the FINAL one: it was handed the delivered manuscript, not the pre-gate draft.
    assert seen["critique_texts"][0] == seen["final_book"], "the one call was not the final census"
    assert seen["usage_rows"] == 0, "the observation census wrote a usage row"
    assert seen["refund"] == 0

    rec = _record(caplog)
    assert rec["initial_census_status"] == "unavailable_no_critic", "an initial self-census ran"
    assert rec["sampled"] is True
    assert rec["final_census_status"] == "ok"
    assert rec["final_census_bytes_digest"] == na._f6_bytes_digest(seen["final_book"])
    assert rec["accounting_status"] == "telemetry_only_unbooked"
    # a successful final census MAY decide would_block — and the job still delivers
    assert rec["would_block"] in (True, False)
    assert _status(seen) == na._STATUS_DONE


# ---------------------------------------------------------------------------
# the OUTER brake, witnessed on its own contract
# ---------------------------------------------------------------------------
@pytest.fixture
def seams(monkeypatch):
    """Call counters on the production seams F6/F8 enter through.

    🔴 A PAYLOAD CHECK IS NOT A BOUNDARY CHECK. `off` means F6 does not RUN — so detection
    executing, or the finaliser being entered, is already a behaviour change even when the inner
    `_f6_enforcing()` brake goes on to prevent every repair and every refusal. Asserting only on
    the delivered payload cannot see that, which is exactly how a single-edit mutant on the
    outer switch stopped witnessing anything."""
    na, nf8 = _live("narration_api"), _live("narasi_f8")
    counts = {"f6_scan": 0, "f6_publish": 0, "f8_publish": 0, "f8_detect": 0}

    def counted(key, func):
        def wrapper(*a, **k):
            counts[key] += 1
            return func(*a, **k)
        return wrapper

    for key, module, attr in (("f6_scan", na, "_f6_scan"),
                              ("f6_publish", na, "_f6_publish"),
                              ("f8_publish", na, "_f8_publish"),
                              ("f8_detect", nf8, "detect")):
        monkeypatch.setattr(module, attr, counted(key, getattr(module, attr)))
    return counts


def test_off_never_enters_f6_detection_or_the_finaliser(job, seams):
    """🔴 THE OUTER BRAKE'S OWN CONTRACT. With `NARASI_F6_ENABLED=0` the scanner is never run —
    not for detection, not for the final scan — and neither publish door is ever reached."""
    seen = job(enabled="0", mode="observe")
    assert seams["f6_scan"] == 0, "F6 detection/final scan ran with the brake on"
    assert seams["f6_publish"] == 0, "the F6 publish door was reached with the brake on"
    assert seams["f8_detect"] == 0, "the F8 seam census ran with the brake on"
    assert seams["f8_publish"] == 0, "the F8 publish door was reached with the brake on"
    assert _status(seen) == _live("narration_api")._STATUS_DONE


def test_off_stays_off_even_when_a_mode_is_configured(job, seams):
    """The brake outranks the mode variable — and it does so by not running, not by running and
    then declining to act."""
    job(enabled="0", mode="enforce")
    assert seams["f6_scan"] == 0 and seams["f6_publish"] == 0
    assert seams["f8_detect"] == 0 and seams["f8_publish"] == 0


def test_observe_does_enter_both_seams(job, seams):
    """The counterpart that stops the two rows above from passing on a machine where F6 never
    runs at all: in `observe` these seams ARE entered."""
    job(mode="observe")
    assert seams["f6_scan"] >= 1, "observe never ran the scanner"
    assert seams["f6_publish"] == 1, "observe never reached its publish door"
    assert seams["f8_detect"] >= 1


# ---------------------------------------------------------------------------
# instrumentation is isolated: telemetry may not decide a delivery
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("target,label", [
    ("_f6_observe_findings", "record construction"),
    ("_f6_bytes_digest", "record construction (digest)"),
])
def test_a_telemetry_construction_failure_never_blocks(job, monkeypatch, target, label):
    na = _live("narration_api")
    off = job(enabled="0", mode="observe")

    def boom(*_a, **_k):
        raise RuntimeError(f"{label} exploded")

    monkeypatch.setattr(na, target, boom)
    seen = job(mode="observe")
    assert _status(seen) == na._STATUS_DONE, f"{label} failure blocked the delivery"
    assert seen["refund"] == 0
    assert (seen["payload"] or {}).get("f6") is None
    assert seen["final_book"] == off["final_book"], f"{label} failure changed the bytes"


def test_a_telemetry_serialisation_failure_never_blocks(job, monkeypatch):
    na = _live("narration_api")
    off = job(enabled="0", mode="observe")

    def boom(*_a, **_k):
        raise TypeError("not serialisable")

    monkeypatch.setattr("json.dumps", boom)
    seen = job(mode="observe")
    assert _status(seen) == na._STATUS_DONE, "a serialisation failure blocked the delivery"
    assert seen["refund"] == 0
    assert seen["final_book"] == off["final_book"]


def test_a_telemetry_emitter_failure_never_blocks(job, monkeypatch):
    """The emitter is a single seam precisely so it can be broken here — a dead sink for these
    records must cost the records, never the delivery."""
    na = _live("narration_api")
    off = job(enabled="0", mode="observe")

    def boom(*_a, **_k):
        raise RuntimeError("emitter down")

    monkeypatch.setattr(na, "_f6_emit_observe", boom)
    seen = job(mode="observe")
    assert _status(seen) == na._STATUS_DONE, "a dead emitter blocked the delivery"
    assert seen["refund"] == 0
    assert seen["final_book"] == off["final_book"]


def test_enforce_error_semantics_are_not_softened_by_the_observe_guard(job, monkeypatch):
    """🔴 THE ISOLATION IS OBSERVE-ONLY. `enforce` must keep refusing exactly as it did; a guard
    that also swallowed its failures would turn a fail-closed gate fail-open."""
    na = _live("narration_api")
    seen = job(mode="enforce")
    assert _status(seen) == na._STATUS_FAILED
    assert seen["refund"] == 1
    assert (seen["payload"] or {}).get("f6", {}).get("delivery_blocked") is True


# ---------------------------------------------------------------------------
# the record's SCHEMA, not one example of it
# ---------------------------------------------------------------------------
_REQUIRED_KEYS = {
    "mode", "job_id", "sampled", "sample_rate", "job_uuid_digest",
    "initial_census_status", "initial_census_chapters", "final_census_status",
    "findings", "would_block", "would_block_reason",
    "violations_detected", "violations_unresolved", "accounting_status",
}
_FINDING_KEYS = {"violation_id", "class", "producer", "locator", "lane",
                 "routable", "missing_for_routing", "evidence_excerpt"}


@pytest.mark.parametrize("kwargs,expect_status", [
    ({"sample_rate": "1.0"}, "ok"),
    ({"sample_rate": "0.0"}, "not_sampled"),
    ({"sample_rate": "1.0", "census_fails": True}, "unavailable"),
])
def test_the_record_schema_holds_in_every_observe_outcome(job, caplog, kwargs, expect_status):
    caplog.set_level(logging.WARNING)
    job(mode="observe", **kwargs)
    rec = _record(caplog)
    missing = _REQUIRED_KEYS - set(rec)
    assert not missing, f"record is missing {sorted(missing)}"
    assert rec["mode"] == "observe"
    assert rec["final_census_status"] == expect_status
    assert rec["accounting_status"] == "telemetry_only_unbooked"
    assert isinstance(rec["sampled"], bool)
    assert rec["job_uuid_digest"] and len(rec["job_uuid_digest"]) == 16
    if expect_status == "ok":
        assert rec["would_block"] in (True, False)
        assert rec["final_census_bytes_digest"]
        for key in ("served_provider", "served_model", "tokens_in", "tokens_out", "cost_usd"):
            assert key in rec["usage"], f"usage is missing {key}"
    else:
        assert rec["would_block"] is None, "a verdict was claimed without a successful census"
    for finding in rec["findings"]:
        assert _FINDING_KEYS <= set(finding), sorted(_FINDING_KEYS - set(finding))


def test_the_record_never_contains_raw_manuscript(job, caplog):
    """🔴 IDs, HASHES AND A BOUNDED EXCERPT — NEVER THE BOOK. A telemetry stream that
    accumulates customer prose is a different product with different obligations."""
    caplog.set_level(logging.WARNING)
    job(mode="observe")
    raw = json.dumps(_record(caplog))
    for body in BODIES:
        assert body not in raw, "a chapter body reached the telemetry record"
        assert body[:40] not in raw
    for finding in _record(caplog)["findings"]:
        excerpt = finding.get("evidence_excerpt")
        if excerpt:
            assert len(excerpt) <= _live("narration_api")._F6_OBSERVE_EXCERPT_CHARS


def test_a_producer_that_omits_its_chapter_is_recorded_as_unroutable():
    """🔴 THE AUDIT'S WORK-LIST, PROVEN ON THE REAL PRODUCER SHAPE. The thread tracker builds
    this with `chapter_introduced` in hand and writes it into the evidence PROSE as `@chN`
    instead of onto the violation, so the lane it routes to has no server-owned chapter."""
    na = _live("narration_api")
    unresolved_thread = {"type": "unresolved_thread", "severity": "high",
                         "evidence": "\"…the letter is never opened\" @ch3",
                         "_f5_claim": "outline_beat:3|1"}
    rows = na._f6_observe_findings([unresolved_thread])
    assert len(rows) == 1
    assert rows[0]["producer"] == "thread_tracker"
    assert rows[0]["routable"] is False
    assert rows[0]["missing_for_routing"] == ["chapter"]
    assert rows[0]["locator"] is None


def test_a_failed_final_census_still_delivers_the_same_bytes(job, caplog):
    caplog.set_level(logging.WARNING)
    na = _live("narration_api")
    off = job(enabled="0", mode="observe")
    seen = job(mode="observe", census_fails=True)
    rec = _record(caplog)
    assert rec["final_census_status"] == "unavailable"
    assert rec["would_block"] is None, "a failed census still produced a verdict"
    assert _status(seen) == na._STATUS_DONE
    assert seen["refund"] == 0
    assert seen["final_book"] == off["final_book"]
