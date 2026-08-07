// l2c_realdb_tranche2.test.mjs — REAL-DATABASE proofs for L2C tranche 2.
//
// NO MOCKS of financial primitives. Real handleSubscriptionEvent /
// recordCreditedPaymentEvent against a disposable PostgreSQL cluster carrying the
// repo's own migrations INCLUDING 0084.
//
//   ATOMICITY (defect 1 — _upsertSub could commit before _setTenantPlan failed)
//     A1 failed plan set on a NEW subscription      -> no subscription row survives
//     A2 failed plan set on an EXISTING subscription -> the prior row is UNCHANGED
//     A3 successful transition                       -> sub row AND tenants.plan both land
//     A4 on_hold                                     -> sub row lands, tenants.plan UNTOUCHED
//
//   ANCHOR UNIQUENESS (defect 2 — two credited rows per provider payment)
//     U1 second credited anchor, different webhook id -> rejected 23505, first row intact
//     U2 an UNCREDITED row may share the payment id with the credited one
//     U3 NULL provider_payment_id rows never collide
//     U4 the 0084 guard itself aborts when duplicates pre-exist, and names them
//
// Every fault mechanism is a temporary trigger inside the disposable database,
// installed and removed in try/finally. No production fault hooks.
//
// Run from backend/:
//   NODE_ENV=development PGSSLMODE=disable \
//   DATABASE_POOL_URL_DEV=postgres://postgres@127.0.0.1:55432/l2ctest \
//   node --test ../tests/node/l2c_realdb_tranche2.test.mjs
import { test, after } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";

process.env.REDIS_URL ??= "redis://localhost:6379";
process.env.BILLING_MODE = "subscription";
process.env.DODO_PRODUCT_STARTER ??= "prod_starter";

const { pool } = await import("../../backend/db.js");
const sub = await import("../../backend/dodo_subscriptions.mjs");
const core = await import("../../backend/payments_core.mjs");
const { redis } = await import("../../backend/redis.js");
after(async () => { try { redis.disconnect(); } catch {} try { await pool.end(); } catch {} });

const q = (s, p = []) => pool.query(s, p);

async function newTenant(tag, plan = null) {
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
  if (plan) await q("UPDATE tenants SET plan=$2 WHERE id=$1", [id, plan]);
  return id;
}

const tenantPlan = async (t) => (await q("SELECT plan FROM tenants WHERE id=$1", [t])).rows[0]?.plan ?? null;
const subRow = async (subId) =>
  (await q("SELECT plan_key, status FROM dodo_subscriptions WHERE dodo_subscription_id=$1", [subId])).rows[0] || null;
const anchors = async (pay) =>
  (await q(`SELECT id, idempotency_key, credited, credits_granted FROM payment_events
             WHERE provider='dodo' AND provider_payment_id=$1 ORDER BY created_at`, [pay])).rows;

const activeEvent = (tenantId, subId, plan = "starter") => ({
  payload: { type: "subscription.active",
    data: { subscription_id: subId, product_id: "prod_starter",
      metadata: { tenant_id: tenantId, plan_key: plan } } },
  webhookId: `wh_${subId}`,
});

/** Install a BEFORE UPDATE trigger on tenants that raises 23514, run fn, always clean up. */
async function withPlanCheckFailure(fn) {
  await q(`CREATE OR REPLACE FUNCTION _t2_plan_check() RETURNS trigger LANGUAGE plpgsql AS
           $$ BEGIN RAISE EXCEPTION 'injected tenants_plan_check' USING ERRCODE = '23514'; END $$`);
  await q(`CREATE TRIGGER _t2_plan_check BEFORE UPDATE ON tenants
           FOR EACH ROW EXECUTE FUNCTION _t2_plan_check()`);
  try { return await fn(); }
  finally {
    await q("DROP TRIGGER IF EXISTS _t2_plan_check ON tenants").catch(() => {});
    await q("DROP FUNCTION IF EXISTS _t2_plan_check()").catch(() => {});
  }
}

// ══ PRECONDITION: 0084's index must actually be present ═════════════════════
test("REALDB-T2-0 migration 0084 is applied and the partial unique index exists", async () => {
  const r = await q(`SELECT indexdef FROM pg_indexes
                      WHERE tablename='payment_events' AND indexname='payment_events_credited_anchor_uniq'`);
  assert.equal(r.rows.length, 1, "payment_events_credited_anchor_uniq must exist (migration 0084 applied)");
  const def = r.rows[0].indexdef;
  assert.match(def, /UNIQUE/, "must be a UNIQUE index");
  assert.match(def, /provider.*provider_payment_id/s, "must key on (provider, provider_payment_id)");
  assert.match(def, /WHERE .*credited/s, "must be PARTIAL on credited — an uncredited row is not an anchor");
});

