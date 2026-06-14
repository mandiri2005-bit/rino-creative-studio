# -*- coding: utf-8 -*-
"""
credits.py — the pre-funded credit balance: check / hold / commit / refund.

TWO-TIER STORE
--------------
  Redis  bal:{tenant}:credits   live spendable balance (sub-ms checks + holds)
  PG     credit_balances        durable source of truth
  PG     credit_ledger          append-only audit (every +/-), idempotent on op_id

Redis is only a CACHE in front of credit_balances. The durable balance changes
ONLY on grant / topup / commit(charge) / admin_adjust. The live Redis balance
additionally moves on hold (−) and refund (+); a hold is a transient reservation
with a TTL that never touches Postgres.

LIFECYCLE OF ONE PAID OP
------------------------
  hold(t, H, op)            Redis −H, write hold:{t}:{op}=H        (durable: untouched)
  …call upstream…
  commit(t, op, actual=A)   Redis += (H−A), del hold; durable −A   (charge recorded)
  — or on failure / 0 output —
  refund(t, op)             Redis += H, del hold                   (durable: untouched)

So a cancelled stream just commits the PARTIAL actual (refunds the unused hold),
and a failed op refunds the whole hold. Nothing ever runs at a loss, and the
durable ledger only ever records credits actually consumed.

All Redis mutations are atomic Lua so two parallel ops can't race the balance.
Every function is best-effort safe: a Redis outage degrades to durable reads and
NEVER silently grants free spend (an uncached balance blocks the hold until
seeded from Postgres).
"""
from __future__ import annotations

import logging
from typing import Optional

import database as db
import redis_client as rc
from credit_catalog import TIER_MONTHLY_CREDITS

log = logging.getLogger("credits")


class InsufficientCredits(Exception):
    """Raised by hold() when the balance can't cover the requested amount."""
    def __init__(self, needed: int, balance: int):
        self.needed = needed
        self.balance = balance
        super().__init__(f"insufficient credits: need {needed}, have {balance}")


def _bal_key(tenant_id: str) -> str:
    return f"bal:{tenant_id}:credits"


def _hold_key(tenant_id: str, op_id: str) -> str:
    return f"hold:{tenant_id}:{op_id}"


_HOLD_TTL = 3600          # a reservation auto-releases after 1h if never settled

# ── Atomic Lua ────────────────────────────────────────────────────────────────
# Return codes use sentinels < 0 so any real balance (>= 0) is unambiguous.
_LUA_HOLD = """
local bal = redis.call('GET', KEYS[1])
if not bal then return -2 end                 -- not cached: caller must seed
bal = tonumber(bal)
local amt = tonumber(ARGV[1])
if bal < amt then return -1 end               -- insufficient
local newbal = redis.call('DECRBY', KEYS[1], amt)
redis.call('SET', KEYS[2], amt, 'EX', tonumber(ARGV[2]))
return newbal
"""

# Settle a hold to its actual cost: refund (held - actual) to the live balance.
# Idempotent — if the hold key is gone (already settled / expired) returns -1.
_LUA_COMMIT = """
local held = redis.call('GET', KEYS[2])
if not held then return -1 end
redis.call('DEL', KEYS[2])
local diff = tonumber(held) - tonumber(ARGV[1])   -- >0 refund unused, <0 extra
return redis.call('INCRBY', KEYS[1], diff)
"""

# Full refund of an outstanding hold. Idempotent (hold gone → -1, no double-credit).
_LUA_REFUND = """
local held = redis.call('GET', KEYS[2])
if not held then return -1 end
redis.call('DEL', KEYS[2])
return redis.call('INCRBY', KEYS[1], tonumber(held))
"""

_scripts: dict = {}


def _script(name: str, body: str):
    cl = rc.client()
    if cl is None:
        return None
    s = _scripts.get(name)
    if s is None:
        s = cl.register_script(body)
        _scripts[name] = s
    return s


# ── Durable (Postgres) helpers ────────────────────────────────────────────────
async def durable_balance(tenant_id: str) -> int:
    row = await db._q_fetchrow(
        "SELECT balance FROM credit_balances WHERE tenant_id=$1",
        db._uid(tenant_id), tenant=str(tenant_id))
    return int(row["balance"]) if row else 0


async def _credit_apply(tenant_id: str, delta: int, reason: str, *,
                        op_id: Optional[str] = None, user_id: Optional[str] = None,
                        metadata: Optional[dict] = None) -> tuple[bool, int]:
    """Atomic durable 'append ledger + move balance'. Returns (applied, balance).
    applied=False means op_id was already processed (idempotent no-op)."""
    import json as _json
    row = await db._q_fetchrow(
        "SELECT applied, balance FROM credit_apply($1,$2,$3,$4,$5,$6::jsonb)",
        db._uid(tenant_id), db._uid(user_id) if user_id else None,
        int(delta), reason, op_id, _json.dumps(metadata or {}),
        tenant=str(tenant_id))
    return (bool(row["applied"]), int(row["balance"])) if row else (False, 0)


