"""L3 END TO END ON THE PRODUCTION BRANCH — no injected hooks, real session.

Every other L3 file installs `na._L3_REPAIR_PROVIDER` / `na._L3_CHAPTER_EXTRACTOR` before
driving the seam. Production installs neither: they are test injection points and are
`None` in a real job, so the real job takes the OTHER branch — the one that builds an
`L3AssistSession` from the metered wave's own provider. No test ever executed it.

Two production defects lived in that unexecuted branch, and both were found by canaries
rather than by this suite:

  · `canon_lite_qc_runner` referenced `api_key`, never bound -> NameError in the default
    adapter factory -> `l3_metered_wave_error` (job `yp8f04rr`);
  · `narration_api` referenced `_cl`, never imported -> NameError building the session
    -> `l3_session_error` (job `98o7l3o8`), AFTER the metered wave had emitted 9 rows.

Both were swallowed by `except Exception` blocks that discard the exception's identity, so
the telemetry named a lifecycle stage (`no_session`) where the truth was a missing name.
A branch no test enters is a branch whose bugs are found in production, by definition.

This file enters it. The hooks stay `None`, the wave really hands its session over, the
real `L3AssistSession` is constructed, and the only things faked are the two OUTBOUND
boundaries: `orchestrator.static.run_worker` (repair generation) and the metered QC
provider (re-extraction). Everything between — session construction, budget accounting,
snapshot framing, the extractor preflight, claim parsing, acceptance, substitution,
chapter-record sync and the delivery-binding read-back — runs for real.

No network.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import canon_lite as cl                        # noqa: E402
import canon_lite_l2 as l2                     # noqa: E402
import canon_lite_qc_runner as qcr             # noqa: E402
import narration_api as na                     # noqa: E402

from test_canon_lite_l3_repair_stage2 import (  # noqa: E402
    BAD_NAME, BOOK, GOOD_NAME, _claims_for, _entity_canon)

CANARY = "t-canary"


@pytest.fixture(autouse=True)
def _production_shape(monkeypatch):
    """Allowlist the tenant AND pin both hooks to None — the production shape."""
    monkeypatch.setenv("NARASI_CANON_LITE_ASSIST_TENANTS", CANARY)
    before = (na._L3_REPAIR_PROVIDER, na._L3_CHAPTER_EXTRACTOR)
    na._L3_REPAIR_PROVIDER = None
    na._L3_CHAPTER_EXTRACTOR = None
    yield
    na._L3_REPAIR_PROVIDER, na._L3_CHAPTER_EXTRACTOR = before


def _real_result(book=BOOK):
    """The shape the job builds: `book` is the assembly, `chapters[*].content` the rows.
    Both are live — the payload ships the book, `_persist_chapters` stores the rows."""
    bodies = [seg.split("\n", 1)[1].rstrip("\n") for seg in book.split("## ")[1:]]
    return {
        "book": book,
        "chapters": [{"no": i, "ok": True, "content": body}
                     for i, body in enumerate(bodies)],
        "n_ok": len(bodies), "n_total": len(bodies),
    }


class _MeteredSpy:
    """Stands in for the job's metered QC provider — the ONE outbound call in
    re-extraction. Returns the raw provider shape `_provider_payload` accepts."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, request):
        self.calls += 1
        body = request.chapter_bytes
        start = body.find(GOOD_NAME.encode("utf-8"))
        assert start >= 0, "the candidate handed to re-extraction was not repaired"
        end = start + len(GOOD_NAME.encode("utf-8"))
        # 🔴 RAW PROVIDER SHAPE, NOT THE TYPED ARTEFACT. `_coverage()` from the stage-2
        #    helpers builds the TUPLE that goes inside a `ChapterClaimsV1`; what crosses
        #    this boundary is the provider's JSON, where coverage is a MAPPING keyed by
        #    predicate. Handing over the tuple makes `parse_chapter_claims` raise, the
        #    engine treats the raise as a provider fault, and the chapter comes back
        #    unrepaired with no explanation — which is exactly how this test first failed.
        #    Coverage is CHECKED only for the predicate that has claims; the schema
        #    rejects CHECKED with none, and rightly so.
        return {
            "coverage": {
                p: (l2.COVERAGE_CHECKED if p == "entity_name_contradiction"
                    else l2.COVERAGE_NO_CLAIMS_FOUND)
                for p in l2.SEMANTIC_PREDICATES
            },
            "claims": [{
                "claim_type": l2.CLAIM_ENTITY_MENTION,
                "canon_ref": "e1",
                "evidence_start": start,
                "evidence_end": end,
                "evidence_sha256": cl.sha256_hex(body[start:end]),
            }],
        }


