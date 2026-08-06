// l2c_anchor_replay.test.mjs — L2C tranche-1 FOCUSED MOCKED TESTS (PARTIAL COVERAGE).
//
// This file is NOT a regression suite and does NOT cover all six candidate source
// fixes. It provides mocked control-flow and key-derivation coverage for defects
// 1, 2 and 5 only, plus one baseline-passing PINNING test (2) and one happy-path
// check (7). Defects 3 and 4 have NO tests in this file. Defect 6 is a comment
// correction and is not test-coverable. Every DB/Redis boundary is module-mocked,
// so this runs with no database. Run from backend/:
//   node --test --experimental-test-module-mocks ../tests/node/l2c_anchor_replay.test.mjs
//
// COVERAGE — READ BEFORE TRUSTING THIS FILE.
//
// Measured 2026-08-07: patched worktree 5 pass / 0 fail; pinned baseline
// d68e39c8 1 pass / 4 fail. FOUR of these five discriminate against the
// baseline. Test 2 ALSO passes on the baseline and is therefore a PINNING
// test, not a regression test.
//
// What is NOT proven here: every financial primitive (topup_grant,
// recordCreditedPaymentEvent, reset_entitlement) and every DB call is mocked.
// These tests prove CONTROL FLOW and KEY DERIVATION only. They do NOT prove a
// single real credit movement, real idempotency of the grant primitive, real
// balance/ledger effects, or any concurrency property. Real-DB and concurrency
// coverage is REQUIRED and still missing.
//
// Defects under test (all previously CONFIRMED against d68e39c8):
//   1 webhook_id used as a payment-anchor fallback            dodo_subscriptions.mjs:693  [covered]
//   2 anchor write skipped when topup_grant.applied === false dodo_subscriptions.mjs:701  [covered]
//   3 orphan-queue INSERT failure swallowed                   payments_core.mjs:277       [NOT COVERED]
//   4 orphan-consumption failure swallowed                    payments_core.mjs:242       [NOT COVERED]
//   5 tenants_plan_check 23514 swallowed into success         dodo_subscriptions.mjs:355  [covered]
//   6 stale retry-timestamp comment                           payments_core.mjs:269       [not test-covered]
import { test, mock, afterEach } from "node:test";
import assert from "node:assert/strict";

process.env.DATABASE_POOL_URL_DEV ??= "postgres://u:p@localhost:5432/db";
process.env.REDIS_URL ??= "redis://localhost:6379";
process.env.BILLING_MODE = "subscription";

// ── recording stubs ──────────────────────────────────────────────────────────
const calls = { topup_grant: [], record: [], reset: [], query: [] };
const behave = { topup_grant: null, record: null, query: null };
const reset = () => {
  for (const k of Object.keys(calls)) calls[k] = [];
  for (const k of Object.keys(behave)) behave[k] = null;
};
afterEach(reset);

mock.module("../../backend/db.js", {
  namedExports: {
    query: async (sql, params) => {
      calls.query.push({ sql: String(sql).replace(/\s+/g, " ").trim(), params });
      if (behave.query) return behave.query(sql, params);
      return { rows: [], rowCount: 1 };
    },
    pool: { connect: async () => { throw new Error("pool.connect must not be reached in unit tests"); }, end: async () => {} },
    withTenant: async (_t, fn) => fn(),
  },
});
mock.module("../../backend/redis.js", {
  namedExports: { redis: { disconnect() {}, del: async () => {}, eval: async () => {} } },
});
mock.module("../../backend/payments_core.mjs", {
  namedExports: {
    topup_grant: async (a) => {
      calls.topup_grant.push(a);
      if (behave.topup_grant) return behave.topup_grant(a);
      return { applied: true, balance: 100, delta: a.amount };
    },
    recordCreditedPaymentEvent: async (a) => {
      calls.record.push(a);
      if (behave.record) return behave.record(a);
      return undefined;
    },
    reset_entitlement: async (a) => { calls.reset.push(a); return { applied: true, balance: 0, delta: 0 }; },
    grant_entitlement: async () => ({ applied: true }),
    reverse_entitlement: async () => ({ reversed: true }),
    recordPaymentEvent: async () => undefined,
    mirrorCreditDelta: async () => undefined,
    creditsForPlan: () => 0,
  },
});

const sub = await import("../../backend/dodo_subscriptions.mjs");

const topupPayload = (payment_id, webhookExtra = {}) => ({
  data: {
    payment_id,
    total_amount: 1000,
    currency: "USD",
    metadata: { tenant_id: "11111111-1111-1111-1111-111111111111", kind: "topup", pack_key: "boost_10", ...webhookExtra },
  },
});

// ── 1. missing payment_id → fail closed, zero credit mutation ────────────────
test("1 missing top-up payment_id fails closed and mutates nothing", async () => {
  await assert.rejects(
    () => sub.handleTopupPayment({ payload: topupPayload(undefined), webhookId: "wh_no_payid" }),
    /topup_missing_payment_anchor/,
    "must throw rather than acknowledge",
  );
  assert.equal(calls.topup_grant.length, 0, "topup_grant must not be called");
  assert.equal(calls.record.length, 0, "recordCreditedPaymentEvent must not be called");
});

