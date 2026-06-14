"""
Step 4 (metering + billing) — the revenue-path guarantees.

Runs against the real Redis + Postgres (DEV branch) when they're reachable;
skips cleanly otherwise (so the suite stays green without infra). Covers the
money path end-to-end:

  1. zero balance        → operation returns HTTP 402
  2. sufficient balance  → operation runs, usage_logs gets a credits row, balance drops
  3. failed / zero output→ the hold is refunded
  4. cancel              → only the partial actual is charged (unused refunded)
  5. double credit (op_id)→ idempotent, never double-credits (webhook safety)

Async bodies are driven through one module-scoped event loop so the asyncpg pool
stays bound to a single loop.
"""
import asyncio
import os
import uuid

import pytest
from fastapi import HTTPException

import database as db          # importable via conftest sys.path insert of ../../python
import redis_client as rc
import credits
import metering
import credit_catalog as catalog

TENANT = os.getenv("TEST_TENANT_ID", "10000000-0000-4000-a000-000000000001")


@pytest.fixture(scope="module")
def loop():
    lp = asyncio.new_event_loop()
    try:
        lp.run_until_complete(db.init_db())
        lp.run_until_complete(rc.init_redis())
        if rc.client() is None:
            raise RuntimeError("redis client not connected")
        # the seeded tenant must exist (FK target) + credit tables must be migrated
        row = lp.run_until_complete(db._q_fetchrow(
            "SELECT id FROM tenants WHERE id=$1", db._uid(TENANT), tenant=TENANT))
        if not row:
            raise RuntimeError(f"test tenant {TENANT} not present")
        lp.run_until_complete(db._q_fetchval(
            "SELECT 1 FROM credit_balances LIMIT 0", tenant=TENANT))
    except Exception as e:                       # no infra / not migrated → skip module
        pytest.skip(f"Step 4 infra unavailable: {e}")
    yield lp
    try:
        lp.run_until_complete(rc.close_redis())
        lp.run_until_complete(db.close_db())
    finally:
        lp.close()


async def _reset(balance=0):
    """Wipe the test tenant's credit state and seed `balance` credits."""
    cl = rc.client()
    async for k in cl.scan_iter(match=f"hold:{TENANT}:*"):
        await cl.delete(k)
    await cl.delete(f"bal:{TENANT}:credits")
    await db._q_exec("DELETE FROM credit_ledger   WHERE tenant_id=$1", db._uid(TENANT), tenant=TENANT)
    await db._q_exec("DELETE FROM credit_balances WHERE tenant_id=$1", db._uid(TENANT), tenant=TENANT)
    await credits._credit_apply(TENANT, 0, "admin_adjust", op_id=f"reset:{uuid.uuid4()}")
    if balance:
        await credits.grant(TENANT, balance, reason="topup", op_id=f"seed:{uuid.uuid4()}")


def _go(loop, coro):
    return loop.run_until_complete(coro)


def test_hold_commit_refunds_unused(loop):
    async def body():
        await _reset(500)
        await credits.hold(TENANT, 300, "op_a")            # live 200
        assert await credits.get_balance(TENANT) == 200
        await credits.commit(TENANT, "op_a", 120)          # refund 180 unused
        assert await credits.get_balance(TENANT) == 380
        assert await credits.durable_balance(TENANT) == 380   # only actual charged
    _go(loop, body())


def test_failed_op_refunds_full_hold(loop):
    async def body():
        await _reset(500)
        await credits.hold(TENANT, 300, "op_fail")
        assert await credits.get_balance(TENANT) == 200
        await credits.refund(TENANT, "op_fail")            # op produced nothing
        assert await credits.get_balance(TENANT) == 500
        assert await credits.durable_balance(TENANT) == 500   # never charged
    _go(loop, body())


def test_grant_idempotent_no_double_credit(loop):
    async def body():
        await _reset(0)
        b1 = await credits.grant(TENANT, 1000, reason="topup", op_id="evt_dup")
        b2 = await credits.grant(TENANT, 1000, reason="topup", op_id="evt_dup")  # webhook re-delivery
        assert b1 == 1000 and b2 == 1000
        assert await credits.durable_balance(TENANT) == 1000
    _go(loop, body())


def test_insufficient_raises(loop):
    async def body():
        await _reset(10)
        with pytest.raises(credits.InsufficientCredits):
            await credits.hold(TENANT, 999, "op_big")
        assert await credits.get_balance(TENANT) == 10     # unchanged
    _go(loop, body())


def test_reconcile_healthy(loop):
    async def body():
        await _reset(500)
        await credits.hold(TENANT, 120, "op_r")
        await credits.commit(TENANT, "op_r", 120)
        rep = await credits.reconcile(TENANT)
        assert rep["durable_matches_ledger"] and rep["redis_matches"], rep
    _go(loop, body())


def test_metering_402_on_zero_balance(loop):
    async def body():
        await _reset(0)
        with pytest.raises(HTTPException) as ei:
            await metering.begin_charge(
                tenant_id=TENANT, user_id=None, operation="image",
                model="imagen-4.0", estimate_units=10)        # 40 credits, balance 0
        assert ei.value.status_code == 402
        assert ei.value.detail["error"] == "insufficient_credits"
    _go(loop, body())


def test_metering_settle_records_usage_credits(loop):
    async def body():
        await _reset(100)
        ch = await metering.begin_charge(
            tenant_id=TENANT, user_id=None, operation="image",
            model="imagen-4.0", estimate_units=1)             # 4 credits
        assert ch.held == 4
        await ch.settle(1)
        assert await credits.get_balance(TENANT) == 96
        row = await db._q_fetchrow(
            "SELECT credits FROM usage_logs WHERE tenant_id=$1 AND endpoint='image' "
            "ORDER BY created_at DESC LIMIT 1", db._uid(TENANT), tenant=TENANT)
        assert row and int(row["credits"]) == 4
    _go(loop, body())


def test_byok_is_zero_credits(loop):
    async def body():
        await _reset(100)
        ch = await metering.begin_charge(
            tenant_id=TENANT, user_id=None, operation="image",
            model="imagen-4.0", estimate_units=10, byok=True)
        assert ch.held == 0
        await ch.settle(10)
        assert await credits.get_balance(TENANT) == 100      # BYOK never charges
    _go(loop, body())


def test_catalog_costs_are_sane():
    # pure (no infra): credit math follows CREDIT_USD_VALUE=0.01 break-even basis
    assert catalog.usd_to_credits(0.04) == 4          # one $0.04 image
    assert catalog.usd_to_credits(0.0) == 0           # free op
    assert catalog.credit_cost("tts", "tts-1", {"chars": 5000}) == 50
    assert catalog.credit_cost("video", "veo-3.1", {"seconds": 8}) == 400