// ══ A1. ATOMICITY, NEW SUBSCRIPTION ═════════════════════════════════════════
test("REALDB-T2-A1 failed plan set rolls the NEW subscription upsert back", async () => {
  const tag = "a1_" + crypto.randomBytes(4).toString("hex");
  const T = await newTenant(tag);
  const SUBID = `sub_${tag}`;
  await withPlanCheckFailure(async () => {
    await assert.rejects(() => sub.handleSubscriptionEvent(activeEvent(T, SUBID)),
      (e) => e.code === "23514", "23514 must propagate");
  });
  assert.equal(await subRow(SUBID), null,
    "no dodo_subscriptions row may survive a transition whose plan set failed");
});

// ══ A2. ATOMICITY, EXISTING SUBSCRIPTION ════════════════════════════════════
// The sharper case: an upsert that would have MUTATED a good row. A partial commit here
// corrupts existing state rather than merely creating an orphan.
test("REALDB-T2-A2 failed plan set leaves an EXISTING subscription row byte-unchanged", async () => {
  const tag = "a2_" + crypto.randomBytes(4).toString("hex");
  const T = await newTenant(tag);
  const SUBID = `sub_${tag}`;

  await sub.handleSubscriptionEvent(activeEvent(T, SUBID, "starter"));
  const before = await subRow(SUBID);
  assert.ok(before, "precondition: the first transition landed");
  assert.equal(before.plan_key, "starter");
  const planBefore = await tenantPlan(T);

  await withPlanCheckFailure(async () => {
    await assert.rejects(
      () => sub.handleSubscriptionEvent({
        payload: { type: "subscription.plan_changed",
          data: { subscription_id: SUBID, product_id: "prod_starter",
            metadata: { tenant_id: T, plan_key: "plus" } } },
        webhookId: `wh_change_${tag}`,
      }),
      (e) => e.code === "23514", "23514 must propagate");
  });

  assert.deepEqual(await subRow(SUBID), before,
    "the existing subscription row must be untouched — no half-applied plan_key");
  assert.equal(await tenantPlan(T), planBefore, "tenants.plan unchanged");
});

// ══ A3. HAPPY PATH STILL COMMITS BOTH ═══════════════════════════════════════
test("REALDB-T2-A3 a successful transition commits the sub row AND tenants.plan together", async () => {
  const tag = "a3_" + crypto.randomBytes(4).toString("hex");
  const T = await newTenant(tag);
  const SUBID = `sub_${tag}`;
  await sub.handleSubscriptionEvent(activeEvent(T, SUBID, "starter"));
  const row = await subRow(SUBID);
  assert.ok(row, "subscription row committed");
  assert.equal(row.plan_key, "starter");
  assert.equal(await tenantPlan(T), "starter", "tenants.plan mirrored in the same transaction");
});

// ══ A4. PLAN-UNTOUCHED SEMANTICS SURVIVE ════════════════════════════════════
// on_hold must NOT move the tier — access persists through dunning. The atomicity rewrite
// must not have quietly turned that into a plan write.
test("REALDB-T2-A4 on_hold records the subscription but leaves tenants.plan untouched", async () => {
  const tag = "a4_" + crypto.randomBytes(4).toString("hex");
  const T = await newTenant(tag);
  const SUBID = `sub_${tag}`;
  await sub.handleSubscriptionEvent(activeEvent(T, SUBID, "starter"));
  assert.equal(await tenantPlan(T), "starter");

  await sub.handleSubscriptionEvent({
    payload: { type: "subscription.on_hold",
      data: { subscription_id: SUBID, product_id: "prod_starter",
        metadata: { tenant_id: T, plan_key: "starter" } } },
    webhookId: `wh_hold_${tag}`,
  });

  assert.equal((await subRow(SUBID)).status, "on_hold", "status recorded");
  assert.equal(await tenantPlan(T), "starter",
    "dunning must NOT downgrade the tier — plan-untouched paths stay untouched");
});

