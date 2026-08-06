// l2c_realdb_tranche1.test.mjs — REAL-DATABASE proofs 1-3 for tranche 1.
//
// NO MOCKS of financial primitives. Real handleTopupPayment / topup_grant /
// recordCreditedPaymentEvent / handleSubscriptionEvent against a disposable
// PostgreSQL cluster carrying the repo's own 67 migrations.
//
//   1 concurrent same-payment delivery  -> exactly one grant, exactly one anchor
//   2 grant ok + anchor INSERT fails    -> retry repairs the anchor, no second grant
//   3 tenants_plan_check 23514          -> fail closed, zero entitlement mutation
//
// Every fault mechanism is a temporary trigger inside the disposable database,
// installed and removed in try/finally. No production fault hooks. No migration.
//
// Run from backend/:
//   NODE_ENV=development PGSSLMODE=disable \
//   DATABASE_POOL_URL_DEV=postgres://postgres@127.0.0.1:55432/l2ctest \
//   node --test ../tests/node/l2c_realdb_tranche1.test.mjs
import { test, after } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";

process.env.REDIS_URL ??= "redis://localhost:6379";
process.env.BILLING_MODE = "subscription";
process.env.DODO_PRODUCT_STARTER ??= "prod_starter";

const { pool } = await import("../../backend/db.js");
const sub = await import("../../backend/dodo_subscriptions.mjs");
const { redis } = await import("../../backend/redis.js");
after(async () => { try { redis.disconnect(); } catch {} try { await pool.end(); } catch {} });

const q = (s, p = []) => pool.query(s, p);
const PACK = "boost_10";
const PACK_CREDITS = sub.topupPackCredits(PACK);

async function newTenant(tag) {
  const id = crypto.randomUUID();
  const cols = (await q(
    "SELECT column_name,is_nullable,column_default FROM information_schema.columns WHERE table_name='tenants'",
  )).rows.filter((c) => c.is_nullable === "NO" && !c.column_default && c.column_name !== "id");
  const names = cols.map((c) => c.column_name);
  await q(
    `INSERT INTO tenants (id${names.length ? "," + names.join(",") : ""})
     VALUES ($1${names.map((_, i) => `,$${i + 2}`).join("")})`,
    [id, ...names.map((n) => (/email/.test(n) ? `${tag}@example.test` : tag))],
  );
  return id;
}
const bal = async (t) => {
  const r = (await q("SELECT COALESCE(balance,0) b, COALESCE(topup_balance,0) tp FROM credit_balances WHERE tenant_id=$1", [t])).rows[0];
  return { balance: Number(r?.b ?? 0), topup: Number(r?.tp ?? 0) };
};
const ledgerOp = async (t, op) =>
  Number((await q("SELECT count(*) c FROM credit_ledger WHERE tenant_id=$1 AND op_id=$2", [t, op])).rows[0].c);
const ledgerAll = async (t) =>
  Number((await q("SELECT count(*) c FROM credit_ledger WHERE tenant_id=$1", [t])).rows[0].c);
const anchorRows = async (pay) =>
  (await q("SELECT id, idempotency_key, credited FROM payment_events WHERE provider='dodo' AND provider_payment_id=$1 ORDER BY id", [pay])).rows;

const topupPayload = (tenantId, payId) => ({
  data: { payment_id: payId, total_amount: 1000, currency: "USD",
    metadata: { tenant_id: tenantId, kind: "topup", pack_key: PACK } },
});

// ══ 1. CONCURRENT SAME-PAYMENT DELIVERY ═════════════════════════════════════
test("REALDB-1 concurrent deliveries of one payment under different webhook ids -> one grant, one anchor", async () => {
  const tag = "c1_" + crypto.randomBytes(4).toString("hex");
  const T = await newTenant(tag);
  const PAY = `pay_${tag}`;
  const before = await bal(T);

  const results = await Promise.allSettled([
    sub.handleTopupPayment({ payload: topupPayload(T, PAY), webhookId: `wh_${tag}_A` }),
    sub.handleTopupPayment({ payload: topupPayload(T, PAY), webhookId: `wh_${tag}_B` }),
  ]);
  const ok = results.filter((r) => r.status === "fulfilled").map((r) => r.value);
  assert.equal(ok.length, 2, `both deliveries settled: ${JSON.stringify(results.map((r) => r.reason?.message ?? "ok"))}`);

  const after_ = await bal(T);
  assert.equal(after_.balance, before.balance + PACK_CREDITS, "exactly ONE balance increase");
  assert.equal(after_.topup, before.topup + PACK_CREDITS, "exactly ONE topup_balance increase");
  assert.equal(await ledgerOp(T, `dodo_topup:${PAY}`), 1, "exactly ONE grant ledger row");
  assert.equal(await ledgerAll(T), 1, "no other ledger rows for this tenant");

  const rows = await anchorRows(PAY);
  assert.equal(rows.length, 1, "exactly ONE (provider, provider_payment_id) anchor row");
  assert.equal(rows[0].idempotency_key, `topup:${PAY}`, "anchor keyed on payment_id");
  assert.ok(!rows[0].idempotency_key.includes("wh_"), "no webhook id in the anchor key");

  const applied = ok.filter((r) => r.applied === true).length;
  const noop = ok.filter((r) => r.applied === false).length;
  assert.equal(applied, 1, "exactly one applied grant");
  assert.equal(noop, 1, "exactly one idempotent no-op");
});

