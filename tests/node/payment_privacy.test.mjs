// Guards for the stored-payload allowlist (audit F-06).
//
// The sample below is the REAL shape of the live production row, not an invented fixture:
// the key set was read out of payment_events.raw_event on 2026-08-01 with
// `jsonb_object_keys` (names only — no values were ever read), so these assertions bind the
// payload the gateway actually sends. A test built on a hand-written fixture would only
// prove the parser works on the fixture; that mistake is on record in this project.
//
// No database, no network, no npm dependency — this file runs anywhere.

import { test, after } from "node:test";
import assert from "node:assert/strict";

// The round-trip test below opens the pg pool and, through payments_core, a Redis client.
// Both keep the event loop alive, so without this the runner finishes its assertions and
// then hangs forever instead of exiting. Every import is optional and guarded: when there
// is no database the round-trip test skips, these modules were never loaded, and the
// dynamic imports fail harmlessly.
after(async () => {
  try { (await import("../../backend/redis.js")).redis.disconnect(); } catch { /* not loaded */ }
  try { await (await import("../../backend/db.js")).pool.end(); } catch { /* not loaded */ }
});
import {
  redactGatewayPayload, stringifyRedacted, rawEventMode, _KEEP, _DENY,
} from "../../backend/payment_privacy.mjs";

/** All 42 `data` keys observed in production, plus the real envelope. */
function productionShapedPayload() {
  return {
    business_id: "biz_x", type: "payment.succeeded", timestamp: "2026-08-01T00:00:00Z",
    data: {
      payload_type: "Payment", payment_id: "pay_x", business_id: "biz_x", brand_id: "brand_x",
      subscription_id: "sub_x", checkout_session_id: "cs_x", invoice_id: "inv_x",
      currency: "USD", total_amount: 19999, tax: 0,
      settlement_amount: 19999, settlement_currency: "USD", settlement_tax: 0,
      status: "succeeded", refund_status: null, retry_attempt: 0,
      created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-01T00:00:01Z",
      digital_products_delivered: true, is_update_payment_method: false,
      payment_method: "card", payment_method_type: "credit", payment_provider: "stripe",
      card_network: "visa", card_type: "credit", card_issuing_country: "ID",
      discount_id: null, discounts: [], refunds: [], disputes: [],
      product_cart: [{ product_id: "prod_x", quantity: 1 }],
      error_code: null,

      // ── everything below must not survive ──
      card_holder_name: "RINO YUFAHRI",
      card_last_four: "4242",
      payment_method_id: "pm_secret_token",
      invoice_url: "https://example.test/invoice/eyJhbGciOi.TOKEN",
      payment_link: "https://example.test/pay/TOKEN",
      error_message: "upstream said: key sk_live_abcdef rejected",
      metadata: { note: "anything at all" },
      custom_field_responses: [{ answer: "free text" }],
      customer: { customer_id: "cus_x", email: "someone@example.test", name: "Rino Yufahri" },
      billing: { country: "ID", city: "Jakarta", street: "Jl. Example 1", zipcode: "12345" },
    },
  };
}

const MUST_NOT_SURVIVE = [
  "card_holder_name", "card_last_four", "payment_method_id", "invoice_url", "payment_link",
  "error_message", "metadata", "custom_field_responses", "email", "name",
  "city", "street", "zipcode", "is_update_payment_method",
];

// Financial and tax evidence. Dropping any of these would trade a privacy problem for an
// accounting one — the 10-year bookkeeping obligation applies to exactly this data.
const MUST_SURVIVE = [
  "payment_id", "subscription_id", "invoice_id", "checkout_session_id",
  "currency", "total_amount", "tax", "settlement_amount", "settlement_currency",
  "settlement_tax", "status", "refunds", "disputes", "discounts", "product_cart",
  "created_at", "payment_method", "card_network", "card_issuing_country", "country",
  "customer_id",
];

