// l2c_realdb_orphan.test.mjs — REAL-DATABASE orphan-before-anchor proof.
//
// NO MOCKS. Real backend/db.js and real backend/payments_core.mjs against a
// disposable PostgreSQL cluster with the repo's own 67 migrations applied.
//
// Models the genuine out-of-order sequence: credits are granted by the real
// topup_grant primitive, a refund arrives while NO payment_events anchor exists,
// and the anchor lands later. Proves a real reversal on the first consumption,
// an injected applied_at failure that rejects, and a retry that converges with
// EXACTLY ZERO additional financial mutation.
//
// Fault injection is a temporary trigger created inside the disposable database
// only, installed and removed in try/finally so a failed assertion cannot leak it.
// There is no production fault hook.
//
// KNOWN BEHAVIOUR (discovered here, not under test): reversal is PROPORTIONAL —
// refund_amount is reversed against the anchor's gross `amount`, so a FULL credit
// reversal requires refund_amount === amount. Partial/cumulative-refund cap tests
// are out of scope for this step.
//
// Run from backend/ with any database on the disposable cluster:
//   NODE_ENV=development PGSSLMODE=disable \
//   REALDB_ADMIN_URL=postgres://postgres@127.0.0.1:55432/postgres \
//   node --test ../tests/node/l2c_realdb_orphan.test.mjs
//
// This file provisions and migrates its OWN database (see _realdb.mjs). It is
// never the database the DSN names, so these tests cannot collide with another
// realdb file's fault-injection triggers when node:test runs files in parallel.
import { test, after } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { useOwnDatabase } from "./_realdb.mjs";

// Never inherit an ambient REDIS_URL: a developer shell may point at production.
// A non-local test Redis must be opted into through the test-specific variable.
process.env.REDIS_URL = process.env.REALDB_TEST_REDIS_URL || "redis://127.0.0.1:6379";
const DSN = await useOwnDatabase("orphan");

const { pool } = await import("../../backend/db.js");
const { recordCreditedPaymentEvent, topup_grant } = await import("../../backend/payments_core.mjs");
const { redis } = await import("../../backend/redis.js");
after(async () => { try { redis.disconnect(); } catch {} try { await pool.end(); } catch {} });

const q = (s, p = []) => pool.query(s, p);
const uniq = crypto.randomBytes(5).toString("hex");
const TENANT = crypto.randomUUID();
const PAY = `pay_real_${uniq}`;
const REFUND_OP = `refund:dodo:rf_${uniq}`;
const CREDITS = 500;
const AMOUNT = 1000;             // gross; refund_amount must equal it for a FULL reversal

// ── deterministic readers ───────────────────────────────────────────────────
const balances = async () => {
  const r = (await q(
    "SELECT COALESCE(balance,0) b, COALESCE(topup_balance,0) t FROM credit_balances WHERE tenant_id=$1", [TENANT],
  )).rows[0];
  return { balance: Number(r?.b ?? 0), topup: Number(r?.t ?? 0) };
};
const anchors = async () =>
  (await q(
    `SELECT id, idempotency_key, status, COALESCE(reversed_credits,0) reversed_credits, credited
       FROM payment_events WHERE provider='dodo' AND provider_payment_id=$1 ORDER BY id`, [PAY],
  )).rows;
const orphanApplied = async () =>
  (await q("SELECT applied_at FROM orphan_reversals WHERE refund_op_id=$1", [REFUND_OP])).rows[0]?.applied_at ?? null;
const ledgerAll = async () =>
  Number((await q("SELECT count(*) c FROM credit_ledger WHERE tenant_id=$1", [TENANT])).rows[0].c);
const ledgerReversal = async () =>
  Number((await q("SELECT count(*) c FROM credit_ledger WHERE tenant_id=$1 AND op_id LIKE $2",
    [TENANT, "%" + REFUND_OP + "%"])).rows[0].c);

const call = () => recordCreditedPaymentEvent({
  tenantId: TENANT, userId: null, provider: "dodo",
  idempotencyKey: `topup:${PAY}`, providerPaymentId: PAY, planKey: "boost_10",
  amount: AMOUNT, currency: "USD", creditsGranted: CREDITS, bucket: "topup", rawEvent: {},
});

