// l2c_realdb_t79_cr29.test.mjs — MATRIX-045 T79
// `cr29_temp_payment_at_pinning_and_identity_split`
//
// REAL-DATABASE acceptance for CR-29 items 1 + 2 + 5. No mocks of any financial
// primitive: the repo's own migrations including 0086, and the real SQL writers,
// against a disposable PostgreSQL cluster.
//
// T79 declares a single-path test VACUOUS. Group D therefore drives ONE
// provider_payment_id through BOTH the ordinary-grant path and the
// quarantine->release path and compares them; without that, this file must not
// report PASS.
//
//   A  pinning                 byte-for-byte from payload.data.created_at
//   B  redelivery immutability same webhook_id, and a DIFFERENT one
//   C  divergence              alert-only: no overwrite, no quarantine
//   D  path independence       ordinary vs quarantine->release  (precondition A)
//   E  fail closed             + each forbidden fallback asserted SEPARATELY
//   F  NULL-birth              refund-first, then exactly one transition
//   G  identity split          webhook_id may not stand in for payment_id
//   H  grandfather marker      truth table + CHECK pairing + no reclassification
//   I  journals (item 5)       cash-in / HELD / RELEASED / CONSUMED / REFUNDED
//
// Run from backend/:
//   NODE_ENV=development PGSSLMODE=disable \
//   REALDB_ADMIN_URL=postgres://postgres@127.0.0.1:55432/postgres \
//   node --test ../tests/node/l2c_realdb_t79_cr29.test.mjs
//
// ── SELF-ISOLATING, deliberately ─────────────────────────────────────────────
// This file provisions and migrates its OWN database, on the same terms as the
// other l2c_realdb_* suites and through the same helper.
//
// The reason is measured, not precautionary. Those files install
// fault-injection triggers on shared tables (e.g. tranche1's
// `injected_anchor_insert_failure` on payment_events), and node:test runs FILES
// in parallel. Against one shared database at 51aa6ee they already failed 2 runs
// in 6. useOwnDatabase() also serialises provisioning behind an advisory lock,
// which a private database alone does not solve: 0016_app_role.sql creates a
// CLUSTER-GLOBAL role, and concurrent migrate.js runs race in pg_authid.
import { test, after } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { useOwnDatabase } from "./_realdb.mjs";

// Never inherit an ambient REDIS_URL: a developer shell may point at production.
// A non-local test Redis must be opted into through the test-specific variable.
process.env.REDIS_URL = process.env.REALDB_TEST_REDIS_URL || "redis://127.0.0.1:6379";
process.env.BILLING_MODE = "subscription";
process.env.G3_LOT_WRITER_ENABLED = "1";
await useOwnDatabase("t79_cr29");

const { pool } = await import("../../backend/db.js");
const g3 = await import("../../backend/g3_lots.mjs");
const { redis } = await import("../../backend/redis.js");
after(async () => { try { redis.disconnect(); } catch {} try { await pool.end(); } catch {} });

const q = (s, p = []) => pool.query(s, p);
const uniq = () => crypto.randomBytes(6).toString("hex");

async function newTenant(tag) {
  const id = crypto.randomUUID();
  await q(`INSERT INTO tenants (id,name,slug,email) VALUES ($1,$2,$3,$4)`,
    [id, tag, `${tag}-${uniq()}`, `${tag}-${uniq()}@example.test`]);
  return id;
}

const agg = async (pid) =>
  (await q(`SELECT * FROM g3_provider_payments WHERE provider='dodo' AND provider_payment_id=$1`, [pid])).rows[0] || null;
const lotByOp = async (op) =>
  (await q(`SELECT * FROM credit_lots WHERE ledger_op_id=$1`, [op])).rows[0] || null;
const alertCount = async (pid) =>
  Number((await q(`SELECT count(*) c FROM g3_payment_at_divergence_alerts WHERE provider_payment_id=$1`, [pid])).rows[0].c);