# ── Redis cache seeding ───────────────────────────────────────────────────────
async def _tier_for(tenant_id: str) -> str:
    row = await db._q_fetchrow(
        "SELECT plan FROM tenants WHERE id=$1", db._uid(tenant_id), tenant=str(tenant_id))
    return (row["plan"] if row and row["plan"] else "free")


async def _ensure_cached(tenant_id: str) -> int:
    """Make sure Redis has the live balance. If credit_balances has no row yet,
    seed the tenant's signup grant (tier monthly allowance) ONCE (idempotent on
    op_id). Returns the cached balance. Uses SET NX so we never clobber a balance
    that already has outstanding holds."""
    cl = rc.client()
    # durable balance (seed the free/tier grant on first ever touch)
    dbal = await durable_balance(tenant_id)
    if dbal == 0:
        exists = await db._q_fetchval(
            "SELECT 1 FROM credit_balances WHERE tenant_id=$1",
            db._uid(tenant_id), tenant=str(tenant_id))
        if not exists:
            tier = await _tier_for(tenant_id)
            grant = TIER_MONTHLY_CREDITS.get(tier, TIER_MONTHLY_CREDITS["free"])
            _, dbal = await _credit_apply(tenant_id, grant, "signup_grant",
                                          op_id="signup_grant")
    if cl is None:
        return dbal
    try:
        # only seed if absent — preserves any in-flight holds already applied
        await cl.set(_bal_key(tenant_id), dbal, nx=True)
        cur = await cl.get(_bal_key(tenant_id))
        return int(cur) if cur is not None else dbal
    except Exception as e:
        log.warning("_ensure_cached(%s) redis: %s", tenant_id, e)
        return dbal


# ── Public API ────────────────────────────────────────────────────────────────
async def get_balance(tenant_id: str) -> int:
    """Live spendable balance (Redis cache, seeded from Postgres)."""
    cl = rc.client()
    if cl is not None:
        try:
            cur = await cl.get(_bal_key(tenant_id))
            if cur is not None:
                return int(cur)
        except Exception as e:
            log.warning("get_balance(%s) redis: %s", tenant_id, e)
    return await _ensure_cached(tenant_id)


async def hold(tenant_id: str, amount: int, op_id: str) -> int:
    """Reserve `amount` credits for op_id. Returns the new live balance.
    Raises InsufficientCredits if the balance can't cover it. amount<=0 is a
    no-op (free / BYOK op) and returns the current balance without a hold."""
    amount = int(amount)
    if amount <= 0:
        return await get_balance(tenant_id)
    await _ensure_cached(tenant_id)
    cl = rc.client()
    if cl is None:
        # No Redis: fall back to a durable check so we never grant free spend.
        bal = await durable_balance(tenant_id)
        if bal < amount:
            raise InsufficientCredits(amount, bal)
        return bal
    s = _script("hold", _LUA_HOLD)
    res = int(await s(keys=[_bal_key(tenant_id), _hold_key(tenant_id, op_id)],
                      args=[amount, _HOLD_TTL]))
    if res == -2:                       # cache evaporated between seed and call
        await _ensure_cached(tenant_id)
        res = int(await s(keys=[_bal_key(tenant_id), _hold_key(tenant_id, op_id)],
                          args=[amount, _HOLD_TTL]))
    if res == -1:
        raise InsufficientCredits(amount, await get_balance(tenant_id))
    return res


async def commit(tenant_id: str, op_id: str, actual: int, *,
                 user_id: Optional[str] = None, metadata: Optional[dict] = None) -> int:
    """Finalise a held op at its ACTUAL cost: refund the unused hold to the live
    balance and record the durable charge. Idempotent on op_id. Returns the new
    live balance. actual<=0 collapses to a full refund (nothing consumed)."""
    actual = max(0, int(actual))
    if actual == 0:
        return await refund(tenant_id, op_id)
    cl = rc.client()
    if cl is not None:
        s = _script("commit", _LUA_COMMIT)
        try:
            await s(keys=[_bal_key(tenant_id), _hold_key(tenant_id, op_id)], args=[actual])
        except Exception as e:
            log.warning("commit redis(%s): %s", op_id, e)
    # durable charge (idempotent via distinct op_id namespace)
    _, _bal = await _credit_apply(tenant_id, -actual, "charge",
                                  op_id=f"charge:{op_id}", user_id=user_id,
                                  metadata=metadata)
    return await get_balance(tenant_id)


