// l2c_orphan_durability.test.mjs — L2C tranche-1 tests for defects 3 and 4.
//
// SCOPE: imports the REAL backend/payments_core.mjs. Only the database boundary
// (db.js query + pool) and redis are mocked, so the control flow, ordering and
// return values under test are the production ones. This file does NOT prove real
// balance/ledger effects or concurrency; that requires the disposable-DB harness.
//
//   defect 3  orphan-queue INSERT failure swallowed      payments_core.mjs:277
//   defect 4  orphan-consumption failure swallowed       payments_core.mjs:242
//
// Run from backend/:
//   node --test --experimental-test-module-mocks ../tests/node/l2c_orphan_durability.test.mjs
import { test, mock, beforeEach } from "node:test";
import assert from "node:assert/strict";

process.env.DATABASE_POOL_URL_DEV ??= "postgres://u:p@localhost:5432/db";
process.env.REDIS_URL ??= "redis://localhost:6379";

// ── controllable DB boundary ────────────────────────────────────────────────
const db = { sql: [], handlers: [] };
const norm = (s) => String(s).replace(/\s+/g, " ").trim();
const run = async (sql, params) => {
  const s = norm(sql);
  db.sql.push(s);
  for (const h of db.handlers) if (h.m.test(s)) return h.fn(s, params);
  return { rows: [], rowCount: 1 };
};
const on = (m, fn) => db.handlers.push({ m, fn });
const boom = (msg) => () => { throw new Error(msg); };
const seen = (re) => db.sql.filter((s) => re.test(s));
beforeEach(() => { db.sql = []; db.handlers = []; });

mock.module("../../backend/db.js", {
  namedExports: {
    query: run,
    pool: { connect: async () => ({ query: run, release() {} }), end: async () => {} },
    withTenant: async (_t, fn) => fn(),
  },
});
mock.module("../../backend/redis.js", {
  namedExports: { redis: { disconnect() {}, del: async () => {}, eval: async () => {}, decrby: async () => {}, incrby: async () => {} } },
});

const { reverse_entitlement, recordCreditedPaymentEvent } = await import("../../backend/payments_core.mjs");

const RE_LOOKUP = /payment_event_for_reversal/;
const RE_ORPHAN_INS = /INSERT INTO orphan_reversals/;
const RE_ORPHAN_SEL = /FROM orphan_reversals WHERE provider/;
const RE_ORPHAN_UPD = /UPDATE orphan_reversals SET applied_at/;
const RE_ANCHOR_INS = /INSERT INTO payment_events/;
const RE_CREDIT = /credit_apply|credit_reverse_grant|credit_debit_guarded/;

const revArgs = (over = {}) => ({
  provider: "dodo", providerPaymentId: "pay_ORPH_1",
  refundOpId: "refund:dodo:rf_1", kind: "refund", refundAmount: 100, rawEvent: {}, ...over,
});
const recArgs = (over = {}) => ({
  tenantId: "11111111-1111-1111-1111-111111111111", userId: null, provider: "dodo",
  idempotencyKey: "wh_anchor_1", providerPaymentId: "pay_ORPH_1", planKey: "starter",
  amount: 1000, currency: "USD", creditsGranted: 500, bucket: "sub", rawEvent: {}, ...over,
});
// One pending orphan row returned by the consumption SELECT.
const onePendingOrphan = () => on(RE_ORPHAN_SEL, () => ({
  rows: [{ id: 7, refund_op_id: "refund:dodo:rf_1", kind: "refund", refund_amount: 100, raw_event: {} }], rowCount: 1,
}));

// ══ DEFECT 3 — orphan queue durability ══════════════════════════════════════

test("D3.1 queue INSERT failure rejects and never returns orphan_queued", async () => {
  on(RE_LOOKUP, () => ({ rows: [], rowCount: 0 }));      // no anchor yet
  on(RE_ORPHAN_INS, boom("orphan_insert_failed"));
  await assert.rejects(() => reverse_entitlement(revArgs()), /orphan_insert_failed/);
  assert.equal(seen(RE_ORPHAN_INS).length, 1, "the INSERT was attempted");
});

test("D3.2 orphan_queued is returned only after the INSERT committed", async () => {
  on(RE_LOOKUP, () => ({ rows: [], rowCount: 0 }));
  on(RE_ORPHAN_INS, () => ({ rows: [], rowCount: 1 }));
  const r = await reverse_entitlement(revArgs());
  assert.deepEqual(r, { reversed: false, reason: "orphan_queued" });
  const iLookup = db.sql.findIndex((s) => RE_LOOKUP.test(s));
  const iInsert = db.sql.findIndex((s) => RE_ORPHAN_INS.test(s));
  assert.ok(iLookup >= 0 && iInsert > iLookup, "lookup precedes the queue INSERT");
  assert.equal(seen(RE_CREDIT).length, 0, "queuing performs no financial mutation");
});

// ══ DEFECT 4 — orphan consumption durability ════════════════════════════════

test("D4.1 orphan SELECT failure propagates", async () => {
  on(RE_ANCHOR_INS, () => ({ rows: [], rowCount: 1 }));
  on(RE_ORPHAN_SEL, boom("orphan_select_failed"));
  await assert.rejects(() => recordCreditedPaymentEvent(recArgs()), /orphan_select_failed/);
  assert.equal(seen(RE_ORPHAN_UPD).length, 0, "nothing was marked applied");
});