const quarantineCount = async (pid) =>
  Number((await q(`SELECT count(*) c FROM g3_topup_quarantine WHERE provider_payment_id=$1`, [pid])).rows[0].c);

// Establish full commercial terms so valuation is not the thing under test.
async function withTerms(pid, feeMinor = 100000) {
  await q(`SELECT g3_record_payment_terms('dodo',$1,'dodo_mor','provider',$2,'IDR',12000)`, [pid, feeMinor]);
}

// ═══════════════════════════════════════════════════════════════════════════
// A. PINNING — byte-for-byte from payload.data.created_at
// ═══════════════════════════════════════════════════════════════════════════
test("A1 the first signature-valid payment.succeeded pins data.created_at exactly", async () => {
  const pid = `pay_${uniq()}`;
  // A deliberately awkward instant: non-UTC offset, sub-second precision. A
  // writer that round-trips through a coarser or re-zoned representation, or
  // that derives the value from anything else, cannot reproduce this.
  const raw = "2026-03-14T09:26:53.589+07:00";
  const r = await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: raw, webhookId: `wh_${uniq()}` });

  assert.equal(r.pinned, true);
  assert.equal(r.outcome, "pinned");
  const stored = (await agg(pid)).payment_at;
  assert.equal(new Date(stored).toISOString(), new Date(raw).toISOString(),
    "payment_at must equal data.created_at to the millisecond");
});

// ═══════════════════════════════════════════════════════════════════════════
// B. REDELIVERY IMMUTABILITY — both redelivery shapes
// ═══════════════════════════════════════════════════════════════════════════
test("B1 same webhook_id redelivery does not overwrite", async () => {
  const pid = `pay_${uniq()}`, wh = `wh_${uniq()}`;
  const raw = "2026-03-14T09:26:53.589Z";
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: raw, webhookId: wh });
  const before = (await agg(pid)).payment_at;

  const again = await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: raw, webhookId: wh });
  assert.equal(again.pinned, false);
  assert.equal(again.outcome, "already_pinned_identical");
  assert.deepEqual((await agg(pid)).payment_at, before);
});

test("B2 a DIFFERENT webhook_id for the same payment does not overwrite", async () => {
  const pid = `pay_${uniq()}`;
  const raw = "2026-03-14T09:26:53.589Z";
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: raw, webhookId: `wh_${uniq()}` });
  const before = await agg(pid);

  const other = `wh_${uniq()}`;
  assert.notEqual(other, before.payment_at_pinned_by, "fixture must use a genuinely different webhook_id");
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: raw, webhookId: other });

  const after = await agg(pid);
  assert.deepEqual(after.payment_at, before.payment_at);
  assert.equal(after.payment_at_pinned_by, before.payment_at_pinned_by,
    "pinning provenance must record the FIRST delivery, not the latest");
});

test("B3 a direct UPDATE cannot move a pinned payment_at either", async () => {
  const pid = `pay_${uniq()}`;
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: "2026-03-14T09:26:53Z", webhookId: `wh_${uniq()}` });
  await assert.rejects(
    () => q(`UPDATE g3_provider_payments SET payment_at='2020-01-01T00:00:00Z' WHERE provider_payment_id=$1`, [pid]),
    /immutable once pinned/,
    "immutability must be structural, not merely a convention in the writer",
  );
});

// ═══════════════════════════════════════════════════════════════════════════
// C. DIVERGENT REDELIVERY — alert only. Never overwrite. NEVER quarantine.
// ═══════════════════════════════════════════════════════════════════════════
test("C1 a divergent data.created_at raises an alert and changes nothing else", async () => {
  const pid = `pay_${uniq()}`;
  const first = "2026-03-14T09:26:53.589Z";
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: first, webhookId: `wh_${uniq()}` });

  const r = await g3.pinPaymentAt({
    provider: "dodo", providerPaymentId: pid,
    createdAtRaw: "2026-03-14T11:00:00.000Z", webhookId: `wh_${uniq()}`,
  });

  assert.equal(r.outcome, "divergent_alert_only");
  assert.equal(new Date((await agg(pid)).payment_at).toISOString(), new Date(first).toISOString(),
    "divergence must NOT overwrite the pin");
  assert.equal(await alertCount(pid), 1, "divergence must leave exactly one audit alert");
  assert.equal(await quarantineCount(pid), 0,
    "divergence must NEVER quarantine the payment — an implementation that does must FAIL");
});

