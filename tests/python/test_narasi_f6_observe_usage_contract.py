"""The F6 `observe` usage contract — a platform measurement that never touches the customer.

🔴 WHAT THIS FILE GUARDS. The observe final census is a real provider call: it costs money and it
   produces COGS metadata worth keeping. It is NOT the customer's work. So it must leave NO row on
   a user-facing surface (`usage_logs` feeds /credits/history), never debit a balance, never touch
   a hold, and never enter a settlement aggregate — while every pre-existing caller keeps writing
   its row byte for byte.

🔴 AND IT MUST NOT LIE ABOUT WHO SERVED IT. `_log_narasi_usage` falls back to a model-prefix guess
   when the response carries no provider. That guess is fine for a usage row; publishing it as
   "this is who served the call" would make the telemetry unadjudicable, which is the one thing
   this record exists to be.
"""
from __future__ import annotations

import asyncio
import types

import pytest

import laozhang_api as lz


def _resp(*, content="{}", tok_in=120, tok_out=40, served_by=None, model=None):
    r = types.SimpleNamespace(
        choices=[types.SimpleNamespace(finish_reason="stop",
                                       message=types.SimpleNamespace(content=content))],
        usage=types.SimpleNamespace(prompt_tokens=tok_in, completion_tokens=tok_out))
    if served_by is not None:
        r._narasi_served_by = served_by
    if model is not None:
        r.model = model
    return r


@pytest.fixture
def billing(monkeypatch):
    """Records every billing-adjacent side effect the logger can produce."""
    seen = {"usage_rows": [], "charges": []}

    async def _log_usage(tenant_id, user_id, model, endpoint, tok_in, tok_out, cost, **kw):
        seen["usage_rows"].append({"model": model, "endpoint": endpoint, "tokens": (tok_in, tok_out),
                                   "credits": kw.get("credits"), "provider": kw.get("provider")})

    async def _charge(tenant_id, cr, **kw):
        seen["charges"].append({"cr": cr})

    monkeypatch.setattr(lz, "db", types.SimpleNamespace(log_usage=_log_usage))
    monkeypatch.setattr(lz, "credits_lib", types.SimpleNamespace(charge=_charge))
    monkeypatch.setattr(lz, "_byok_active", lambda: False)
    monkeypatch.setattr(lz, "_calc_cost", lambda m, i, o: 0.0025)
    monkeypatch.setattr(lz, "catalog", types.SimpleNamespace(credit_cost=lambda *a, **k: 7))
    return seen


# ---------------------------------------------------------------------------
# 1 — backward compatibility: the default is the old behaviour, exactly
# ---------------------------------------------------------------------------
def test_the_default_still_writes_the_usage_row(billing):
    cr = asyncio.run(lz._log_narasi_usage("t", "u", "m", _resp()))
    assert len(billing["usage_rows"]) == 1, "an existing caller stopped writing its row"
    assert billing["usage_rows"][0]["credits"] == 7, "credit_row defaults to True"
    assert cr == 7


def test_credit_row_false_still_writes_a_row_with_zero_credits(billing):
    """The pre-existing gate convention, untouched: the row exists, its credits field is 0, and
    the REAL cr comes back for the caller's own aggregate."""
    cr = asyncio.run(lz._log_narasi_usage("t", "u", "m", _resp(), credit_row=False))
    assert len(billing["usage_rows"]) == 1
    assert billing["usage_rows"][0]["credits"] == 0
    assert cr == 7, "the real cr must still be returned"


# ---------------------------------------------------------------------------
# 2 — observe: no row at all
# ---------------------------------------------------------------------------
def test_usage_row_false_writes_no_usage_row(billing):
    """🔴 `usage_logs` FEEDS /credits/history. A platform measurement must not appear there."""
    asyncio.run(lz._log_narasi_usage("t", "u", "m", _resp(),
                                     credit_row=False, usage_row=False))
    assert billing["usage_rows"] == [], "the observe census wrote a user-visible usage row"


def test_the_observe_path_never_debits_a_balance(billing):
    asyncio.run(lz._log_narasi_usage("t", "u", "m", _resp(),
                                     charge=False, credit_row=False, usage_row=False))
    assert billing["charges"] == []
    assert billing["usage_rows"] == []


def test_suppressing_the_row_does_not_suppress_the_charge_for_other_callers(billing):
    """The two switches are independent, and this is the sharp edge: nothing about `usage_row`
    may quietly turn a charging caller into a free one, or the reverse."""
    asyncio.run(lz._log_narasi_usage("t", "u", "m", _resp(), charge=True))
    assert billing["charges"] == [{"cr": 7}]
    assert len(billing["usage_rows"]) == 1


# ---------------------------------------------------------------------------
# 3 — the telemetry payload: observed, never assumed
# ---------------------------------------------------------------------------
def test_usage_out_is_filled_from_the_response_that_actually_succeeded(billing):
    out: dict = {}
    asyncio.run(lz._log_narasi_usage(
        "t", "u", "claude-haiku-4-5-20251001",
        _resp(tok_in=1200, tok_out=310, served_by="atlascloud",
              model="claude-haiku-4-5-20251001"),
        credit_row=False, usage_row=False, usage_out=out))
    assert out["served_provider"] == "atlascloud"
    assert out["served_model"] == "claude-haiku-4-5-20251001"
    assert out["model_requested"] == "claude-haiku-4-5-20251001"
    assert (out["tokens_in"], out["tokens_out"]) == (1200, 310)
    assert out["cost_usd"] == "0.0025"
    assert out["credits_would_be"] == 7
    assert out["cost_basis"] == "credit_catalog.credit_cost + _calc_cost"
    # neither catalog carries a version marker today; a fabricated one would let a later audit
    # believe it could pin this estimate to a priced snapshot
    assert out["pricing_catalog_version"] is None