test("D4.2 reversal failure during consumption propagates and marks nothing applied", async () => {
  on(RE_ANCHOR_INS, () => ({ rows: [], rowCount: 1 }));
  onePendingOrphan();
  on(RE_LOOKUP, boom("reversal_lookup_failed"));
  await assert.rejects(() => recordCreditedPaymentEvent(recArgs()), /reversal_lookup_failed/);
  assert.equal(seen(RE_ORPHAN_UPD).length, 0, "applied_at must stay unset");
});

test("D4.3 unrecognized reversal result rejects and leaves applied_at unset", async () => {
  on(RE_ANCHOR_INS, () => ({ rows: [], rowCount: 1 }));
  onePendingOrphan();
  on(RE_LOOKUP, () => ({ rows: [], rowCount: 0 }));   // anchor still unresolvable -> re-queues
  on(RE_ORPHAN_INS, () => ({ rows: [], rowCount: 1 }));
  await assert.rejects(() => recordCreditedPaymentEvent(recArgs()), /orphan_apply_unresolved/);
  assert.equal(seen(RE_ORPHAN_UPD).length, 0, "applied_at must stay unset");
});

test("D4.4 applied_at UPDATE failure propagates", async () => {
  on(RE_ANCHOR_INS, () => ({ rows: [], rowCount: 1 }));
  onePendingOrphan();
  on(RE_LOOKUP, () => ({ rows: [{ id: 1, tenant_id: "t", user_id: null, credits_granted: 500, credited: false, status: "succeeded", reversed_credits: 0, bucket: "sub", amount: 1000 }], rowCount: 1 }));
  on(RE_ORPHAN_UPD, boom("applied_at_update_failed"));
  await assert.rejects(() => recordCreditedPaymentEvent(recArgs()), /applied_at_update_failed/);
  assert.equal(seen(RE_ORPHAN_UPD).length, 1, "the UPDATE was attempted");
  assert.equal(seen(RE_CREDIT).length, 0, "not_credited performs no financial mutation");
});

test("D4.5 terminal not_credited marks the orphan applied", async () => {
  on(RE_ANCHOR_INS, () => ({ rows: [], rowCount: 1 }));
  onePendingOrphan();
  on(RE_LOOKUP, () => ({ rows: [{ id: 1, tenant_id: "t", user_id: null, credits_granted: 500, credited: false, status: "succeeded", reversed_credits: 0, bucket: "sub", amount: 1000 }], rowCount: 1 }));
  await recordCreditedPaymentEvent(recArgs());
  assert.equal(seen(RE_ORPHAN_UPD).length, 1, "orphan marked applied");
  assert.equal(seen(RE_CREDIT).length, 0);
});

test("D4.6 terminal already_reversed marks applied with no second financial mutation", async () => {
  on(RE_ANCHOR_INS, () => ({ rows: [], rowCount: 1 }));
  onePendingOrphan();
  on(RE_LOOKUP, () => ({ rows: [{ id: 1, tenant_id: "11111111-1111-1111-1111-111111111111", user_id: null, credits_granted: 500, credited: true, status: "refunded", reversed_credits: 500, bucket: "sub", amount: 1000 }], rowCount: 1 }));
  // anchored path: already fully reversed -> nothing left to reverse
  on(/FROM payment_events WHERE id=\$1 FOR UPDATE/, () => ({ rows: [{ user_id: null, credits_granted: 500, reversed_credits: 500, bucket: "sub", amount: 1000, status: "refunded" }], rowCount: 1 }));
  await recordCreditedPaymentEvent(recArgs());
  assert.equal(seen(RE_ORPHAN_UPD).length, 1, "orphan marked applied");
  assert.equal(seen(RE_CREDIT).length, 0, "no second financial mutation on an already-reversed anchor");
});

// NOTE ON SCOPE: both attempts below start from an ALREADY fully-reversed payment
// event (reversed_credits === credits_granted), so BOTH reversal calls are terminal
// no-ops. This proves convergence FROM AN ALREADY-REVERSED STATE with zero mutations
// across both attempts. It does NOT prove "exactly one mutation on attempt 1, zero on
// retry" — that requires a real credit reversal and belongs to the disposable real-DB
// suite (see l2c_realdb_*.test.mjs).
test("D4.7 retry converges from an already-reversed state; zero mutations across both attempts (does NOT prove one-then-zero)", async () => {
  const anchored = () => {
    on(RE_ANCHOR_INS, () => ({ rows: [], rowCount: 1 }));
    onePendingOrphan();
    on(RE_LOOKUP, () => ({ rows: [{ id: 1, tenant_id: "11111111-1111-1111-1111-111111111111", user_id: null, credits_granted: 500, credited: true, status: "refunded", reversed_credits: 500, bucket: "sub", amount: 1000 }], rowCount: 1 }));
    on(/FROM payment_events WHERE id=\$1 FOR UPDATE/, () => ({ rows: [{ user_id: null, credits_granted: 500, reversed_credits: 500, bucket: "sub", amount: 1000, status: "refunded" }], rowCount: 1 }));
  };
  // attempt 1 — reversal is a TERMINAL no-op (already reversed); applied_at update fails
  anchored();
  on(RE_ORPHAN_UPD, boom("applied_at_update_failed"));
  await assert.rejects(() => recordCreditedPaymentEvent(recArgs()), /applied_at_update_failed/);
  const mutations1 = seen(RE_CREDIT).length;

  // attempt 2 — same orphan still pending, update now succeeds
  db.sql = []; db.handlers = [];
  anchored();
  await recordCreditedPaymentEvent(recArgs());
  assert.equal(seen(RE_ORPHAN_UPD).length, 1, "converges: orphan marked applied on retry");
  assert.equal(mutations1 + seen(RE_CREDIT).length, 0, "zero financial mutations across both attempts");
});