async def refund(tenant_id: str, op_id: str) -> int:
    """Release an outstanding hold back to the live balance (op failed / cancelled
    with no output). Durable balance is untouched — it was never charged.
    Idempotent: a missing hold is a no-op."""
    cl = rc.client()
    if cl is None:
        return await durable_balance(tenant_id)
    s = _script("refund", _LUA_REFUND)
    try:
        await s(keys=[_bal_key(tenant_id), _hold_key(tenant_id, op_id)])
    except Exception as e:
        log.warning("refund redis(%s): %s", op_id, e)
    return await get_balance(tenant_id)


async def grant(tenant_id: str, amount: int, *, reason: str, op_id: Optional[str] = None,
                user_id: Optional[str] = None, metadata: Optional[dict] = None) -> int:
    """Add credits durably (signup / monthly / topup / admin), idempotent on op_id,
    and mirror into the live Redis balance only when newly applied. Returns the
    new live balance."""
    amount = int(amount)
    cl = rc.client()
    # Seed the live cache from the PRE-grant durable balance first, so the INCRBY
    # below isn't double-counted against a cache that already includes the grant.
    if cl is not None:
        try:
            await _ensure_cached(tenant_id)
        except Exception as e:
            log.warning("grant pre-seed redis(%s): %s", op_id, e)
    applied, dbal = await _credit_apply(tenant_id, amount, reason, op_id=op_id,
                                        user_id=user_id, metadata=metadata)
    if cl is not None and applied:
        try:
            await cl.incrby(_bal_key(tenant_id), amount)
        except Exception as e:
            log.warning("grant redis(%s): %s", op_id, e)
    return await get_balance(tenant_id)


async def topup(tenant_id: str, amount: int, op_id: str, *,
                user_id: Optional[str] = None, metadata: Optional[dict] = None) -> int:
    """Credit a one-off purchase. op_id MUST be stable per purchase (e.g. Stripe
    event id) so a double-delivered webhook never double-credits."""
    return await grant(tenant_id, amount, reason="topup", op_id=op_id,
                       user_id=user_id, metadata=metadata)


async def set_monthly_allowance(tenant_id: str, tier: str, op_id: str, *,
                                user_id: Optional[str] = None) -> int:
    """Grant a tier's monthly credit allowance, idempotent per billing period via
    op_id (e.g. 'monthly_grant:<sub>:<period_end>')."""
    amount = TIER_MONTHLY_CREDITS.get(tier, TIER_MONTHLY_CREDITS["free"])
    return await grant(tenant_id, amount, reason="monthly_grant", op_id=op_id,
                       user_id=user_id, metadata={"tier": tier})


async def reconcile(tenant_id: str) -> dict:
    """Compare the live Redis balance against the durable balance and the ledger
    sum. Returns a drift report; does NOT mutate. Healthy state:
        durable == ledger_sum   AND   redis == durable - outstanding_holds
    """
    durable = await durable_balance(tenant_id)
    ledger_sum = await db._q_fetchval(
        "SELECT COALESCE(SUM(delta),0) FROM credit_ledger WHERE tenant_id=$1",
        db._uid(tenant_id), tenant=str(tenant_id))
    ledger_sum = int(ledger_sum or 0)
    redis_bal = None
    outstanding = 0
    cl = rc.client()
    if cl is not None:
        try:
            cur = await cl.get(_bal_key(tenant_id))
            redis_bal = int(cur) if cur is not None else None
            keys = [k async for k in cl.scan_iter(match=_hold_key(tenant_id, "*"))]
            for k in keys:
                v = await cl.get(k)
                outstanding += int(v) if v is not None else 0
        except Exception as e:
            log.warning("reconcile(%s) redis: %s", tenant_id, e)
    return {
        "durable": durable,
        "ledger_sum": ledger_sum,
        "redis": redis_bal,
        "outstanding_holds": outstanding,
        "durable_matches_ledger": durable == ledger_sum,
        "redis_matches": (redis_bal is None) or (redis_bal == durable - outstanding),
    }


async def resync_from_durable(tenant_id: str) -> int:
    """Force the Redis cache to the durable balance. Use only when no holds are in
    flight (e.g. startup / admin), otherwise outstanding reservations are lost."""
    dbal = await durable_balance(tenant_id)
    cl = rc.client()
    if cl is not None:
        try:
            await cl.set(_bal_key(tenant_id), dbal)
        except Exception as e:
            log.warning("resync_from_durable(%s): %s", tenant_id, e)
    return dbal
