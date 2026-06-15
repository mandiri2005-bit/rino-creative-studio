# -*- coding: utf-8 -*-
"""
metering.py — the gate that wraps every paid upstream call.

    charge = await begin_charge(... estimate_units ...)   # HOLD (or HTTP 402)
    try:
        ...run the upstream operation...
        await charge.settle(actual_units, tok_in=, tok_out=)  # COMMIT actual + log
    except Exception:
        await charge.refund()                                  # give the hold back
        raise

The HOLD reserves a conservative estimate before the call; SETTLE finalises the
ACTUAL cost (refunding any unused portion of the hold) and writes the usage_logs
row with its credit charge. A cancelled stream settles its PARTIAL actual; a
failed / zero-output call refunds the whole hold. Insufficient balance raises
HTTP 402 with {error, needed, balance, topup_url} so the frontend can prompt a
top-up. BYOK ops cost 0 credits (the user pays the upstream) and skip the hold.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Optional, Union

from fastapi import HTTPException

import credit_catalog as _cat
import credits as _credits
import database as _db

log = logging.getLogger("metering")

TOPUP_URL = os.getenv("BILLING_TOPUP_URL", "/billing")
# Kill-switch: set METERING_ENABLED=false to disable the credit gate entirely
# (no holds, no 402, no charges) — usage is still logged. Useful in dev.
METERING_ENABLED = os.getenv("METERING_ENABLED", "true").lower() not in ("false", "0", "no")

# operation → usage_logs.endpoint (CHECK: chat|image|tts|video|embedding|batch|narasi|other)
_OP_ENDPOINT = {
    "chat": "chat", "narasi": "narasi", "image": "image",
    "video": "video", "tts": "tts", "embedding": "embedding",
}


def _provider_for(model: str) -> str:
    m = (model or "").lower()
    if m.startswith(("gemini", "imagen")):  return "gemini"
    if m.startswith("deepseek"):            return "deepseek"
    if m.startswith(("gpt", "o3", "o1")):   return "openai"
    return "laozhang"


def insufficient_credits(needed: int, balance: int) -> HTTPException:
    """Build the 402 the frontend listens for to show the top-up prompt."""
    return HTTPException(status_code=402, detail={
        "error": "insufficient_credits",
        "needed": int(needed),
        "balance": int(balance),
        "topup_url": TOPUP_URL,
    })


class Charge:
    """A reserved credit hold for one operation. Settle once (commit actual) OR
    refund once (release). Idempotent: a second settle/refund is a no-op."""

    def __init__(self, *, tenant_id: str, user_id: Optional[str], op_id: str,
                 operation: str, model: str, held: int, byok: bool = False):
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.op_id = op_id
        self.operation = operation
        self.model = model
        self.held = int(held)
        self.byok = byok
        self._done = False

    @property
    def endpoint(self) -> str:
        return _OP_ENDPOINT.get(self.operation, "other")

    async def settle(self, units: Union[int, float, dict], *,
                     session_id=None, job_id=None,
                     tok_in: int = 0, tok_out: int = 0,
                     provider: Optional[str] = None,
                     usd: Optional[float] = None) -> int:
        """Finalise at ACTUAL cost. Returns credits actually charged.

        `usd`: the REAL upstream cost the caller already accumulated. Pass it for
        multi-model runs (e.g. a narration where cheap workers + an expensive
        manager use different-priced models) — credits are then derived from the
        true blended cost via usd_to_credits, instead of mis-pricing the whole
        token total at this Charge's single `model`. When omitted, behaviour is
        unchanged: price `units` at `self.model`."""
        if self._done:
            return 0
        self._done = True
        actual = 0
        # Effective USD: caller-supplied real cost when given, else the
        # single-model estimate from the token total.
        eff_usd = float(usd) if usd is not None else _cat.operation_usd(self.operation, self.model, units)
        if not self.byok:
            actual = (_cat.usd_to_credits(eff_usd) if usd is not None
                      else _cat.credit_cost(self.operation, self.model, units))
            try:
                await _credits.commit(self.tenant_id, self.op_id, actual,
                                      user_id=self.user_id,
                                      metadata={"op": self.operation, "model": self.model})
            except Exception as e:
                log.warning("settle commit(%s) failed: %s", self.op_id, e)
        # Always record the usage row (credits=0 for BYOK) so nothing is invisible.
        try:
            await _db.log_usage(self.tenant_id, self.user_id, self.model,
                                self.endpoint, int(tok_in), int(tok_out), eff_usd,
                                session_id=session_id, job_id=job_id,
                                provider=provider or _provider_for(self.model),
                                credits=actual)
        except Exception as e:
            log.warning("settle log_usage(%s) failed: %s", self.op_id, e)
        return actual

    async def refund(self) -> None:
        """Release the whole hold (op failed / produced nothing)."""
        if self._done:
            return
        self._done = True
        if not self.byok and self.held > 0:
            try:
                await _credits.refund(self.tenant_id, self.op_id)
            except Exception as e:
                log.warning("refund(%s) failed: %s", self.op_id, e)


async def begin_charge(*, tenant_id: str, user_id: Optional[str], operation: str,
                       model: str, estimate_units: Union[int, float, dict],
                       op_id: Optional[str] = None, byok: bool = False) -> Charge:
    """HOLD the estimated credits for an operation. Raises HTTP 402 if the balance
    can't cover the estimate. BYOK / zero-cost ops hold nothing."""
    op_id = op_id or str(uuid.uuid4())
    if byok or not METERING_ENABLED:
        # BYOK pays upstream directly; disabled gate holds nothing. Either way the
        # op still runs and settle() logs a credits=0 usage row.
        return Charge(tenant_id=tenant_id, user_id=user_id, op_id=op_id,
                      operation=operation, model=model, held=0, byok=True)
    est = _cat.credit_cost(operation, model, estimate_units)
    if est <= 0:
        return Charge(tenant_id=tenant_id, user_id=user_id, op_id=op_id,
                      operation=operation, model=model, held=0)
    try:
        await _credits.hold(tenant_id, est, op_id)
    except _credits.InsufficientCredits as e:
        raise insufficient_credits(e.needed, e.balance)
    return Charge(tenant_id=tenant_id, user_id=user_id, op_id=op_id,
                  operation=operation, model=model, held=est)


