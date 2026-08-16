"""F6 hard block — observed on the REAL job path, not inferred from a call order.

🔴 WHY THIS FILE EXISTS. The accounting can say `delivery_blocked=True` all it likes; what
   matters is what the job DOES. Until this file, the only witness that the block was wired
   at all was a source-order assertion — deleting the `if delivery_blocked` branch, the
   `return`, the refund or the FAILED finalize would not have turned a single test red.

   So these drive `_run_narration_job_after_parity` itself and watch the four things that
   constitute a refusal:

       _finalize(status=FAILED, error="f6_unresolved_hard_violation")
       _refund(...) called
       _persist_chapters(...) NOT called
       _finalize(status=DONE) NOT called

   and, for the positive path, the exact opposite.

🔴 ONE DOOR. The refusal reuses F1's existing fail+refund+return shape rather than adding a
   second delivery-block mechanism; two mechanisms for one rule is how this workstream has
   repeatedly ended up with neither being provable.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest


def _live(name):
    import importlib
    return sys.modules.get(name, importlib.import_module(name))


BOOK = "\n\n".join(f"## Bab {i}: T{i}\n\nprosa bab {i} yang cukup panjang." for i in (1, 2, 3))


@pytest.fixture
def job(monkeypatch):
    """Run the real job body and report every delivery-relevant call it made."""
    na = _live("narration_api")
    seen = {"finalize": [], "refund": 0, "persisted": None, "settle": 0}

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

    def run(*, f6_accounting):
        async def _gen(_req, **_kw):
            return {"ok": True, "book": BOOK, "chapters": [{"no": 1}]}

        async def _fake_f6_finalize(result, _body, **_kw):
            result["f6"] = dict(f6_accounting)
            result.pop("_f6_pending", None)
            return dict(f6_accounting)

        monkeypatch.setattr(na, "generate_narration", _gen)
        monkeypatch.setattr(na, "_cancel_watcher", lambda *a, **k: asyncio.sleep(3600))
        monkeypatch.setattr(na, "_finalize", _finalize)
        monkeypatch.setattr(na, "_set_status", _anoop)
        monkeypatch.setattr(na, "_safe_progress", _anoop)
        monkeypatch.setattr(na, "_settle", _settle)
        monkeypatch.setattr(na, "_refund", _refund)
        monkeypatch.setattr(na, "_apply_v3_gates", _anoop)
        monkeypatch.setattr(na, "_reconcile_checkboxes", _anoop)
        monkeypatch.setattr(na, "_persist_chapters", _persist)
        # The finaliser itself has its own suite; here the ACCOUNTING is the input and the
        # delivery decision is what is under test.
        monkeypatch.setattr(na, "_f6_finalize", _fake_f6_finalize)
        monkeypatch.setattr(na, "credits_lib", types.SimpleNamespace(touch_hold=_anoop))
        monkeypatch.setattr(na, "db", types.SimpleNamespace(
            get_known_bad_claims=_anoop, get_known_good_claims=_anoop, log_usage=_anoop,
            checkpoint_narasi_meter=_anoop))

        async def drive():
            before = set(asyncio.all_tasks())
            await na._run_narration_job_after_parity(
                body={"chapters": [{"word_target": 400}]}, job_id="j-f6", job_uuid=None,
                tenant_id="t", user_id="u", total=1, meter_op="op", model="m",
                executor="narration_worker")
            survivors = (set(asyncio.all_tasks()) - before) - {asyncio.current_task()}
            if survivors:
                await asyncio.gather(*survivors, return_exceptions=True)

        asyncio.run(drive())
        return seen

    return run


CLEAN = {"violations_detected": 1, "violations_resolved": 1, "violations_unresolved": 0,
         "delivery_blocked": False}
BLOCKED = {"violations_detected": 1, "violations_resolved": 0, "violations_unresolved": 1,
           "delivery_blocked": True, "unresolved_ids": ["tense_drift:2"]}


def _statuses(seen):
    return [f["status"] for f in seen["finalize"]]


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------
def test_an_unresolved_violation_finalises_the_job_as_FAILED(job):
    seen = job(f6_accounting=BLOCKED)
    na = _live("narration_api")
    assert _statuses(seen) == [na._STATUS_FAILED]
    assert seen["finalize"][0]["error"] == "f6_unresolved_hard_violation"


def test_an_unresolved_violation_refunds_the_hold(job):
    assert job(f6_accounting=BLOCKED)["refund"] == 1


def test_an_unresolved_violation_never_persists_the_chapters(job):
    """🔴 THE POINT OF THE WHOLE GATE. The manuscript with the unrepaired defect must not
    reach the customer's row."""
    assert job(f6_accounting=BLOCKED)["persisted"] is None


def test_an_unresolved_violation_never_finalises_DONE(job):
    na = _live("narration_api")
    assert na._STATUS_DONE not in _statuses(job(f6_accounting=BLOCKED))


def test_an_unresolved_violation_never_settles_the_charge(job):
    """The job returns before settlement; a blocked delivery is refunded, not billed."""
    assert job(f6_accounting=BLOCKED)["settle"] == 0


# ---------------------------------------------------------------------------
# ...and the positive path, so the gate is not merely "block everything"
# ---------------------------------------------------------------------------
def test_a_resolved_book_is_persisted_and_finalised_DONE(job):
    seen = job(f6_accounting=CLEAN)
    na = _live("narration_api")
    assert na._STATUS_DONE in _statuses(seen)
    assert na._STATUS_FAILED not in _statuses(seen)
    assert seen["persisted"] is not None
    assert seen["refund"] == 0
    assert seen["settle"] == 1


def test_the_accounting_reaches_the_delivered_payload(job):
    seen = job(f6_accounting=CLEAN)
    assert (seen["persisted"] or {}).get("f6", {}).get("violations_resolved") == 1


def test_the_private_pending_state_is_never_persisted(job):
    seen = job(f6_accounting=CLEAN)
    assert "_f6_pending" not in (seen["persisted"] or {})