function keysDeep(o, acc = new Set()) {
  if (Array.isArray(o)) { o.forEach((v) => keysDeep(v, acc)); return acc; }
  if (o && typeof o === "object") {
    for (const [k, v] of Object.entries(o)) { acc.add(k); keysDeep(v, acc); }
  }
  return acc;
}

test("strips every identity and instrument field from the real payload shape", () => {
  const out = redactGatewayPayload(productionShapedPayload(), { mode: "redacted" });
  const present = keysDeep(out);
  for (const k of MUST_NOT_SURVIVE) {
    assert.equal(present.has(k), false, `${k} survived redaction`);
  }
});

test("keeps the financial and tax evidence", () => {
  const out = redactGatewayPayload(productionShapedPayload(), { mode: "redacted" });
  const present = keysDeep(out);
  for (const k of MUST_SURVIVE) {
    assert.equal(present.has(k), true, `${k} was dropped — that is accounting evidence`);
  }
  // Values, not just key presence: a shape check would pass on an empty object.
  assert.equal(out.data.total_amount, 19999);
  assert.equal(out.data.settlement_currency, "USD");
  assert.equal(out.data.customer.customer_id, "cus_x");
  assert.equal(out.data.billing.country, "ID");
});

test("records what it dropped, by name only, never by value", () => {
  const out = redactGatewayPayload(productionShapedPayload(), { mode: "redacted" });
  assert.equal(out._redacted.mode, "redacted");
  for (const k of ["card_holder_name", "card_last_four", "invoice_url", "email"]) {
    assert.ok(out._redacted.dropped.includes(k), `${k} missing from the dropped list`);
  }
  // The provenance record must not leak the values it is describing.
  const serialised = JSON.stringify(out);
  for (const v of ["RINO YUFAHRI", "4242", "someone@example.test", "sk_live_abcdef", "Jl. Example 1"]) {
    assert.equal(serialised.includes(v), false, `redacted payload still contains ${v}`);
  }
});

test("nested identity data is dropped wherever it appears, not only at the top", () => {
  const out = redactGatewayPayload({
    data: { refunds: [{ refund_id: "r1", customer: { email: "deep@example.test" } }] },
  });
  assert.equal(JSON.stringify(out).includes("deep@example.test"), false);
  assert.equal(out.data.refunds[0].refund_id, "r1");
});

test("DENY beats KEEP and beats the operator escape hatch", () => {
  // PAYMENT_RAW_EVENT_EXTRA_KEEP widens the allowlist without a deploy. It must not be able
  // to re-open a field that is denied outright — otherwise one env var undoes the control.
  const out = redactGatewayPayload(productionShapedPayload(), {
    mode: "redacted",
    extraKeep: ["card_last_four", "card_holder_name", "email", "invoice_url"],
  });
  const present = keysDeep(out);
  for (const k of ["card_last_four", "card_holder_name", "email", "invoice_url"]) {
    assert.equal(present.has(k), false, `${k} was re-admitted through extraKeep`);
  }
  assert.equal([..._DENY].some((d) => _KEEP.has(d)), false, "a denied name is also allowlisted");
});

test("the escape hatch does work for a field that is merely unknown", () => {
  const withNew = productionShapedPayload();
  withNew.data.some_new_provider_field = "keep me";
  const without = redactGatewayPayload(withNew, { mode: "redacted", extraKeep: [] });
  assert.equal(without.data.some_new_provider_field, undefined);
  const with_ = redactGatewayPayload(withNew, { mode: "redacted", extraKeep: ["some_new_provider_field"] });
  assert.equal(with_.data.some_new_provider_field, "keep me");
});

test("never mutates the caller's payload", () => {
  const original = productionShapedPayload();
  const before = JSON.stringify(original);
  redactGatewayPayload(original, { mode: "redacted" });
  assert.equal(JSON.stringify(original), before, "the input object was modified in place");
});