def _drive(monkeypatch):
    """Run the seam exactly as production does. Returns (telemetry, result, counters)."""
    canon = _entity_canon()
    snap = l2.materialize_final_snapshot({"book": BOOK}, canon=canon)
    metered = _MeteredSpy()
    counters = {"waves": 0, "sessions": [], "repairs": 0}

    # Chapter 2 carries the violation: it names BAD_NAME where the canon says GOOD_NAME.
    claims = {
        0: _claims_for(snap.block_bytes(0), index=0,
                       chapter_id=snap.blocks[0].chapter_id, canon=canon,
                       needle=GOOD_NAME),
        1: _claims_for(snap.block_bytes(1), index=1,
                       chapter_id=snap.blocks[1].chapter_id, canon=canon,
                       needle=BAD_NAME),
    }

    async def _fake_wave(*_a, on_session=None, **_k):
        counters["waves"] += 1
        # 🔴 THE HANDOFF IS THE POINT. A fake that returns claims without calling
        #    `on_session` leaves the seam with no metered session, sends it down the
        #    `no_session` path, and never constructs the object this file exists to build.
        assert on_session is not None, "the seam stopped passing on_session"
        on_session(metered)
        counters["sessions"].append(metered)
        return claims

    monkeypatch.setattr(qcr, "maybe_run_metered_wave", _fake_wave)

    repaired_body = snap.block_bytes(1).replace(
        BAD_NAME.encode("utf-8"), GOOD_NAME.encode("utf-8")).decode("utf-8")

    import orchestrator.static as st

    async def _fake_run_worker(worker, prompt, timeout=None, task_id=None):
        counters["repairs"] += 1
        return {"ok": True, "output": repaired_body}

    monkeypatch.setattr(st, "run_worker", _fake_run_worker)

    result = _real_result()
    telemetry = asyncio.run(na._canon_lite_l3_assist_repair(
        result, mode="assist", canon=canon, wave_token=None, run_id="j1",
        tenant_id=CANARY))
    return telemetry, result, counters, metered


def test_the_production_branch_repairs_and_binds_to_the_delivered_bytes(monkeypatch):
    telemetry, result, counters, metered = _drive(monkeypatch)

    # -- the branch was actually entered ------------------------------------
    assert na._L3_REPAIR_PROVIDER is None and na._L3_CHAPTER_EXTRACTOR is None, \
        "the hooks were installed — this ran the branch production never takes"
    assert telemetry is not None
    assert telemetry.get("stage") == "complete", (
        f"stage={telemetry.get('stage')!r} — on the commit before the `_cl` import this "
        f"is 'no_session', because building the session raised NameError")

    # -- exactly one of each, no hidden second wave or duplicate session ----
    assert counters["waves"] == 1
    assert len(counters["sessions"]) == 1
    assert len({id(s) for s in counters["sessions"]}) == 1
    assert counters["repairs"] == 1, "one repairable violation, one repair call"
    assert metered.calls == 1, "re-extraction must ride the job's own metered session"

    # -- the verdict ---------------------------------------------------------
    assert telemetry.get("delivery_binding") == l2.BINDING_MATCH
    assert telemetry.get("l3_status") == "present"

    # -- the bad text is gone from BOTH live copies --------------------------
    assert BAD_NAME not in result["book"], "the delivered book still carries the violation"
    bodies = [c["content"] for c in result["chapters"]]
    assert not any(BAD_NAME in b for b in bodies), (
        "the stored chapter rows still carry the violation — the reader opens these")
    assert GOOD_NAME in result["book"]
    assert any(GOOD_NAME in b for b in bodies)

    # -- the book and the rows are the SAME repaired bytes -------------------
    reassembled = l2.materialize_final_snapshot(result, canon=None)
    assert reassembled is not None
    for i, body in enumerate(bodies):
        assert body in result["book"], f"chapter {i} row is not the text in the book"


def test_the_recorded_outcome_is_bounded_and_matches_the_delivery(monkeypatch):
    """The seam's own record — what an auditor reads — must agree with the payload."""
    telemetry, result, _counters, _m = _drive(monkeypatch)
    recorded = result.get("canon_lite_l3")

    assert isinstance(recorded, dict)
    assert recorded["stage"] == "complete"
    assert recorded["delivery_binding"] == l2.BINDING_MATCH
    assert recorded["outcome"] in na.L3_OUTCOMES
    assert recorded["manuscript_changed"] is True, (
        "a repair was accepted and substituted; the record must say the text moved")
    assert recorded["chapters_repaired"] >= 1
    # Bounded: no prose, no provider text, no names.
    blob = repr(recorded)
    for leak in (GOOD_NAME, BAD_NAME, "Namanya", "desa", "kota"):
        assert leak not in blob


def test_the_hash_in_the_record_describes_the_manuscript_that_shipped(monkeypatch):
    """The whole point of the binding: the bytes the verdict was computed over are the
    bytes the reader receives. Recompute the digest off the delivered payload."""
    _telemetry, result, _counters, _m = _drive(monkeypatch)
    recorded = result["canon_lite_l3"]

    delivered = l2.materialize_final_snapshot(result, canon=None)
    assert delivered is not None
    assert recorded["manuscript_sha256"] == delivered.manuscript_sha256, (
        "the recorded hash does not describe the manuscript on the delivery path")
