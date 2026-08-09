// cr29_item4_dodo_metadata.test.mjs — the CR-27 assent-metadata contract.
//
// CR-27 (OWNER-DECISION-CR27) authorises EXACTLY TWO inert metadata fields on the
// top-up checkout: `tos_version` and `acceptance_event_id`. They are carried
// unmodified through the frozen webhook → anchor → ledger path so a purchase's
// governing version stays pinned.
//
// 🔴 `acceptance_event_seq` is LOCAL ORDERING and must NEVER leave the database.
// Sending it would be a third metadata field, which CR-27 does not authorise, and
// it is the specific mistake these tests exist to make impossible to ship quietly.
//
// Pure logic: the Dodo client is injected, so no network and no SDK install.
//   node --test ../tests/node/cr29_item4_dodo_metadata.test.mjs
import { test, after } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
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
const BACKEND = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "backend");
const VERSION = "v2026.08-id";
const EVENT_ID = "3f1b0c2e-9a4d-4c77-8b21-0e5d6a7c8f90";
const EVENT_SEQ = 4242;

function capture(impl = () => ({ session_id: "cs_1", checkout_url: "https://c/1" })) {
  const calls = [];
  return {
    calls,
    client: { checkoutSessions: { create: async (body, options) => { calls.push({ body, options }); return impl(); } } },
  };
}

const run = (h, assentPins) => sub._createTopupWithClient({
  client: h.client, tenantId: "t-1", userId: "u-1", packKey: PACK, assentPins,
});

// ── the authorised pair IS sent ──────────────────────────────────────────────
test("both authorised fields reach the provider metadata, byte-for-byte", async () => {
  const h = capture();
  await run(h, { tos_version: VERSION, acceptance_event_id: EVENT_ID });
  const md = h.calls[0].body.metadata;
  assert.equal(md.tos_version, VERSION);
  assert.equal(md.acceptance_event_id, EVENT_ID);
});

// ── NON-VACUITY: the pre-existing metadata is untouched ──────────────────────
test("NON-VACUITY: the existing metadata contract is preserved", async () => {
  const h = capture();
  await run(h, { tos_version: VERSION, acceptance_event_id: EVENT_ID });
  const md = h.calls[0].body.metadata;
  assert.equal(md.kind, "topup", "the webhook discriminator must survive");
  assert.equal(md.tenant_id, "t-1");
  assert.equal(md.pack_key, PACK);
});

// ── the whole point: event_seq must NEVER be sent ────────────────────────────
test("acceptance_event_seq is NEVER sent, even when handed to the call", async () => {
  const h = capture();
  await run(h, { tos_version: VERSION, acceptance_event_id: EVENT_ID, acceptance_event_seq: EVENT_SEQ });
  const md = h.calls[0].body.metadata;
  assert.equal(md.acceptance_event_seq, undefined, "event_seq is local ordering; CR-27 forbids a third field");
  assert.ok(!JSON.stringify(md).includes(String(EVENT_SEQ)),
    "the sequence value must not appear anywhere in the metadata payload");
});

// ── an unauthorised key cannot ride along, whatever the caller passes ────────
test("any key outside the authorised pair is dropped", async () => {
  const h = capture();
  await run(h, {
    tos_version: VERSION, acceptance_event_id: EVENT_ID,
    acceptance_event_seq: EVENT_SEQ, determination_rule: "explicit_user_locale",
    artifact_sha256: "a".repeat(64), serving_deployment_id: "deploy-1", locale: "id",
  });
  const md = h.calls[0].body.metadata;
  for (const k of ["acceptance_event_seq", "determination_rule", "artifact_sha256",
                   "serving_deployment_id", "locale"]) {
    assert.equal(md[k], undefined, `${k} must not be forwarded to the provider`);
  }
});

// ── the exported allow-list is the contract, and it is exactly two ───────────
test("ASSENT_METADATA_KEYS is exactly the two CR-27 authorises", () => {
  assert.deepEqual([...sub.ASSENT_METADATA_KEYS].sort(), ["acceptance_event_id", "tos_version"]);
  assert.ok(Object.isFrozen(sub.ASSENT_METADATA_KEYS), "the allow-list must not be mutable at runtime");
});

test("source contains no path that would forward acceptance_event_seq", () => {
  const src = fs.readFileSync(path.join(BACKEND, "dodo_subscriptions.mjs"), "utf8");
  const md = src.slice(src.indexOf("_assentMetadata"), src.indexOf("}, { maxRetries: 0 })"));
  assert.ok(!/acceptance_event_seq\s*[:,]/.test(md),
    "no metadata path may name acceptance_event_seq as a field");
});

// ── shadow mode sends nothing extra ──────────────────────────────────────────
test("shadow-mode intents (no pins) add no assent metadata at all", async () => {
  for (const pins of [null, undefined, {}, { tos_version: null, acceptance_event_id: null }]) {
    const h = capture();
    await run(h, pins);
    const md = h.calls[0].body.metadata;
    assert.equal(md.tos_version, undefined, `pins=${JSON.stringify(pins)}`);
    assert.equal(md.acceptance_event_id, undefined, `pins=${JSON.stringify(pins)}`);
    assert.equal(md.kind, "topup", "the base metadata still ships");
  }
});

test("an empty-string pin is omitted, not sent as evidence of a pin", async () => {
  const h = capture();
  await run(h, { tos_version: "", acceptance_event_id: "" });
  const md = h.calls[0].body.metadata;
  assert.equal(md.tos_version, undefined, "'' would read as a pinned version that does not exist");
  assert.equal(md.acceptance_event_id, undefined);
});

test("a partial assent tuple fails definitively before provider dispatch", async () => {
  for (const pins of [
    { tos_version: VERSION },
    { acceptance_event_id: EVENT_ID },
    { tos_version: "   ", acceptance_event_id: EVENT_ID },
  ]) {
    const h = capture();
    await assert.rejects(run(h, pins), /topup_assent_pin_incomplete/);
    assert.equal(h.calls.length, 0, "an incomplete evidence tuple must never reach Dodo");
  }
});

// ── item 6's containment is untouched by this change ─────────────────────────
test("item-6 containment survives: per-request maxRetries stays 0", async () => {
  const h = capture();
  await run(h, { tos_version: VERSION, acceptance_event_id: EVENT_ID });
  assert.deepEqual(h.calls[0].options, { maxRetries: 0 });
  assert.equal(h.calls.length, 1, "still exactly one provider call");
});
