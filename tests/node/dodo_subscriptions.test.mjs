// dodo_subscriptions.test.mjs — pure-logic unit tests for the global subscription
// module (plan config, email canonicalisation, disposable guard, product reverse-map,
// billing-mode switch). No DB/network. Run from backend/:
//   node --test ../tests/node/dodo_subscriptions.test.mjs
import { test, after, beforeEach } from "node:test";
import assert from "node:assert/strict";

// Keep db.js / redis side effects inert (pool lazy; redis disconnected in after()).
process.env.DATABASE_POOL_URL_DEV ??= "postgres://u:p@localhost:5432/db";
process.env.REDIS_URL ??= "redis://localhost:6379";
// Product ids for the reverse-map test (set BEFORE importing the module).
process.env.DODO_PRODUCT_STARTER = "prod_starter";
process.env.DODO_PRODUCT_PLUS = "prod_plus";
process.env.DODO_PRODUCT_PRO = "prod_pro";
process.env.DODO_PRODUCT_ULTRA = "prod_ultra";
process.env.DODO_PRODUCT_BOOST10 = "prod_boost10";
process.env.DODO_PRODUCT_BOOST50 = "prod_boost50";
process.env.DODO_PRODUCT_BOOST100 = "prod_boost100";

const sub = await import("../../backend/dodo_subscriptions.mjs");
const { redis } = await import("../../backend/redis.js");
after(() => { try { redis.disconnect(); } catch {} });

beforeEach(() => { delete process.env.BILLING_MODE; });

test("subscriptionMode() only true for BILLING_MODE=subscription", () => {
  assert.equal(sub.subscriptionMode(), false);          // unset → one_time (Indonesia-safe default)
  process.env.BILLING_MODE = "one_time";
  assert.equal(sub.subscriptionMode(), false);
  process.env.BILLING_MODE = "subscription";
  assert.equal(sub.subscriptionMode(), true);
});

test("VALID_SUB_PLANS excludes free, lists the 4 paid plans", () => {
  assert.ok(!sub.VALID_SUB_PLANS.includes("free"), "free is grant-only, never subscribed");
  for (const p of ["starter", "plus", "pro", "ultra"]) assert.ok(sub.VALID_SUB_PLANS.includes(p), p);
});

test("subPlanCredits maps the locked global table (defaults)", () => {
  assert.equal(sub.subPlanCredits("free"), 500);
  assert.equal(sub.subPlanCredits("starter"), 5000);
  assert.equal(sub.subPlanCredits("plus"), 20000);
  assert.equal(sub.subPlanCredits("pro"), 50000);
  assert.equal(sub.subPlanCredits("ultra"), 100000);
  assert.equal(sub.subPlanCredits("bogus"), 0);
});

test("isSubPlan recognises configured plans", () => {
  assert.equal(sub.isSubPlan("ultra"), true);
  assert.equal(sub.isSubPlan("enterprise"), false);
});

test("subProductId / planForProduct round-trip via env", () => {
  assert.equal(sub.subProductId("starter"), "prod_starter");
  assert.equal(sub.subProductId("ultra"), "prod_ultra");
  assert.equal(sub.subProductId("free"), null);        // free has no product_env
  assert.equal(sub.planForProduct("prod_pro"), "pro");
  assert.equal(sub.planForProduct("prod_unknown"), null);
  assert.equal(sub.planForProduct(null), null);
});

test("normalizeEmailHash collapses Gmail dot/+tag variants to ONE identity", () => {
  const a = sub.normalizeEmailHash("john.doe@gmail.com");
  const b = sub.normalizeEmailHash("johndoe@gmail.com");
  const c = sub.normalizeEmailHash("j.o.h.n.doe+promo@gmail.com");
  const d = sub.normalizeEmailHash("JohnDoe@googlemail.com");   // googlemail alias + case
  assert.equal(a, b, "dots are insignificant on gmail");
  assert.equal(a, c, "+tag stripped on gmail");
  assert.equal(a, d, "googlemail == gmail, case-insensitive");
});

test("normalizeEmailHash keeps dots significant on non-Gmail providers", () => {
  const a = sub.normalizeEmailHash("john.doe@outlook.com");
  const b = sub.normalizeEmailHash("johndoe@outlook.com");
  assert.notEqual(a, b, "non-gmail dots are a different mailbox");
});

test("normalizeEmailHash rejects malformed input", () => {
  assert.equal(sub.normalizeEmailHash("not-an-email"), null);
  assert.equal(sub.normalizeEmailHash("@nolocal.com"), null);
  assert.equal(sub.normalizeEmailHash(""), null);
});

test("isDisposableDomain blocks throwaway domains, allows normal, rejects empty", () => {
  assert.equal(sub.isDisposableDomain("mailinator.com"), true);
  assert.equal(sub.isDisposableDomain("gmail.com"), false);
  assert.equal(sub.isDisposableDomain(""), true, "no domain → reject (cannot verify)");
  assert.equal(sub.isDisposableDomain("GUERRILLAMAIL.COM"), true, "case-insensitive");
});

// ── Top-up packs (one-time) ───────────────────────────────────────────────────
test("VALID_TOPUP_PACKS lists the 3 boost packs", () => {
  assert.deepEqual(sub.VALID_TOPUP_PACKS.sort(), ["boost_10", "boost_100", "boost_50"]);
});

test("topupPackCredits maps the locked boost table", () => {
  assert.equal(sub.topupPackCredits("boost_10"), 5000);
  assert.equal(sub.topupPackCredits("boost_50"), 28000);
  assert.equal(sub.topupPackCredits("boost_100"), 60000);
  assert.equal(sub.topupPackCredits("bogus"), 0);
});

test("isTopupPack / topupProductId resolve via env", () => {
  assert.equal(sub.isTopupPack("boost_50"), true);
  assert.equal(sub.isTopupPack("starter"), false, "a sub plan is NOT a topup pack");
  assert.equal(sub.topupProductId("boost_10"), "prod_boost10");
  assert.equal(sub.topupProductId("boost_100"), "prod_boost100");
  assert.equal(sub.topupProductId("bogus"), null);
});

test("topup packs and sub plans are disjoint key spaces", () => {
  for (const p of sub.VALID_TOPUP_PACKS) assert.ok(!sub.VALID_SUB_PLANS.includes(p), `${p} must not be a sub plan`);
  for (const p of sub.VALID_SUB_PLANS) assert.ok(!sub.VALID_TOPUP_PACKS.includes(p), `${p} must not be a topup pack`);
});