test("mode=full is a passthrough, and redacted is the DEFAULT", () => {
  const p = productionShapedPayload();
  assert.equal(redactGatewayPayload(p, { mode: "full" }), p);

  const prev = process.env.PAYMENT_RAW_EVENT_MODE;
  delete process.env.PAYMENT_RAW_EVENT_MODE;
  try {
    // The audit criticised FIX_F24_STRIPE_IDEM_TXN for shipping the fix behind a
    // default-off flag. This control must not repeat that: unset means protected.
    assert.equal(rawEventMode(), "redacted");
    assert.equal(JSON.parse(stringifyRedacted(p)).data.card_holder_name, undefined);
  } finally {
    if (prev === undefined) delete process.env.PAYMENT_RAW_EVENT_MODE;
    else process.env.PAYMENT_RAW_EVENT_MODE = prev;
  }
});

test("handles the shapes that would otherwise throw", () => {
  assert.deepEqual(redactGatewayPayload(null), {});
  assert.deepEqual(redactGatewayPayload(undefined), {});
  assert.equal(typeof stringifyRedacted({}), "string");
  assert.equal(redactGatewayPayload({ data: { status: null } }).data.status, null);
});

// ─────────────────────────────────────────────────────────────────────────────
// Binding the WRITE PATH, not just the function.
//
// Everything above proves the redactor redacts. None of it proves payments_core actually
// calls it — a unit test on a helper stays green while the production path bypasses the
// helper entirely. Two layers, because each covers the other's blind spot: the source check
// always runs (so there is no infra-free state where nothing is verified), and the
// round-trip check proves the real INSERT really stores a redacted payload.
// ─────────────────────────────────────────────────────────────────────────────

test("every gateway-payload write site routes through the redactor", async () => {
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../../backend/payments_core.mjs", import.meta.url), "utf8");

  const bypass = src.match(/JSON\.stringify\(\s*rawEvent/g) || [];
  assert.equal(bypass.length, 0,
    `payments_core writes a raw gateway payload directly ${bypass.length} time(s)`);

  // Five sites were wired: grant_entitlement, recordPaymentEvent,
  // recordCreditedPaymentEvent, the orphan_reversals queue, and the reversal_events merge.
  const wired = src.match(/stringifyRedacted\(/g) || [];
  assert.ok(wired.length >= 5,
    `expected at least 5 redacted write sites, found ${wired.length}`);
});

test("recordPaymentEvent stores a payload with the PII already gone", {
  skip: process.env.DATABASE_POOL_URL_DEV ? false : "no DATABASE_POOL_URL_DEV",
}, async () => {
  const { recordPaymentEvent } = await import("../../backend/payments_core.mjs");
  const { query } = await import("../../backend/db.js");

  const tenantId = "44444444-4444-4444-8444-444444444444";
  await query(
    `INSERT INTO tenants (id,name,slug,email,plan) VALUES ($1,'T-priv','t-priv-${Date.now()}','priv@x.test','ultra')
     ON CONFLICT (id) DO NOTHING`, [tenantId]);

  const idem = `privacy_roundtrip_${Date.now()}`;
  await recordPaymentEvent({
    tenantId, userId: null, provider: "dodo", idempotencyKey: idem,
    providerPaymentId: "pay_rt", planKey: "ultra", amount: 19999, currency: "USD",
    status: "succeeded", rawEvent: productionShapedPayload(),
  });

  const { rows } = await query(
    `SELECT raw_event FROM payment_events WHERE idempotency_key = $1`, [idem], tenantId);
  assert.equal(rows.length, 1, "the event row was not written");

  const stored = JSON.stringify(rows[0].raw_event);
  for (const v of ["RINO YUFAHRI", "4242", "someone@example.test", "sk_live_abcdef", "Jl. Example 1"]) {
    assert.equal(stored.includes(v), false, `stored payload still contains ${v}`);
  }
  // And the evidence really did survive the round trip — a row of {} would pass the checks
  // above while destroying the accounting record.
  assert.equal(rows[0].raw_event.data.total_amount, 19999);
  assert.equal(rows[0].raw_event.data.settlement_currency, "USD");
  assert.ok(rows[0].raw_event._redacted.dropped.includes("card_holder_name"));
});