def test_usage_out_never_claims_the_configured_provider_served_the_call(billing):
    """🔴 THE WHOLE POINT OF `served_provider`. The row's own `provider` column still falls back
    to the model-prefix guess — that is what it has always done — but the telemetry must say
    "unknown" rather than name a provider nobody observed."""
    out: dict = {}
    asyncio.run(lz._log_narasi_usage("t", "u", "gemini-2.5-flash-lite", _resp(),
                                     usage_out=out))
    assert out["served_provider"] is None, "a model-prefix guess was published as observed"
    assert out["served_model"] is None
    assert out["model_requested"] == "gemini-2.5-flash-lite"
    # the usage ROW keeps its historical behaviour — the guess is fine there
    assert billing["usage_rows"][0]["provider"] == "gemini"


def test_usage_out_is_emptied_when_the_call_did_not_complete(billing, monkeypatch):
    """A call that raised produced no observation. An empty dict is honest; a half-filled one
    would be adjudicated as if it described a call that worked."""
    async def _boom(*_a, **_k):
        raise RuntimeError("db down")
    monkeypatch.setattr(lz, "db", types.SimpleNamespace(log_usage=_boom))
    out: dict = {}
    cr = asyncio.run(lz._log_narasi_usage("t", "u", "m", _resp(served_by="kie"), usage_out=out))
    assert cr == 0, "a failed logger must not report a cost"
    assert out == {}, "partial observation metadata survived a failed call"


def test_stale_metadata_from_a_previous_attempt_never_survives(billing):
    """🔴 RESIDUE IS INDISTINGUISHABLE FROM AN OBSERVATION. A retry reusing the same dict must
    not let the first attempt's provider/model/tokens be read as the second's."""
    out = {"served_provider": "atlascloud", "served_model": "old-model",
           "tokens_in": 999, "tokens_out": 888, "leftover": True}
    asyncio.run(lz._log_narasi_usage("t", "u", "gemini-2.5-flash-lite", _resp(tok_in=10, tok_out=5),
                                     usage_out=out))
    assert "leftover" not in out, "the dict was not cleared on entry"
    assert out["served_provider"] is None, "a previous attempt's provider survived"
    assert out["served_model"] is None
    assert (out["tokens_in"], out["tokens_out"]) == (10, 5)


# ---------------------------------------------------------------------------
# 3b — the one refused combination
# ---------------------------------------------------------------------------
def test_charging_without_an_audit_row_is_refused_before_any_debit(billing):
    """🔴 DEBIT WITHOUT AN AUDIT ROW IS THE ONE THING THE OBSERVE SWITCH MUST NOT MAKE
    POSSIBLE. Refused up front, so the refusal cannot be swallowed by the best-effort handler,
    and refused BEFORE `credits_lib.charge` can run."""
    with pytest.raises(ValueError, match="audit row"):
        asyncio.run(lz._log_narasi_usage("t", "u", "m", _resp(),
                                         charge=True, usage_row=False))
    assert billing["charges"] == [], "a debit happened despite the refusal"
    assert billing["usage_rows"] == []


# ---------------------------------------------------------------------------
# 4 — the census helper threads both through
# ---------------------------------------------------------------------------
def test_the_census_helper_forwards_usage_row_and_usage_out(monkeypatch):
    seen = {}

    async def _log(tenant_id, user_id, model, resp, **kw):
        seen.update(kw)
        if isinstance(kw.get("usage_out"), dict):
            kw["usage_out"]["served_provider"] = "kie"
        return 5

    class Completions:
        def create(self, **kwargs):
            return _resp(content='{"score": 8}', served_by="kie")

    monkeypatch.setattr(lz, "make_narasi_client",
                        lambda *a, **k: types.SimpleNamespace(
                            chat=types.SimpleNamespace(completions=Completions())))
    monkeypatch.setattr(lz, "_log_narasi_usage", _log)

    out: dict = {}
    asyncio.run(lz._narasi_consistency_critique(
        "## Bab 1: Judul\n\nsatu dua tiga\n", "storytelling", "id", model="m",
        tenant_id="t", user_id="u", job_uuid=None, credit_row=False,
        usage_row=False, usage_out=out))

    assert seen.get("usage_row") is False, "the census helper did not forward usage_row"
    assert seen.get("credit_row") is False
    assert out.get("served_provider") == "kie", "usage_out did not reach the logger"


def test_the_census_helper_defaults_leave_every_existing_caller_alone(monkeypatch):
    seen = {}

    async def _log(tenant_id, user_id, model, resp, **kw):
        seen.update(kw)
        return 5

    class Completions:
        def create(self, **kwargs):
            return _resp(content='{"score": 8}')

    monkeypatch.setattr(lz, "make_narasi_client",
                        lambda *a, **k: types.SimpleNamespace(
                            chat=types.SimpleNamespace(completions=Completions())))
    monkeypatch.setattr(lz, "_log_narasi_usage", _log)

    asyncio.run(lz._narasi_consistency_critique(
        "## Bab 1: Judul\n\nsatu dua tiga\n", "storytelling", "id", model="m",
        tenant_id="t", user_id="u", job_uuid=None))

    assert seen.get("usage_row") is True, "the default must keep writing the row"
    assert seen.get("usage_out") is None
