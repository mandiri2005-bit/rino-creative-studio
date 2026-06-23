// dodo.test.mjs — Dodo rail unit tests (pure logic; no DB / network).
// Plan→credits moved to payments_core (shared); this covers Dodo-specific bits.
// Run from backend/ after `npm install`:  node --test ../tests/node/dodo.test.mjs
import { test, after } from "node:test";
import assert from "node:assert/strict";

process.env.DODO_PAYMENTS_API_KEY ??= ""; // keep unconfigured unless env provides keys

const dodo = await import("../../backend/dodo.mjs");
// dodo.mjs → payments_core → redis.js opens an ioredis connection at load; drop it.
const { redis } = await import("../../backend/redis.js");
after(() => { try { redis.disconnect(); } catch {} });

test("_freshTimestamp enforces a ±5 minute replay window", () => {
  const now = 1_700_000_000;
  assert.equal(dodo._freshTimestamp(String(now), now), true);
  assert.equal(dodo._freshTimestamp(String(now - 299), now), true);
  assert.equal(dodo._freshTimestamp(String(now + 299), now), true);
  assert.equal(dodo._freshTimestamp(String(now - 301), now), false);
  assert.equal(dodo._freshTimestamp(String(now + 301), now), false);
  assert.equal(dodo._freshTimestamp("not-a-number", now), false);
  assert.equal(dodo._freshTimestamp("", now), false);
  assert.equal(dodo._freshTimestamp(undefined, now), false);
});

test("productForPlan returns null for unknown / unconfigured plans", () => {
  assert.equal(dodo.productForPlan("bogus"), null);
  if (!process.env.DODO_PRODUCT_ID_PRO) assert.equal(dodo.productForPlan("pro"), null);
});

test("isConfigured reflects whether an API key is present", () => {
  assert.equal(dodo.isConfigured(), !!process.env.DODO_PAYMENTS_API_KEY);
});
