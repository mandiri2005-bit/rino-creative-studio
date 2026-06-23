// payments_core.test.mjs — shared grant core unit tests (pure mapping; no DB).
// The atomic grant_entitlement path is integration-tested against a DB (Phase E).
// Run from backend/ after `npm install`:  node --test ../tests/node/payments_core.test.mjs
import { test, after } from "node:test";
import assert from "node:assert/strict";

const pc = await import("../../backend/payments_core.mjs");
const { redis } = await import("../../backend/redis.js");
after(() => { try { redis.disconnect(); } catch {} });

test("creditsForPlan maps plan keys to tier credits (studio→enterprise)", () => {
  assert.equal(pc.creditsForPlan("starter"), pc._TIER_CREDITS.starter);
  assert.equal(pc.creditsForPlan("pro"), pc._TIER_CREDITS.pro);
  assert.equal(pc.creditsForPlan("studio"), pc._TIER_CREDITS.enterprise);
  assert.equal(pc.creditsForPlan("bogus"), 0);
  assert.equal(pc.creditsForPlan(undefined), 0);
});

test("VALID_PLAN_KEYS is the client-facing plan set", () => {
  assert.deepEqual(pc.VALID_PLAN_KEYS, ["starter", "pro", "studio"]);
});

test("PLAN_TO_TIER maps studio to the enterprise tier", () => {
  assert.equal(pc.PLAN_TO_TIER.studio, "enterprise");
});