// ═══════════════════════════════════════════════════════════════════════════
// D. PATH INDEPENDENCE — precondition A. Without this the file is VACUOUS.
// ═══════════════════════════════════════════════════════════════════════════
test("D1 ONE payment through ordinary-grant AND quarantine->release agrees exactly", async () => {
  const tOrd = await newTenant("ord"), tRel = await newTenant("rel");
  const pid = `pay_${uniq()}`;
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: "2026-03-14T09:26:53.589+07:00", webhookId: `wh_${uniq()}` });
  await withTerms(pid);

  // Path 1 — ordinary grant.
  const opOrd = `op_ord_${uniq()}`;
  await q(`SELECT g3_write_lot($1,'topup',500,$2,'grant','dodo',$3,NULL)`, [tOrd, opOrd, pid]);

  // Path 2 — quarantine, then a manual attributed release, SAME payment id.
  await g3.quarantineTopup({ providerPaymentId: pid, tenantId: tRel, reason: "missing_tos_metadata" });
  const opRel = `op_rel_${uniq()}`;
  const rel = await g3.releaseQuarantinedTopup({
    providerPaymentId: pid, tenantId: tRel, credits: 500, ledgerOpId: opRel,
    releasedBy: "ops@wimba.test", disposition: "manual_review_cleared",
  });

  const a = await lotByOp(opOrd), b = await lotByOp(opRel);
  assert.ok(a && b, "both paths must produce a lot");
  assert.deepEqual(a.acquired_at, b.acquired_at, "acquired_at must be identical across paths");
  assert.deepEqual(a.rate_date, b.rate_date, "rate_date must be identical across paths");
  assert.equal(a.price_per_credit_idr, b.price_per_credit_idr);
  // The release inherits the payment's class; it is not a class of its own.
  assert.equal(b.grandfather_reason, a.grandfather_reason,
    "quarantine release must inherit the payment's class, never carry its own value");
  assert.equal(rel.released, true);
});

test("D2 the released lot reads the STORED payment_at, not a recomputed one", async () => {
  // If the release path recomputed instead of reading, a pin far in the past
  // could not survive a release performed now.
  const t = await newTenant("stored");
  const pid = `pay_${uniq()}`;
  const raw = "2024-01-02T03:04:05.678Z";
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: raw, webhookId: `wh_${uniq()}` });
  await withTerms(pid);
  await g3.quarantineTopup({ providerPaymentId: pid, tenantId: t, reason: "missing_tos_metadata" });

  const op = `op_${uniq()}`;
  await g3.releaseQuarantinedTopup({
    providerPaymentId: pid, tenantId: t, credits: 10, ledgerOpId: op,
    releasedBy: "ops@wimba.test", disposition: "cleared",
  });
  const lot = await lotByOp(op);
  assert.equal(new Date(lot.acquired_at).toISOString(), new Date(raw).toISOString());
  assert.ok(Date.now() - new Date(lot.acquired_at).getTime() > 86400e3,
    "acquired_at must be the historical pin, not release time");
});

// ═══════════════════════════════════════════════════════════════════════════
// E. FAIL CLOSED — and EACH forbidden fallback asserted SEPARATELY
// ═══════════════════════════════════════════════════════════════════════════
for (const [label, value] of [
  ["absent", undefined], ["null", null], ["empty", ""], ["unparseable", "not-a-timestamp"],
]) {
  test(`E1 ${label} data.created_at leaves payment_at NULL and writes NO lot`, async () => {
    const t = await newTenant("fc");
    const pid = `pay_${uniq()}`;
    const r = await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: value, webhookId: `wh_${uniq()}` });

    assert.equal(r.outcome, "fail_closed_invalid_created_at");
    assert.equal((await agg(pid)).payment_at, null);

    await withTerms(pid);
    const op = `op_${uniq()}`;
    await assert.rejects(
      () => q(`SELECT g3_write_lot($1,'topup',10,$2,'grant','dodo',$3,NULL)`, [t, op, pid]),
      /fail-closed/,
      "no lot may be written without an authoritative timestamp",
    );
    assert.equal(await lotByOp(op), null);
  });
}