// ══ U1. THE DEFECT ITSELF ═══════════════════════════════════════════════════
test("REALDB-T2-U1 a second CREDITED anchor for one payment is rejected, first row intact", async () => {
  const tag = "u1_" + crypto.randomBytes(4).toString("hex");
  const T = await newTenant(tag);
  const PAY = `pay_${tag}`;

  await core.recordCreditedPaymentEvent({
    tenantId: T, userId: null, provider: "dodo", idempotencyKey: `wh_first_${tag}`,
    providerPaymentId: PAY, creditsGranted: 5000, bucket: "sub",
  });
  const first = await anchors(PAY);
  assert.equal(first.length, 1, "precondition: exactly one credited anchor");

  // A DIFFERENT webhook id: receipt identity does not collide, so ON CONFLICT
  // (provider, idempotency_key) does not absorb it. Anchor identity must.
  await assert.rejects(
    () => core.recordCreditedPaymentEvent({
      tenantId: T, userId: null, provider: "dodo", idempotencyKey: `wh_second_${tag}`,
      providerPaymentId: PAY, creditsGranted: 5000, bucket: "sub",
    }),
    (e) => e.code === "23505" && String(e.constraint || "").includes("credited_anchor"),
    "a second credited anchor must be rejected by payment_events_credited_anchor_uniq",
  );

  const after_ = await anchors(PAY);
  assert.equal(after_.length, 1, "still exactly one anchor row");
  assert.equal(after_[0].id, first[0].id, "and it is the ORIGINAL row, not a replacement");
  assert.equal(Number(after_[0].credits_granted), 5000, "credits_granted untouched");
});

// ══ U2. THE LEGITIMATE CASE MUST STILL WORK ═════════════════════════════════
// An uncredited row for the same payment is an ordinary lifecycle, not a duplicate anchor.
// A non-partial constraint would have rejected this.
test("REALDB-T2-U2 an UNCREDITED row may share the payment id with the credited anchor", async () => {
  const tag = "u2_" + crypto.randomBytes(4).toString("hex");
  const T = await newTenant(tag);
  const PAY = `pay_${tag}`;

  await q(`INSERT INTO payment_events (tenant_id, provider, idempotency_key, provider_payment_id,
             status, credited) VALUES ($1,'dodo',$2,$3,'pending',FALSE)`, [T, `wh_pending_${tag}`, PAY]);
  await core.recordCreditedPaymentEvent({
    tenantId: T, userId: null, provider: "dodo", idempotencyKey: `wh_ok_${tag}`,
    providerPaymentId: PAY, creditsGranted: 100, bucket: "topup",
  });
  await q(`INSERT INTO payment_events (tenant_id, provider, idempotency_key, provider_payment_id,
             status, credited) VALUES ($1,'dodo',$2,$3,'failed',FALSE)`, [T, `wh_failed_${tag}`, PAY]);

  const rows = await anchors(PAY);
  assert.equal(rows.length, 3, "two uncredited rows coexist with one credited anchor");
  assert.equal(rows.filter((r) => r.credited).length, 1, "exactly one of them is credited");
});

// ══ U3. NULL ANCHORS NEVER COLLIDE ══════════════════════════════════════════
test("REALDB-T2-U3 credited rows with NULL provider_payment_id do not collide", async () => {
  const tag = "u3_" + crypto.randomBytes(4).toString("hex");
  const T = await newTenant(tag);
  for (const n of [1, 2, 3]) {
    await core.recordCreditedPaymentEvent({
      tenantId: T, userId: null, provider: "dodo", idempotencyKey: `wh_null_${tag}_${n}`,
      providerPaymentId: null, creditsGranted: 10, bucket: "sub",
    });
  }
  const c = Number((await q(
    `SELECT count(*) c FROM payment_events
      WHERE tenant_id=$1 AND provider_payment_id IS NULL AND credited`, [T])).rows[0].c);
  assert.equal(c, 3, "NULL anchors are not constrained — the index is partial on NOT NULL");
});

