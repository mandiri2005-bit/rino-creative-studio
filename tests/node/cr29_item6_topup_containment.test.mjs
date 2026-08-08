// cr29_item6_topup_containment.test.mjs — MATRIX-045 T80.
// CR-29 / Item 6 TEMPORARY containment on the top-up checkout call site
// (PLAN §S10.G3.1-h). Pure logic: the Dodo client is injected, so no network and
// no SDK install. Run from backend/:
//   node --test ../tests/node/cr29_item6_topup_containment.test.mjs
import { test, after } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";

process.env.DATABASE_POOL_URL_DEV ??= "postgres://u:p@localhost:5432/db";
process.env.REDIS_URL ??= "redis://localhost:6379";
process.env.DODO_PRODUCT_BOOST10 = "prod_boost10";
process.env.DODO_PRODUCT_BOOST50 = "prod_boost50";
process.env.DODO_PRODUCT_BOOST100 = "prod_boost100";

const sub = await import("../../backend/dodo_subscriptions.mjs");
const { redis } = await import("../../backend/redis.js");
after(() => { try { redis.disconnect(); } catch {} });

const PACK = sub.VALID_TOPUP_PACKS[0];
const HERE = path.dirname(fileURLToPath(import.meta.url));
const BACKEND = path.join(HERE, "..", "..", "backend");

// Record every create() call plus everything written to console.error.
function harness(impl) {
  const calls = [];
  const alerts = [];
  const realError = console.error;
  console.error = (...a) => alerts.push(a.join(" "));
  const client = {
    checkoutSessions: {
      create: async (body, options) => { calls.push({ body, options }); return impl(calls.length); },
    },
  };
  return {
    client, calls, alerts,
    restore: () => { console.error = realError; },
    parsedAlerts: () => alerts.map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean),
  };
}

const run = (h) => sub._createTopupWithClient({ client: h.client, tenantId: "t-1", userId: "u-1", packKey: PACK });

// ── 1. NON-VACUITY + normal success ──────────────────────────────────────────
// If this fails, every failure-path assertion below is meaningless: a suite that
// only exercises failures must NOT be able to report PASS.
test("NON-VACUITY: a normal successful create still returns a usable checkout URL", async () => {
  const h = harness(() => ({ session_id: "cs_live_1", checkout_url: "https://checkout.dodo/x" }));
  try {
    const out = await run(h);
    assert.equal(out.checkoutUrl, "https://checkout.dodo/x", "top-up must still work");
    assert.equal(h.calls.length, 1);
    assert.equal(h.parsedAlerts().length, 0, "the happy path must raise no alert");
  } finally { h.restore(); }
});

// ── 2. per-request maxRetries=0 ──────────────────────────────────────────────
test("the create call passes { maxRetries: 0 } per request", async () => {
  const h = harness(() => ({ session_id: "cs_1", checkout_url: "https://c/1" }));
  try {
    await run(h);
    assert.deepEqual(h.calls[0].options, { maxRetries: 0 });
  } finally { h.restore(); }
});

// ── 3. the GLOBAL client must keep its default retries ───────────────────────
test("dodo.mjs does NOT disable retries on the shared client", () => {
  const src = fs.readFileSync(path.join(BACKEND, "dodo.mjs"), "utf8");
  const ctor = src.slice(src.indexOf("new DodoPayments("));
  assert.ok(!/maxRetries/.test(ctor.slice(0, 200)),
    "the global client must keep SDK default retries — the wide change was explicitly rejected");
});

// ── 4. thrown / timeout after dispatch ⇒ UNKNOWN, and 7. no second call ──────
test("a throw after dispatch is checkout_status_unknown, alerts, and is NEVER retried", async () => {
  const h = harness(() => { const e = new Error("socket hang up"); e.name = "APIConnectionTimeoutError"; throw e; });
  try {
    await assert.rejects(run(h), /^Error: checkout_status_unknown$/);
    assert.equal(h.calls.length, 1, "exactly one provider call — a second create is the defect this exists to stop");
    const a = h.parsedAlerts().find((x) => x.evt === "topup_checkout_status_unknown");
    assert.ok(a, "a structured alert must be emitted");
    assert.equal(a.tenant_id, "t-1");
    assert.equal(a.pack_key, PACK);
    assert.ok(a.requested_at, "alert must carry the request time");
  } finally { h.restore(); }
});