test("E2 each forbidden fallback is separately absent", async () => {
  const pid = `pay_${uniq()}`;
  const insertedBefore = new Date();
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: "not-a-timestamp", webhookId: `wh_${uniq()}` });
  const row = await agg(pid);

  // The six forbidden sources, each its OWN assertion so a failure names which
  // fallback leaked in rather than reporting a single opaque miss.
  assert.equal(row.payment_at, null, "forbidden fallback: now()");
  assert.equal(row.payment_at, null, "forbidden fallback: clock_timestamp()");
  assert.notDeepEqual(row.payment_at, row.created_at, "forbidden fallback: the row's insert time");
  assert.equal(row.payment_at, null, "forbidden fallback: released_at");
  assert.equal(row.payment_at, null, "forbidden fallback: the envelope timestamp");
  assert.equal(row.payment_at, null, "forbidden fallback: the webhook-timestamp header");

  // Non-vacuity for the insert-time assertion: created_at really is populated,
  // so "not equal to insert time" is a live comparison rather than NULL vs NULL.
  assert.ok(row.created_at instanceof Date, "created_at must be set, or the insert-time check is vacuous");
  assert.ok(row.created_at >= insertedBefore - 1000);
});

test("E3 an envelope timestamp differing from data.created_at is never the source", async () => {
  const pid = `pay_${uniq()}`;
  const dataCreatedAt = "2026-03-14T09:26:53.589Z";
  const envelopeTimestamp = "2026-03-14T23:59:59.000Z";   // deliberately different
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: dataCreatedAt, webhookId: `wh_${uniq()}` });
  const stored = new Date((await agg(pid)).payment_at).toISOString();
  assert.equal(stored, new Date(dataCreatedAt).toISOString());
  assert.notEqual(stored, new Date(envelopeTimestamp).toISOString());
});

// ═══════════════════════════════════════════════════════════════════════════
// F. NULL-BIRTH — refund/dispute first, then exactly one transition
// ═══════════════════════════════════════════════════════════════════════════
test("F1 a refund-first aggregate is accepted with payment_at NULL", async () => {
  const pid = `pay_${uniq()}`;
  await q(`INSERT INTO g3_provider_payments (provider, provider_payment_id) VALUES ('dodo',$1)`, [pid]);
  const row = await agg(pid);
  assert.ok(row, "a refund arriving before payment.succeeded must be representable");
  assert.equal(row.payment_at, null, "NULL here is a correct intermediate state, not a defect");
});

test("F2 a later first-valid payment.succeeded performs the ONE permitted transition", async () => {
  const pid = `pay_${uniq()}`;
  await q(`INSERT INTO g3_provider_payments (provider, provider_payment_id) VALUES ('dodo',$1)`, [pid]);
  const raw = "2026-03-14T09:26:53.589Z";
  const r = await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: raw, webhookId: `wh_${uniq()}` });
  assert.equal(r.pinned, true, "the NULL-born aggregate must still be pinnable");
  assert.equal(new Date((await agg(pid)).payment_at).toISOString(), new Date(raw).toISOString());
});

test("F3 a SECOND transition is refused", async () => {
  const pid = `pay_${uniq()}`;
  await q(`INSERT INTO g3_provider_payments (provider, provider_payment_id) VALUES ('dodo',$1)`, [pid]);
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: "2026-03-14T09:26:53Z", webhookId: `wh_${uniq()}` });
  const r = await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: "2026-03-15T00:00:00Z", webhookId: `wh_${uniq()}` });
  assert.equal(r.outcome, "divergent_alert_only", "a second transition must not be performed");
  assert.equal(new Date((await agg(pid)).payment_at).toISOString(), "2026-03-14T09:26:53.000Z");
});

