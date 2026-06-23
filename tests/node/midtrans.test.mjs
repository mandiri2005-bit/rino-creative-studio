// midtrans.test.mjs — Midtrans rail unit tests (pure logic; no DB / network).
// Covers server-authoritative pricing, the SHA512 notification verifier, and the
// order_id format. Run from backend/ after `npm install`:
//   node --test ../tests/node/midtrans.test.mjs
import { test, after } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";

// Set a server key BEFORE import so verifyNotification is active for the test.
process.env.MIDTRANS_SERVER_KEY ??= "SB-Mid-server-TESTKEY";
const SK = process.env.MIDTRANS_SERVER_KEY;

const mid = await import("../../backend/midtrans.mjs");
const { redis } = await import("../../backend/redis.js");
after(() => { try { redis.disconnect(); } catch {} });

const sign = (orderId, statusCode, gross) =>
  crypto.createHash("sha512").update(`${orderId}${statusCode}${gross}${SK}`).digest("hex");

test("priceForPlan returns an IDR price, 0 for unknown", () => {
  assert.ok(mid.priceForPlan("starter") > 0);
  assert.ok(mid.priceForPlan("pro") > 0);
  assert.ok(mid.priceForPlan("studio") > 0);
  assert.equal(mid.priceForPlan("bogus"), 0);
});

test("verifyNotification accepts a correctly SHA512-signed body", () => {
  const body = { order_id: "ceritai-abc-1", status_code: "200", gross_amount: "199000.00",
                 signature_key: sign("ceritai-abc-1", "200", "199000.00") };
  assert.equal(mid.verifyNotification(body), true);
});

test("verifyNotification rejects tampered amount, bad signature, and missing fields", () => {
  const good = sign("o1", "200", "79000.00");
  // tampered gross_amount → signature no longer matches
  assert.equal(mid.verifyNotification({ order_id: "o1", status_code: "200", gross_amount: "99999.00", signature_key: good }), false);
  // forged signature
  assert.equal(mid.verifyNotification({ order_id: "o1", status_code: "200", gross_amount: "79000.00", signature_key: "deadbeef" }), false);
  // missing fields
  assert.equal(mid.verifyNotification({ order_id: "o1", status_code: "200" }), false);
  assert.equal(mid.verifyNotification({}), false);
  assert.equal(mid.verifyNotification(null), false);
});

test("_newOrderId is unique and within Midtrans' 50-char limit", () => {
  const tid = "11111111-2222-3333-4444-555555555555";
  const a = mid._newOrderId(tid), b = mid._newOrderId(tid);
  assert.notEqual(a, b);
  assert.ok(a.length <= 50, `order_id too long: ${a.length}`);
  assert.match(a, /^ceritai-/);
});

test("isConfigured reflects whether a server key is present", () => {
  assert.equal(mid.isConfigured(), !!process.env.MIDTRANS_SERVER_KEY);
});
