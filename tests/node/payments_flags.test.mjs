// payments_flags.test.mjs — per-rail kill switch semantics (pure env logic; no DB).
// Run from backend/ after `npm install`:  node --test ../tests/node/payments_flags.test.mjs
import { test, after, beforeEach } from "node:test";
import assert from "node:assert/strict";

// Keep db.js import side-effect-safe (pool lazy; redis disconnected in after()).
process.env.DATABASE_POOL_URL_DEV ??= "postgres://u:p@localhost:5432/db";
process.env.REDIS_URL ??= "redis://localhost:6379";

const dodo = await import("../../backend/dodo.mjs");
const midtrans = await import("../../backend/midtrans.mjs");
const { redis } = await import("../../backend/redis.js");
after(() => { try { redis.disconnect(); } catch {} });

function clearFlags() {
  delete process.env.PAYMENTS_ENABLED;
  delete process.env.DODO_ENABLED;
  delete process.env.MIDTRANS_ENABLED;
}
beforeEach(clearFlags);
after(clearFlags);

test("default ACTIVE when all flags unset (forgotten var ≠ outage)", () => {
  assert.equal(dodo.railEnabled(), true);
  assert.equal(midtrans.railEnabled(), true);
});

test("explicit DODO_ENABLED=false disables ONLY Dodo", () => {
  process.env.DODO_ENABLED = "false";
  assert.equal(dodo.railEnabled(), false);
  assert.equal(midtrans.railEnabled(), true);
});

test("explicit MIDTRANS_ENABLED=false disables ONLY Midtrans", () => {
  process.env.MIDTRANS_ENABLED = "false";
  assert.equal(midtrans.railEnabled(), false);
  assert.equal(dodo.railEnabled(), true);
});

test("global PAYMENTS_ENABLED=false disables BOTH rails", () => {
  process.env.PAYMENTS_ENABLED = "false";
  assert.equal(dodo.railEnabled(), false);
  assert.equal(midtrans.railEnabled(), false);
});

test("only the literal string 'false' disables — any other value stays ACTIVE", () => {
  for (const v of ["true", "1", "TRUE", "yes", ""]) {
    process.env.DODO_ENABLED = v;
    assert.equal(dodo.railEnabled(), true, `DODO_ENABLED=${JSON.stringify(v)} should stay enabled`);
  }
});