// ── 2. anchor identity is the payment id, never the webhook id ───────────────
test("2 PINNING (also passes on baseline): op key and anchor key derive from payment_id only - does NOT prove a single credit movement, topup_grant is mocked", async () => {
  const pay = "pay_STABLE_1";
  await sub.handleTopupPayment({ payload: topupPayload(pay), webhookId: "wh_A" });
  await sub.handleTopupPayment({ payload: topupPayload(pay), webhookId: "wh_B" });

  assert.equal(calls.topup_grant.length, 2, "both deliveries reach the grant primitive");
  const opIds = calls.topup_grant.map((c) => c.opId);
  assert.deepEqual(opIds, [`dodo_topup:${pay}`, `dodo_topup:${pay}`], "op id is derived from payment_id only");
  assert.equal(new Set(opIds).size, 1, "one financial op key (real idempotency belongs to the primitive and is NOT proven here)");
  for (const o of opIds) assert.ok(!o.includes("wh_"), "webhook id must never appear in the financial op key");
  for (const r of calls.record) {
    assert.equal(r.idempotencyKey, `topup:${pay}`, "anchor key derives from payment_id");
    assert.equal(r.providerPaymentId, pay);
    assert.ok(!String(r.idempotencyKey).includes("wh_"), "webhook id must never be the anchor");
  }
});

// ── 3. anchor recovery across a failed first delivery ────────────────────────
test("3 anchor write is retried and succeeds even when the grant is an idempotent no-op", async () => {
  const pay = "pay_RECOVER_1";
  // First delivery: grant applies, anchor persistence fails → must propagate.
  behave.record = async () => { throw new Error("anchor_write_failed"); };
  await assert.rejects(
    () => sub.handleTopupPayment({ payload: topupPayload(pay), webhookId: "wh_1" }),
    /anchor_write_failed/,
    "first delivery must fail, not acknowledge success",
  );
  assert.equal(calls.topup_grant.length, 1);
  assert.equal(calls.record.length, 1, "anchor write was attempted");

  // Retry: grant is now an idempotent no-op (applied=false). Anchor MUST still be written.
  behave.record = null;
  behave.topup_grant = async () => ({ applied: false, balance: 100, delta: 0 });
  const r = await sub.handleTopupPayment({ payload: topupPayload(pay), webhookId: "wh_2" });
  assert.equal(r.handled, true);
  assert.equal(r.applied, false, "retry did not move credits again");
  assert.equal(calls.record.length, 2, "anchor write MUST run on the applied=false retry");
  assert.equal(calls.record[1].providerPaymentId, pay);
  // Real single-credit-movement proof requires the DB integration test, not this mock.
});

// ── 6/7. valid top-up still works; replay is a no-op ────────────────────────
test("7 valid top-up grants and anchors; an INJECTED applied=false replay still re-asserts the anchor - does NOT exercise real grant idempotency", async () => {
  const pay = "pay_HAPPY_1";
  const a = await sub.handleTopupPayment({ payload: topupPayload(pay), webhookId: "wh_x" });
  assert.equal(a.handled, true);
  assert.equal(a.applied, true);
  assert.equal(calls.record.length, 1);

  behave.topup_grant = async () => ({ applied: false, balance: 100, delta: 0 });
  const b = await sub.handleTopupPayment({ payload: topupPayload(pay), webhookId: "wh_y" });
  assert.equal(b.applied, false, "replay is an idempotent no-op");
  assert.equal(calls.record.length, 2, "anchor remains idempotently re-asserted");
});

// ── 5. tenants_plan_check 23514 fails closed ────────────────────────────────
test("5 tenants_plan_check 23514 propagates and prevents any credit reset", async () => {
  behave.query = async (sql) => {
    const s = String(sql);
    if (/UPDATE tenants SET plan/.test(s)) { const e = new Error("violates check constraint"); e.code = "23514"; throw e; }
    if (/dodo_subscription_lookup|FROM dodo_subscriptions/.test(s)) {
      return { rows: [{ tenant_id: "11111111-1111-1111-1111-111111111111", user_id: null, plan_key: "starter" }], rowCount: 1 };
    }
    return { rows: [], rowCount: 1 };
  };
  await assert.rejects(
    () => sub.handleSubscriptionEvent({
      payload: { type: "subscription.active", data: { subscription_id: "sub_1", product_id: "prod_starter", metadata: { tenant_id: "11111111-1111-1111-1111-111111111111", plan_key: "starter" } } },
      webhookId: "wh_plan",
    }),
    (e) => e.code === "23514",
    "23514 must propagate, not be swallowed into a 200",
  );
  assert.equal(calls.reset.length, 0, "reset_entitlement must NOT run after a failed plan set");
});