def quote(operation: str, model: str, units: Union[int, float, dict]) -> int:
    """Credits an operation WOULD cost — for the pre-confirm cost display."""
    return _cat.credit_cost(operation, model, units)


# ── Lightweight gate+debit for sync ops (pre-check 402, charge on success) ─────
# Simpler than a hold for one-shot ops: block up front if the tenant can't afford
# the estimate, run the op, then debit the actual on success. A failed op debits
# nothing (we only charge after success), so no refund path is needed.
async def gate(tenant_id: str, operation: str, model: str,
               units: Union[int, float, dict], *, byok: bool = False) -> int:
    """Raise HTTP 402 if the tenant can't cover the estimated cost. Returns the
    estimate. No-op for BYOK / disabled metering / unauthenticated (no tenant)."""
    if byok or not METERING_ENABLED or not tenant_id:
        return 0
    est = _cat.credit_cost(operation, model, units)
    if est <= 0:
        return 0
    bal = await _credits.get_balance(tenant_id)
    if bal < est:
        raise insufficient_credits(est, bal)
    return est


async def debit(tenant_id: str, user_id: Optional[str], operation: str, model: str,
                units: Union[int, float, dict], *, byok: bool = False, log: bool = True,
                session_id=None, job_id=None, video_job=None, tok_in: int = 0, tok_out: int = 0,
                provider: Optional[str] = None) -> int:
    """Post-hoc charge for a completed op. With log=True also writes a usage_logs
    row carrying the credits; set log=False when the caller already logs the usage
    row itself (e.g. image flows via _capture_image_flow) to avoid a duplicate.
    Returns credits charged (0 for BYOK / unauthenticated / disabled)."""
    if not tenant_id or not METERING_ENABLED:
        return 0
    credits = 0 if byok else _cat.credit_cost(operation, model, units)
    if credits:
        try:
            # Tag the ledger row with the video-assembly job id (when present) so a
            # failed assembly can refund exactly what its scenes consumed (see
            # /video/credits/refund). Distinct from job_id, which is the media-task id.
            md = {"op": operation, "model": model}
            if video_job:
                md["video_job"] = str(video_job)
            await _credits.charge(tenant_id, credits, op_id=str(uuid.uuid4()),
                                  user_id=user_id, metadata=md)
        except Exception as e:
            log.warning("debit(%s) failed: %s", operation, e)
    if log:
        try:
            usd = _cat.operation_usd(operation, model, units)
            await _db.log_usage(tenant_id, user_id, model, _OP_ENDPOINT.get(operation, "other"),
                                int(tok_in), int(tok_out), usd, session_id=session_id, job_id=job_id,
                                provider=provider or _provider_for(model), credits=credits)
        except Exception as e:
            log.warning("debit(%s) log failed: %s", operation, e)
    return credits
