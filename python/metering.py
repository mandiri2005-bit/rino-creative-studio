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
                     provider: Optional[str] = None) -> int:
        """Finalise at ACTUAL cost. Returns credits actually charged."""
        if self._done:
            return 0
        self._done = True
        actual = 0
        if not self.byok:
            actual = _cat.credit_cost(self.operation, self.model, units)
            try:
                await _credits.commit(self.tenant_id, self.op_id, actual,
                                      user_id=self.user_id,
                                      metadata={"op": self.operation, "model": self.model})
            except Exception as e:
                log.warning("settle commit(%s) failed: %s", self.op_id, e)
        # Always record the usage row (credits=0 for BYOK) so nothing is invisible.
        try:
            usd = _cat.operation_usd(self.operation, self.model, units)
            await _db.log_usage(self.tenant_id, self.user_id, self.model,
                                self.endpoint, int(tok_in), int(tok_out), usd,
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
    if byok:
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