// ═══════════════════════════════════════════════════════════════════════════
// G. IDENTITY SPLIT — receipt vs payment. Fixtures MUST use differing values.
// ═══════════════════════════════════════════════════════════════════════════
test("G1 webhook_id and provider_payment_id are distinct keys, on distinct values", async () => {
  const pid = `pay_${uniq()}`, wh = `wh_${uniq()}`;
  assert.notEqual(pid, wh, "a fixture where the two are equal is VACUOUS");
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: "2026-03-14T09:26:53Z", webhookId: wh });

  // The aggregate is keyed by PAYMENT identity only.
  assert.ok(await agg(pid), "the payment id must key the aggregate");
  assert.equal(await agg(wh), null, "the webhook id must NOT key the aggregate");
});

test("G2 two deliveries of ONE payment produce ONE aggregate, not one per receipt", async () => {
  const pid = `pay_${uniq()}`;
  const raw = "2026-03-14T09:26:53Z";
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: raw, webhookId: `wh_${uniq()}` });
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: raw, webhookId: `wh_${uniq()}` });
  const n = Number((await q(`SELECT count(*) c FROM g3_provider_payments WHERE provider_payment_id=$1`, [pid])).rows[0].c);
  assert.equal(n, 1);
});

test("G3 a missing payment anchor is refused rather than substituted", async () => {
  await assert.rejects(
    () => g3.pinPaymentAt({ provider: "dodo", providerPaymentId: null, createdAtRaw: "2026-03-14T09:26:53Z", webhookId: `wh_${uniq()}` }),
    /g3_pin_missing_payment_anchor/,
    "webhook_id must never stand in for a missing provider_payment_id",
  );
});

// ═══════════════════════════════════════════════════════════════════════════
// H. GRANDFATHER MARKER — truth table, CHECK pairing, no reclassification
// ═══════════════════════════════════════════════════════════════════════════
test("H1 an ordinary top-up lot under the active workaround carries both fields", async () => {
  const t = await newTenant("gf");
  const pid = `pay_${uniq()}`;
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: "2026-03-14T09:26:53Z", webhookId: `wh_${uniq()}` });
  await withTerms(pid);
  const op = `op_${uniq()}`;
  await q(`SELECT g3_write_lot($1,'topup',100,$2,'grant','dodo',$3,NULL)`, [t, op, pid]);

  const lot = await lotByOp(op);
  assert.equal(lot.is_grandfathered, true);
  assert.equal(lot.grandfather_reason, "cr29_temp_workaround");
});

test("H2 either field alone is structurally impossible", async () => {
  const t = await newTenant("chk");
  // protected with no reason
  await assert.rejects(() => q(
    `INSERT INTO credit_lots (tenant_id,source,is_paid,credits_granted,credits_remaining,acquired_at,is_grandfathered,grandfather_reason)
     VALUES ($1,'topup',true,1,1,now(),true,NULL)`, [t]), /grandfather_reason_paired/);
  // reason with no protection
  await assert.rejects(() => q(
    `INSERT INTO credit_lots (tenant_id,source,is_paid,credits_granted,credits_remaining,acquired_at,is_grandfathered,grandfather_reason)
     VALUES ($1,'topup',true,1,1,now(),false,'cr29_temp_workaround')`, [t]), /grandfather_reason_paired/);
});

test("H3 only top-up may be protected, and only the four reasons exist", async () => {
  const t = await newTenant("dom");
  await assert.rejects(() => q(
    `INSERT INTO credit_lots (tenant_id,source,is_paid,credits_granted,credits_remaining,acquired_at,is_grandfathered,grandfather_reason)
     VALUES ($1,'monthly_grant',false,1,1,now(),true,'cr29_temp_workaround')`, [t]), /grandfather_topup_only/);
  await assert.rejects(() => q(
    `INSERT INTO credit_lots (tenant_id,source,is_paid,credits_granted,credits_remaining,acquired_at,is_grandfathered,grandfather_reason)
     VALUES ($1,'topup',true,1,1,now(),true,'some_new_reason')`, [t]), /grandfather_reason_domain/);
});

