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


_TIER_LABEL = {"free": "Free", "starter": "Starter", "pro": "Pro", "enterprise": "Studio"}

def tier_locked(model: str, need_tier: str, have_tier: str) -> HTTPException:
    """Build the 403 the frontend listens for to show the upgrade prompt (model-lock)."""
    return HTTPException(status_code=403, detail={
        "error": "tier_locked",
        "model": model,
        "required_plan": need_tier,
        "current_plan": have_tier,
        "upgrade_url": TOPUP_URL,
        "message": f"Model ini cuma buat paket {_TIER_LABEL.get(need_tier, need_tier)} ke atas.",
    })

def ensure_tier(user, min_tier: str, model: str) -> None:
    """Model-lock gate: raise 403 when the user's plan ranks below `min_tier`. Call it
    BEFORE gate() so a locked model 403s before any hold/charge. No-op for:
      • unauthenticated calls (user=None), and
      • trusted internal-service / video-worker calls (user.is_internal) — these run
        server-side (Flow/Batch scene gen reuse /generate-image,/veo/submit) and must
        NOT be tier-gated, else a Free user's Flow render 403s mid-way.
    Tier locks ALSO apply to BYOK (product boundary, not billing) — do NOT early-return
    on byok the way gate() does."""
    if user is None or getattr(user, "is_internal", False):
        return
    have = getattr(user, "tier", "free")
    if not _cat.tier_at_least(have, min_tier):
        raise tier_locked(model, min_tier, have)


def free_pool_exhausted() -> HTTPException:
    """429 for the global free-tier kill-switch (community daily budget spent)."""
    return HTTPException(status_code=429, detail={
        "error": "free_pool_exhausted",
        "upgrade_url": TOPUP_URL,
        "message": "Kuota gratis komunitas hari ini sudah penuh 🙏 Coba lagi besok, "
                   "atau upgrade ke Starter buat generate tanpa nunggu.",
    })

async def _free_prep(tenant_id: str) -> None:
    """Free-tenant pre-flight, run BEFORE the balance check: (1) top up today's
    leaky-bucket claim so the gate sees today's credits, (2) enforce the global
    anti-farming kill-switch (429). No-op for paid tenants — one plan lookup, then
    Redis-cheap. Never blocks paid spend."""
    if not tenant_id:
        return
    if (await _credits.tier_of(tenant_id)) != "free":
        return
    await _credits.ensure_daily(tenant_id)
    if await _credits.free_global_blocked():
        raise free_pool_exhausted()


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
    await _free_prep(tenant_id)          # free: top up daily claim + global kill-switch
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
    await _free_prep(tenant_id)          # free: top up daily claim + global kill-switch
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
                provider: Optional[str] = None, op_id: Optional[str] = None) -> int:
    """Post-hoc charge for a completed op. With log=True also writes a usage_logs
    row carrying the credits; set log=False when the caller already logs the usage
    row itself (e.g. image flows via _capture_image_flow) to avoid a duplicate.
    Pass a STABLE op_id (e.g. "video-renderfee:<jobId>") for a charge that may be
    retried/re-run — charge() is idempotent on op_id, so a stitch re-run (BullMQ
    retry or recovery re-stitch) never double-charges. Default = fresh uuid (each
    call a distinct charge, the right behaviour for per-scene asset meters).
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
            await _credits.charge(tenant_id, credits, op_id=op_id or str(uuid.uuid4()),
                                  user_id=user_id, metadata=md)
        except Exception as e:
            log.warning("debit(%s) failed: %s", operation, e)
    if log:
        try:
            usd = _cat.operation_usd(operation, model, units)
            _ep = _OP_ENDPOINT.get(operation, "other")
            if video_job:           # tag video-pipeline (Video Instant) usage: image-VI / video-VI / tts-VI / chat-VI
                _ep = f"{_ep}-VI"
            await _db.log_usage(tenant_id, user_id, model, _ep,
                                int(tok_in), int(tok_out), usd, session_id=session_id, job_id=job_id,
                                provider=provider or _provider_for(model), credits=credits)
        except Exception as e:
            log.warning("debit(%s) log failed: %s", operation, e)
    return credits