// ── 5. session_id but no checkout_url — alert carries the DIGEST, never the raw id ──
test("2xx with session_id and no checkout_url ⇒ created_without_checkout_url, alert carries session_id_sha256", async () => {
  const RAW = "cs_orphan_9";
  const EXPECTED = createHash("sha256").update(RAW, "utf8").digest("hex");
  const h = harness(() => ({ session_id: RAW, checkout_url: null, payment_id: "pay_secret", }));
  try {
    await assert.rejects(run(h), /^Error: created_without_checkout_url$/);
    assert.equal(h.calls.length, 1, "must not create another session");
    const a = h.parsedAlerts().find((x) => x.evt === "topup_created_without_checkout_url");
    assert.ok(a, "alert required — a payable session exists that the customer cannot reach");
    assert.equal(a.session_id_sha256, EXPECTED, "digest must be SHA-256 of the raw session id");
    assert.equal(a.session_id, undefined, "the RAW session id must never be logged");
  } finally { h.restore(); }
});

// ── 5b. PRIVACY: no raw provider identifier may appear anywhere in the logs ──
test("PRIVACY: raw session_id, checkout_url and payment_id never appear in any alert line", async () => {
  const cases = [
    () => ({ session_id: "cs_raw_leak", checkout_url: null, payment_id: "pay_leak" }),
    () => ({ session_id: "cs_raw_leak", checkout_url: "   ", payment_id: "pay_leak" }),
    () => { const e = new Error("cs_raw_leak leaked via message"); e.name = "APIConnectionError"; throw e; },
    () => ({ checkout_url: "https://checkout.dodo/leak", payment_id: "pay_leak" }), // anomaly path
  ];
  for (const impl of cases) {
    const h = harness(impl);
    try {
      await run(h).catch(() => {});
      const blob = h.alerts.join("\n");
      assert.ok(!blob.includes("cs_raw_leak"), `raw session id leaked: ${blob}`);
      assert.ok(!blob.includes("pay_leak"), `payment_id leaked: ${blob}`);
      assert.ok(!/checkout\.dodo/.test(blob), `checkout_url leaked: ${blob}`);
    } finally { h.restore(); }
  }
});

// ── 6. malformed 2xx ─────────────────────────────────────────────────────────
test("2xx without session_id ⇒ checkout_protocol_anomaly, fail closed, no retry", async () => {
  for (const bad of [{}, { session_id: "" }, { session_id: "   ", checkout_url: "https://c/x" }, null]) {
    const h = harness(() => bad);
    try {
      await assert.rejects(run(h), /^Error: checkout_protocol_anomaly$/, `payload: ${JSON.stringify(bad)}`);
      assert.equal(h.calls.length, 1);
      assert.ok(h.parsedAlerts().some((x) => x.evt === "topup_checkout_protocol_anomaly"));
    } finally { h.restore(); }
  }
});

// ── 8. no session_id leakage to the customer ─────────────────────────────────
test("no thrown error carries a session_id, and the HTTP mapping never returns one", async () => {
  const h = harness(() => ({ session_id: "cs_secret_42", checkout_url: "" }));
  try {
    await run(h);
    assert.fail("should have thrown");
  } catch (e) {
    assert.equal(e.message, "created_without_checkout_url");
    assert.ok(!/cs_secret_42/.test(e.message), "the session id must not ride out on the error");
    assert.ok(!/cs_secret_42/.test(h.alerts.join("\n")), "nor may it appear in the log line");
  } finally { h.restore(); }

  const server = fs.readFileSync(path.join(BACKEND, "server.js"), "utf8");
  const i = server.indexOf('error: "checkout_unavailable"');
  assert.ok(i > 0, "the checkout_unavailable mapping must exist");
  const branch = server.slice(i - 400, i + 400);
  assert.ok(!/session_id/.test(branch), "the customer-facing branch must not mention session_id");
});

// ── 9. the dead payment_link fallback is gone ────────────────────────────────
test("the session.payment_link fallback is removed (it could never fire)", () => {
  const src = fs.readFileSync(path.join(BACKEND, "dodo_subscriptions.mjs"), "utf8");
  assert.ok(!/session\.payment_link/.test(src),
    "CheckoutSessionResponse has no payment_link field — the fallback was dead code");
});

// ── pre-dispatch validation stays DEFINITIVE ─────────────────────────────────
test("pre-dispatch validation failures stay definitive and never alert", async () => {
  const h = harness(() => ({ session_id: "cs_1", checkout_url: "https://c/1" }));
  try {
    await assert.rejects(
      sub._createTopupWithClient({ client: null, tenantId: "t", userId: "u", packKey: PACK }),
      /^Error: dodo_not_configured$/);
    await assert.rejects(
      sub._createTopupWithClient({ client: h.client, tenantId: "t", userId: "u", packKey: "nope" }),
      /^Error: unknown_pack$/);
    assert.equal(h.calls.length, 0, "no provider call may be dispatched");
    assert.equal(h.parsedAlerts().length, 0, "definitive failures are not unknown-alerts");
  } finally { h.restore(); }
});