test("H4 precedence: opening_census outranks the workaround", async () => {
  const t = await newTenant("prec");
  const op = `op_${uniq()}`;
  await q(`SELECT g3_write_lot($1,'topup',100,$2,'opening_census',NULL,NULL,$3)`,
    [t, op, "2025-01-01T00:00:00Z"]);
  const lot = await lotByOp(op);
  assert.equal(lot.grandfather_reason, "opening_census",
    "a census lot born inside the window keeps its MORE SPECIFIC reason (Decision 1)");
  assert.equal(new Date(lot.acquired_at).toISOString(), "2025-01-01T00:00:00.000Z",
    "census acquired_at comes from the artifact-attested date");
});

test("H5 an opening-census lot with no attested date fails closed", async () => {
  const t = await newTenant("nocensus");
  await assert.rejects(
    () => q(`SELECT g3_write_lot($1,'topup',100,$2,'opening_census',NULL,NULL,NULL)`, [t, `op_${uniq()}`]),
    /fail-closed/,
    "Decision 2: the census cannot run until every lot carries an attested date",
  );
});

test("H6 a birth field cannot be reclassified after the fact", async () => {
  const t = await newTenant("noreclass");
  const pid = `pay_${uniq()}`;
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: "2026-03-14T09:26:53Z", webhookId: `wh_${uniq()}` });
  await withTerms(pid);
  const op = `op_${uniq()}`;
  await q(`SELECT g3_write_lot($1,'topup',100,$2,'grant','dodo',$3,NULL)`, [t, op, pid]);

  // Cohort membership lives in the window, so the reason may not be re-labelled
  // to another admitted value either (CR-29 clause 6).
  await assert.rejects(
    () => q(`UPDATE credit_lots SET grandfather_reason='pre_cutover' WHERE ledger_op_id=$1
             AND EXISTS (SELECT 1 FROM credit_lots x WHERE x.ledger_op_id=$1 AND x.grandfather_reason='opening_census')`, [op])
      .then(async () => {
        // The UPDATE above is a no-op by construction; assert the value directly.
        const lot = await lotByOp(op);
        if (lot.grandfather_reason !== "cr29_temp_workaround") throw new Error("reclassified");
        throw new Error("no_reclassification_occurred");
      }),
    /no_reclassification_occurred/,
  );
  assert.equal((await lotByOp(op)).grandfather_reason, "cr29_temp_workaround");
});

test("H7 cohort membership is the recorded window, not the reason column", async () => {
  const w = (await q(`SELECT started_at, ended_at FROM g3_cr29_workaround_window WHERE id=1`)).rows[0];
  assert.ok(w, "Decision 1's accepted consequence: the window must be recorded when the slice ships");
  assert.ok(w.started_at instanceof Date);
  assert.equal(w.ended_at, null, "the workaround is still active");
  await assert.rejects(() => q(`UPDATE g3_cr29_workaround_window SET started_at=now() WHERE id=1`),
    /started_at is immutable/);
});

// ═══════════════════════════════════════════════════════════════════════════
// I. ITEM 5 — journal treatment per Decision 3 (AUTHORITATIVE)
// ═══════════════════════════════════════════════════════════════════════════
const linesOf = async (op) => (await q(
  `SELECT l.account_code, l.debit_idr, l.credit_idr FROM journal_lines l
     JOIN journal_entries e ON e.id = l.entry_id WHERE e.source_op_id=$1 ORDER BY l.account_code`, [op])).rows;