// ══ U5. THE BENIGN RACE MUST STAY IDEMPOTENT ════════════════════════════════
// The counterpart to U1. Same payment, SAME derived anchor key (tranche 1 keys the anchor on
// payment_id, not the webhook id), many concurrent deliveries. ON CONFLICT names only the
// RECEIPT arbiter, so a loser can hit the anchor index first and raise 23505 with nothing
// actually wrong. That must be absorbed, not surfaced — otherwise an ordinary duplicate
// webhook delivery 500s. The postcondition is deterministic even though the race is not.
test("REALDB-T2-U5 concurrent deliveries sharing one anchor key all settle, leaving one anchor", async () => {
  const tag = "u5_" + crypto.randomBytes(4).toString("hex");
  const T = await newTenant(tag);
  const PAY = `pay_${tag}`;
  const KEY = `topup:${PAY}`;           // the tranche-1 shape: derived from payment_id

  const results = await Promise.allSettled(
    Array.from({ length: 6 }, () => core.recordCreditedPaymentEvent({
      tenantId: T, userId: null, provider: "dodo", idempotencyKey: KEY,
      providerPaymentId: PAY, creditsGranted: 250, bucket: "topup",
    })),
  );
  const failed = results.filter((r) => r.status === "rejected").map((r) => r.reason?.message);
  assert.deepEqual(failed, [], `every delivery of one receipt must settle: ${JSON.stringify(failed)}`);

  const rows = await anchors(PAY);
  assert.equal(rows.length, 1, "exactly ONE credited anchor survives the pile-up");
  assert.equal(rows[0].idempotency_key, KEY, "and it is the shared receipt key");

  // The benign path must not have weakened U1: a DIFFERENT receipt is still rejected.
  await assert.rejects(
    () => core.recordCreditedPaymentEvent({
      tenantId: T, userId: null, provider: "dodo", idempotencyKey: `wh_other_${tag}`,
      providerPaymentId: PAY, creditsGranted: 250, bucket: "topup",
    }),
    (e) => e.code === "23505",
    "a different receipt claiming the same payment must STILL be rejected",
  );
});

// ══ U4. THE MIGRATION GUARD ═════════════════════════════════════════════════
// Proves 0084 refuses to run against pre-existing duplicates instead of silently choosing a
// winner. Drops the index, manufactures the duplicate the constraint normally prevents, and
// runs 0084's own guard block verbatim. Restores the index in finally.
test("REALDB-T2-U4 the 0084 guard aborts on pre-existing duplicates and names them", async () => {
  const tag = "u4_" + crypto.randomBytes(4).toString("hex");
  const T = await newTenant(tag);
  const PAY = `pay_${tag}`;
  const GUARD = `
    DO $$
    DECLARE v_pairs INT; v_rows INT; v_list TEXT;
    BEGIN
      SELECT count(*), COALESCE(sum(n),0), COALESCE(string_agg(
               format('(%s, %s) x%s', provider, provider_payment_id, n), '; ' ORDER BY n DESC), '')
        INTO v_pairs, v_rows, v_list
        FROM (SELECT provider, provider_payment_id, count(*) AS n
                FROM public.payment_events
               WHERE credited AND provider_payment_id IS NOT NULL
               GROUP BY provider, provider_payment_id HAVING count(*) > 1) d;
      IF v_pairs > 0 THEN
        RAISE EXCEPTION 'L2C 0084 ABORT: % provider payment(s) already carry more than one CREDITED payment_events row (% rows total). Offenders: %',
          v_pairs, v_rows, v_list;
      END IF;
    END $$;`;

  try {
    await q("DROP INDEX IF EXISTS payment_events_credited_anchor_uniq");
    for (const n of [1, 2]) {
      await q(`INSERT INTO payment_events (tenant_id, provider, idempotency_key, provider_payment_id,
                 status, credited, credits_granted) VALUES ($1,'dodo',$2,$3,'succeeded',TRUE,500)`,
        [T, `wh_dup_${tag}_${n}`, PAY]);
    }
    assert.equal((await anchors(PAY)).length, 2, "precondition: the duplicate exists");

    // The guard must abort, and must be specific enough to act on.
    await assert.rejects(() => q(GUARD), (e) => {
      assert.match(e.message, /L2C 0084 ABORT/, "must identify itself");
      assert.match(e.message, new RegExp(PAY), "must name the offending payment id");
      return true;
    }, "0084 must refuse to run while duplicates exist");

    // And it is the guard, not the index, that stops it: with duplicates present the index
    // creation would also fail — proving the constraint is genuinely violated by this data.
    await assert.rejects(
      () => q(`CREATE UNIQUE INDEX payment_events_credited_anchor_uniq
                 ON public.payment_events (provider, provider_payment_id)
                 WHERE credited AND provider_payment_id IS NOT NULL`),
      (e) => e.code === "23505",
      "the index itself is genuinely violated by the duplicate",
    );
  } finally {
    await q("DELETE FROM payment_events WHERE provider_payment_id=$1", [PAY]).catch(() => {});
    await q(`CREATE UNIQUE INDEX IF NOT EXISTS payment_events_credited_anchor_uniq
               ON public.payment_events (provider, provider_payment_id)
               WHERE credited AND provider_payment_id IS NOT NULL`).catch(() => {});
  }

  const idx = await q(`SELECT 1 FROM pg_indexes WHERE indexname='payment_events_credited_anchor_uniq'`);
  assert.equal(idx.rows.length, 1, "index restored after the guard test");
});
