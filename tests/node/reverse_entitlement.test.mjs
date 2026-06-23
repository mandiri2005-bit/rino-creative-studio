// reverse_entitlement.test.mjs — refund/chargeback reversal (audit finding #1).
// Self-contained: creates a credited test payment, reverses it, asserts the
// round-trip + idempotency, then cleans up. Integration — needs an OWNER DB URL
// (bypasses RLS for setup/teardown). Skipped without TEST_DATABASE_URL.
//   TEST_DATABASE_URL="postgres://owner…" node --test ../tests/node/reverse_entitlement.test.mjs
import { test, after } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";

const URL = process.env.TEST_DATABASE_URL;
process.env.DATABASE_POOL_URL_DEV ??= URL || "postgres://u:p@localhost:5432/db";
process.env.REDIS_URL ??= "redis://localhost:6379";

const { reverse_entitlement } = await import("../../backend/payments_core.mjs");
const { pool } = await import("../../backend/db.js");
const { redis } = await import("../../backend/redis.js");
after(() => { try { redis.disconnect(); } catch {} try { pool.end(); } catch {} });

test("reverse_entitlement reverses the grant, flips status, and is idempotent",
  { skip: URL ? false : "set TEST_DATABASE_URL (owner) to run this integration test" },
  async () => {
    const q = (s, p = []) => pool.query(s, p);
    const tid = (await q("SELECT id FROM tenants LIMIT 1")).rows[0]?.id;
    assert.ok(tid, "need at least one tenant");
    const key = "revtest-" + crypto.randomBytes(5).toString("hex");
    const refundOp = "refund:test:" + key;
    const bal0 = Number((await q("SELECT COALESCE(balance,0) b FROM credit_balances WHERE tenant_id=$1", [tid])).rows[0]?.b || 0);
    await q("INSERT INTO payment_events (tenant_id,provider,idempotency_key,plan_key,amount,currency,status,credited,credits_granted) VALUES ($1,'midtrans',$2,'pro',500,'IDR','succeeded',true,500)", [tid, key]);
    await q("SELECT applied,balance FROM credit_apply($1,NULL,500,'topup',$2,'{}'::jsonb)", [tid, "test:setup:" + key]);
    try {
      const r = await reverse_entitlement({ provider: "midtrans", idempotencyKey: key, refundOpId: refundOp, kind: "refund", rawEvent: { refund_id: "rf_test", settlement_amount: 9900 } });
      assert.equal(r.reversed, true);
      assert.equal(r.applied, true);
      assert.equal(r.credits, 500);
      const rev = (await q("SELECT reversal_events FROM payment_events WHERE idempotency_key=$1", [key])).rows[0].reversal_events;
      assert.equal(rev.length, 1, "refund event payload captured");
      assert.equal(rev[0].refund_id, "rf_test");
      assert.equal(Number((await q("SELECT balance b FROM credit_balances WHERE tenant_id=$1", [tid])).rows[0].b), bal0, "balance returns to start");
      const pe = (await q("SELECT status, reversed_at IS NOT NULL rv FROM payment_events WHERE idempotency_key=$1", [key])).rows[0];
      assert.equal(pe.status, "refunded");
      assert.equal(pe.rv, true);
      assert.equal(Number((await q("SELECT delta FROM credit_ledger WHERE op_id=$1", [refundOp])).rows[0].delta), -500);
      // idempotent: a re-delivered refund must NOT double-decrement
      const r2 = await reverse_entitlement({ provider: "midtrans", idempotencyKey: key, refundOpId: refundOp, kind: "refund", rawEvent: { refund_id: "rf_test_dup" } });
      assert.equal(r2.applied, false);
      assert.equal(Number((await q("SELECT balance b FROM credit_balances WHERE tenant_id=$1", [tid])).rows[0].b), bal0, "no double-reverse");
      const rev2 = (await q("SELECT reversal_events FROM payment_events WHERE idempotency_key=$1", [key])).rows[0].reversal_events;
      assert.equal(rev2.length, 1, "idempotent reverse does not re-append");
    } finally {
      await q("DELETE FROM credit_ledger WHERE op_id IN ($1,$2)", ["test:setup:" + key, refundOp]);
      await q("DELETE FROM payment_events WHERE idempotency_key=$1", [key]);
    }
  });