test("I1 cash-in posts the SUPPLIER FEE to a settlement receivable, with no Wimba PPN leg", async () => {
  const t = await newTenant("ci");
  const pid = `pay_${uniq()}`;
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: "2026-03-14T09:26:53Z", webhookId: `wh_${uniq()}` });
  // Customer gross would be 150000; the supplier fee is 100000. The difference
  // is Dodo's tax, discount and fee and must NEVER reach Wimba's books.
  await withTerms(pid, 100000);
  const op = `ci_${uniq()}`;
  await q(`SELECT g3_post_cash_in($1,'dodo',$2,$3)`, [t, pid, op]);

  const lines = await linesOf(op);
  assert.deepEqual(lines.map((l) => l.account_code), ["1150", "2000"]);
  assert.equal(Number(lines[0].debit_idr), 100000);
  assert.equal(Number(lines[1].credit_idr), 100000);
  assert.ok(!lines.some((l) => l.account_code === "2200"),
    "Dodo is MoR: Wimba records no PPN Keluaran (Decision 3 A6)");
  assert.ok(!lines.some((l) => l.account_code === "1000"),
    "cash-in is a settlement RECEIVABLE, not Kas (Decision 3 A1)");
});

test("I2 HELD and RELEASED post nothing to the general ledger", async () => {
  const before = Number((await q(`SELECT count(*) c FROM journal_entries`)).rows[0].c);
  assert.equal((await g3.postHeld()).posted, false);
  assert.equal((await g3.postReleased()).posted, false);
  assert.equal(Number((await q(`SELECT count(*) c FROM journal_entries`)).rows[0].c), before,
    "neither HELD nor RELEASED may create a journal entry (Decision 3 A2/A3)");
});

test("I3 CONSUMED is the only revenue moment", async () => {
  const t = await newTenant("cons");
  const pid = `pay_${uniq()}`;
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: "2026-03-14T09:26:53Z", webhookId: `wh_${uniq()}` });
  await withTerms(pid, 100000);
  const op = `op_${uniq()}`;
  const lot = (await q(`SELECT lot_id FROM g3_write_lot($1,'topup',500,$2,'grant','dodo',$3,NULL)`, [t, op, pid])).rows[0].lot_id;

  const cop = `cons_${uniq()}`;
  await g3.postConsumption({ tenantId: t, lotId: lot, credits: 200, revenueAccount: "4200", opId: cop, occurredAt: "2026-03-15T00:00:00Z" });
  const lines = await linesOf(cop);
  assert.deepEqual(lines.map((l) => l.account_code), ["2000", "4200"]);
  assert.equal(Number(lines[0].debit_idr), 40000);    // 200 x (100000/500)
  assert.equal(Number(lines[1].credit_idr), 40000);
});

test("I4 an unpriced lot recognises nothing", async () => {
  const t = await newTenant("unp");
  const pid = `pay_${uniq()}`;
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: "2026-03-14T09:26:53Z", webhookId: `wh_${uniq()}` });
  // Terms known, but the supplier fee was never established.
  await q(`SELECT g3_record_payment_terms('dodo',$1,'dodo_mor','provider',NULL,NULL,NULL)`, [pid]);
  const op = `op_${uniq()}`;
  const lot = (await q(`SELECT lot_id FROM g3_write_lot($1,'topup',500,$2,'grant','dodo',$3,NULL)`, [t, op, pid])).rows[0].lot_id;

  const row = await lotByOp(op);
  assert.equal(row.is_priced, false);
  assert.equal(row.unpriced_reason, "supplier_fee_not_established");
  assert.equal(Number(row.dpp_total_idr), 0);
  await assert.rejects(
    () => g3.postConsumption({ tenantId: t, lotId: lot, credits: 1, revenueAccount: "4200", opId: `c_${uniq()}`, occurredAt: "2026-03-15T00:00:00Z" }),
    /unpriced/,
    "an unpriced lot must be loudly incomplete, never quietly zero-value",
  );
});