test("REAL-DB orphan-before-anchor: one reversal, applied_at failure rejects, retry converges with zero extra mutation",
  async () => {
    assert.ok(DSN, "DATABASE_POOL_URL_DEV must point at the disposable cluster");

    // ── seed: tenant only. NO payment_events anchor is created here. ────────
    const cols = (await q(
      "SELECT column_name, is_nullable, column_default FROM information_schema.columns WHERE table_name='tenants'",
    )).rows.filter((c) => c.is_nullable === "NO" && !c.column_default && c.column_name !== "id");
    const names = cols.map((c) => c.column_name);
    await q(
      `INSERT INTO tenants (id${names.length ? "," + names.join(",") : ""})
       VALUES ($1${names.map((_, i) => `,$${i + 2}`).join("")})`,
      [TENANT, ...names.map((n) => (/email/.test(n) ? `t_${uniq}@example.test` : `t_${uniq}`))],
    );

    // ── real credit movement through the actual primitive ──────────────────
    const before = await balances();
    await topup_grant({
      userId: null, tenantId: TENANT, amount: CREDITS,
      opId: `dodo_topup:${PAY}`,
      expiresAt: new Date(Date.now() + 30 * 864e5).toISOString(),
      meta: { provider: "dodo", pack_key: "boost_10", payment_id: PAY },
    });
    const granted = await balances();
    assert.equal(granted.balance, before.balance + CREDITS, "topup_grant raised balance");
    assert.equal(granted.topup, before.topup + CREDITS, "topup_grant raised topup_balance");
    assert.equal((await anchors()).length, 0, "precondition: NO anchor exists yet");

    // ── refund arrives BEFORE any anchor → genuine orphan ──────────────────
    await q(
      `INSERT INTO orphan_reversals (provider, provider_payment_id, refund_op_id, kind, refund_amount, raw_event)
       VALUES ('dodo',$1,$2,'refund',$3,'{}'::jsonb)`,
      [PAY, REFUND_OP, AMOUNT],
    );
    assert.equal(await orphanApplied(), null, "orphan starts pending");

    try {
      // ── attempt 1: anchor lands, reversal runs, applied_at is blocked ────
      await q(`CREATE OR REPLACE FUNCTION _t_block_applied() RETURNS trigger LANGUAGE plpgsql AS
               $$ BEGIN RAISE EXCEPTION 'injected_applied_at_failure'; END $$`);
      await q(`CREATE TRIGGER _t_block_applied BEFORE UPDATE ON orphan_reversals
               FOR EACH ROW EXECUTE FUNCTION _t_block_applied()`);

      await assert.rejects(call, /injected_applied_at_failure/,
        "the applied_at failure must propagate, not be swallowed");

      const a1 = await anchors();
      assert.equal(a1.length, 1, "exactly ONE payment_events row for (provider, provider_payment_id)");
      assert.equal(a1[0].idempotency_key, `topup:${PAY}`, "anchor keyed on the payment id, not a webhook id");
      assert.equal(Number(a1[0].reversed_credits), CREDITS, "anchor records the full reversal");
      assert.equal(a1[0].status, "refunded", "anchor reached its terminal status");

      const afterRev = await balances();
      assert.equal(afterRev.balance, before.balance, "balance returned to pre-grant value");
      assert.equal(afterRev.topup, before.topup, "topup_balance returned to pre-grant value");
      assert.equal(await ledgerReversal(), 1, "the financial reversal COMMITTED exactly once");
      assert.equal(await orphanApplied(), null, "orphan remains pending");

      // ── attempt 2: unblock, retry, expect pure convergence ──────────────
      const balPrev = afterRev;
      const ledgerPrev = await ledgerAll();
      const reversedPrev = Number(a1[0].reversed_credits);

      await q("DROP TRIGGER IF EXISTS _t_block_applied ON orphan_reversals");
      await call();

      const a2 = await anchors();
      assert.equal(a2.length, 1, "still exactly ONE anchor row");
      assert.equal(Number(a2[0].reversed_credits), reversedPrev, "reversed_credits unchanged");
      assert.deepEqual(await balances(), balPrev, "zero additional balance/topup_balance mutation");
      assert.equal(await ledgerAll(), ledgerPrev, "zero additional ledger rows");
      assert.equal(await ledgerReversal(), 1, "still exactly one reversal across both attempts");
      assert.notEqual(await orphanApplied(), null, "orphan is now marked applied");
    } finally {
      await q("DROP TRIGGER IF EXISTS _t_block_applied ON orphan_reversals").catch(() => {});
      await q("DROP FUNCTION IF EXISTS _t_block_applied()").catch(() => {});
    }
  });