// ══ 2. GRANT OK, ANCHOR INSERT FAILS, RETRY REPAIRS ═════════════════════════
test("REALDB-2 anchor INSERT failure rejects; retry repairs the anchor without a second grant", async () => {
  const tag = "c2_" + crypto.randomBytes(4).toString("hex");
  const T = await newTenant(tag);
  const PAY = `pay_${tag}`;
  const before = await bal(T);
  try {
    await q(`CREATE OR REPLACE FUNCTION _t_block_anchor() RETURNS trigger LANGUAGE plpgsql AS
             $$ BEGIN RAISE EXCEPTION 'injected_anchor_insert_failure'; END $$`);
    await q(`CREATE TRIGGER _t_block_anchor BEFORE INSERT ON payment_events
             FOR EACH ROW EXECUTE FUNCTION _t_block_anchor()`);

    await assert.rejects(
      () => sub.handleTopupPayment({ payload: topupPayload(T, PAY), webhookId: `wh_${tag}_1` }),
      /injected_anchor_insert_failure/, "anchor failure must propagate");

    const mid = await bal(T);
    assert.equal(mid.balance, before.balance + PACK_CREDITS, "grant committed exactly once");
    assert.equal(mid.topup, before.topup + PACK_CREDITS, "topup_balance increased exactly once");
    assert.equal(await ledgerOp(T, `dodo_topup:${PAY}`), 1, "exactly ONE grant ledger operation");
    assert.equal((await anchorRows(PAY)).length, 0, "ZERO anchor rows after the failure");

    await q("DROP TRIGGER IF EXISTS _t_block_anchor ON payment_events");
    const ledgerBefore = await ledgerAll(T);

    const r = await sub.handleTopupPayment({ payload: topupPayload(T, PAY), webhookId: `wh_${tag}_2` });
    assert.equal(r.applied, false, "grant is an idempotent no-op on retry");
    assert.deepEqual(await bal(T), mid, "no additional balance/topup mutation");
    assert.equal(await ledgerAll(T), ledgerBefore, "no additional ledger rows");

    const rows = await anchorRows(PAY);
    assert.equal(rows.length, 1, "the missing anchor was created; exactly one exists");
    assert.equal(rows[0].idempotency_key, `topup:${PAY}`, "anchor identity derives from payment_id");
    assert.ok(!rows[0].idempotency_key.includes("wh_"), "never webhook_id");
  } finally {
    await q("DROP TRIGGER IF EXISTS _t_block_anchor ON payment_events").catch(() => {});
    await q("DROP FUNCTION IF EXISTS _t_block_anchor()").catch(() => {});
  }
});

// ══ 3. REAL 23514 FAIL-CLOSED ═══════════════════════════════════════════════
test("REALDB-3 tenants_plan_check 23514 fails closed with zero entitlement mutation", async () => {
  const tag = "c3_" + crypto.randomBytes(4).toString("hex");
  const T = await newTenant(tag);
  const SUBID = `sub_${tag}`;
  const before = await bal(T);
  const ledgerBefore = await ledgerAll(T);
  try {
    await q(`CREATE OR REPLACE FUNCTION _t_plan_check() RETURNS trigger LANGUAGE plpgsql AS
             $$ BEGIN RAISE EXCEPTION 'injected tenants_plan_check' USING ERRCODE = '23514'; END $$`);
    await q(`CREATE TRIGGER _t_plan_check BEFORE UPDATE ON tenants
             FOR EACH ROW EXECUTE FUNCTION _t_plan_check()`);

    await assert.rejects(
      () => sub.handleSubscriptionEvent({
        payload: { type: "subscription.active",
          data: { subscription_id: SUBID, product_id: "prod_starter",
            metadata: { tenant_id: T, plan_key: "starter" } } },
        webhookId: `wh_${tag}`,
      }),
      (e) => e.code === "23514",
      "handler must reject with 23514, not swallow it");

    assert.deepEqual(await bal(T), before, "balance and bucket balances unchanged");
    assert.equal(await ledgerAll(T), ledgerBefore, "zero entitlement/reset ledger operations");
    // NOTE: the dodo_subscriptions row may already have committed via _upsertSub.
    // That atomicity defect is TRANCHE 2 and is deliberately NOT asserted here.
  } finally {
    await q("DROP TRIGGER IF EXISTS _t_plan_check ON tenants").catch(() => {});
    await q("DROP FUNCTION IF EXISTS _t_plan_check()").catch(() => {});
  }
});