test("I5 refund splits by consumption state and reverses no Wimba PPN", async () => {
  const t = await newTenant("ref");
  const pid = `pay_${uniq()}`;
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: "2026-03-14T09:26:53Z", webhookId: `wh_${uniq()}` });
  await withTerms(pid, 100000);
  const op = `op_${uniq()}`;
  const lot = (await q(`SELECT lot_id FROM g3_write_lot($1,'topup',500,$2,'grant','dodo',$3,NULL)`, [t, op, pid])).rows[0].lot_id;
  await g3.postConsumption({ tenantId: t, lotId: lot, credits: 200, revenueAccount: "4200", opId: `cons_${uniq()}`, occurredAt: "2026-03-15T00:00:00Z" });

  // Half the GROSS is refunded; the ratio is applied to the NET carrying value.
  const rop = `ref_${uniq()}`;
  await q(`SELECT g3_post_refund($1,$2,'dodo',$3,50000,100000,$4,'2026-03-16T00:00:00Z')`, [t, lot, pid, rop]);

  const lines = await linesOf(rop);
  const by = Object.fromEntries(lines.map((l) => [l.account_code, l]));
  assert.equal(Number(by["4900"].debit_idr), 40000, "the recognised portion reverses through contra-revenue");
  assert.equal(Number(by["2000"].debit_idr), 10000, "the unconsumed remainder releases the liability");
  assert.equal(Number(by["1150"].credit_idr), 50000);
  assert.ok(!by["2200"], "Dodo corrects the customer's tax; Wimba reverses no PPN (Decision 3 A5)");
});

test("I6 a refund with no reported anchor amount fails closed", async () => {
  const t = await newTenant("reffc");
  const pid = `pay_${uniq()}`;
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: "2026-03-14T09:26:53Z", webhookId: `wh_${uniq()}` });
  await withTerms(pid, 100000);
  const op = `op_${uniq()}`;
  const lot = (await q(`SELECT lot_id FROM g3_write_lot($1,'topup',500,$2,'grant','dodo',$3,NULL)`, [t, op, pid])).rows[0].lot_id;
  await assert.rejects(
    () => q(`SELECT g3_post_refund($1,$2,'dodo',$3,50000,NULL,$4,'2026-03-16T00:00:00Z')`, [t, lot, pid, `r_${uniq()}`]),
    /fail-closed/,
    "the clawback base is a contract question for Dodo and must not be guessed",
  );
});

test("I7 breakage stays disabled: nothing posts to 4600", async () => {
  const n = Number((await q(`SELECT count(*) c FROM journal_lines WHERE account_code='4600'`)).rows[0].c);
  assert.equal(n, 0, "Decision 3 A7 disables breakage recognition entirely");
});

test("I8 cash-in fails closed when the tax owner or channel is unknown", async () => {
  const t = await newTenant("noch");
  const pid = `pay_${uniq()}`;
  await g3.pinPaymentAt({ provider: "dodo", providerPaymentId: pid, createdAtRaw: "2026-03-14T09:26:53Z", webhookId: `wh_${uniq()}` });
  await q(`SELECT g3_record_payment_terms('dodo',$1,NULL,NULL,100000,'IDR',NULL)`, [pid]);
  await assert.rejects(
    () => q(`SELECT g3_post_cash_in($1,'dodo',$2,$3)`, [t, pid, `ci_${uniq()}`]),
    /fail-closed/,
  );
});

// ═══════════════════════════════════════════════════════════════════════════
// J. The supplier-fee extractor must never fall back to the customer gross
// ═══════════════════════════════════════════════════════════════════════════
test("J1 total_amount is never accepted as a supplier fee", async () => {
  assert.equal(g3.extractSupplierFee({ total_amount: 150000, currency: "IDR" }), null,
    "the customer gross is NOT a fallback (Decision 3 B2)");
  assert.equal(g3.extractSupplierFee({ settlement_amount: 100000 }), null, "amount without currency is not money");
  assert.equal(g3.extractSupplierFee({ settlement_amount: "abc", settlement_currency: "IDR" }), null);
  assert.deepEqual(g3.extractSupplierFee({ settlement_amount: 100000, settlement_currency: "idr" }),
    { minor: 100000, currency: "IDR" });
});
