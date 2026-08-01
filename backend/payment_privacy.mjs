// ─────────────────────────────────────────────────────────────────────────────
// payment_privacy.mjs — field-level allowlist for stored gateway payloads.
//
// Closes the technical half of audit finding F-06. Production was storing the FULL
// provider webhook body in payment_events.raw_event, payment_events.reversal_events and
// orphan_reversals.raw_event. Verified against the live row on 2026-08-01, the Dodo payload
// carries 42 keys under `data`, including card_holder_name, card_last_four, card_network,
// card_issuing_country, the customer object, the billing address, invoice_url and
// payment_link. That is cardholder and identity data, retained indefinitely, in a table the
// application role can read and (before 0070) modify.
//
// WHAT THIS DOES NOT DECIDE
//   Retention PERIOD, the legal basis, and what the privacy policy should promise are policy
//   questions and are deliberately not encoded here. This module only provides the
//   mechanism: keep the financial and tax evidence, drop the identity and instrument data,
//   and record what was dropped so the redaction is auditable rather than invisible.
//
// WHY AN ALLOWLIST AND NOT A DENYLIST
//   A denylist silently fails open the moment a provider adds a field. Dodo, Midtrans and
//   Stripe all nest differently (`data`, flat, `data.object`), and none of them promises a
//   stable schema. An allowlist keyed on field NAME survives all three shapes and fails
//   CLOSED on anything new: an unrecognised field is dropped and its name recorded, so the
//   loss is visible in `_redacted.dropped` and can be corrected deliberately.
//
//   The cost is real and worth stating: a genuinely useful new provider field is dropped
//   until someone adds it. PAYMENT_RAW_EVENT_EXTRA_KEEP exists so that does not require a
//   deploy, and `_redacted.dropped` is where you find out it happened.
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Field names retained in stored payloads.
 *
 * Matched by key NAME at any depth, so a PII field is dropped wherever it is nested. A key
 * that holds an object or array is walked, and the same rule applies inside it — which is
 * how `customer` survives as a container while the email and name inside it do not.
 */
const KEEP = new Set([
  // ── envelope / routing ──
  "type", "event_type", "payload_type", "timestamp", "data", "object",
  "business_id", "brand_id",

  // ── identifiers: needed to tie a payment to a payout, an invoice and a journal ──
  "id", "payment_id", "subscription_id", "invoice_id", "checkout_session_id",
  "order_id", "transaction_id", "customer_id", "product_id", "refund_id",

  // ── money: the whole point of keeping evidence at all ──
  "currency", "amount", "total_amount", "gross_amount", "net_amount",
  "settlement_amount", "settlement_currency", "settlement_time",
  "tax", "settlement_tax", "fee", "discount_id", "discount_amount", "quantity",

  // ── lifecycle / dispute trail ──
  "status", "transaction_status", "fraud_status", "refund_status", "dispute_status",
  "retry_attempt", "created_at", "updated_at", "transaction_time", "expiry_time",
  "digital_products_delivered",

  // ── containers that are walked, not kept wholesale ──
  "customer", "billing", "product_cart", "discounts", "refunds", "disputes", "items",

  // `metadata` is where WE echo our own linkage back through the gateway. Denying it
  // outright — as an earlier version of this file did — silently threw away tenant_id,
  // user_id and plan_key, i.e. the payload-side proof of which tenant a payment belonged
  // to. That only showed up when this was dry-run against the real production row. It is
  // walked instead: our own keys survive, and anything else a caller or provider stuffs in
  // there still drops, because the same allowlist applies one level down.
  "metadata", "tenant_id", "user_id",

  // ── instrument: category only, never anything that identifies the holder ──
  "payment_method", "payment_method_type", "payment_provider", "payment_type",
  "card_network", "card_type", "card_issuing_country", "bank",

  // ── jurisdiction: country drives the VAT/PPN decision, so it is tax evidence ──
  "country", "country_code",

  // ── plan ──
  "plan", "plan_key", "plan_id",

  // ── failure classification, but NOT the free-text message (see below) ──
  "error_code", "status_code",
]);

/**
 * Names dropped even if some future edit adds them to KEEP. A second, independent layer:
 * KEEP is a long list maintained by hand and one careless addition would re-open the hole,
 * so the fields that must never be stored are also denied explicitly.
 *
 * - card_holder_name / card_last_four / payment_method_id: cardholder and instrument data.
 * - invoice_url / payment_link / receipt_url: tokenised URLs — a bearer capability, and the
 *   audit calls them out by name.
 * - email / name / phone / address parts: plain identity data.
 * - custom_field_responses: free-form buyer input, so its contents cannot be reasoned about.
 *   (`metadata` is NOT denied — it is walked, because we put our own tenant/user/plan
 *   linkage in it. Unknown keys inside it still drop.)
 * - error_message: provider error text is echoed into the payload, and a separate finding
 *   in this codebase records that upstream error strings can carry secrets.
 */
const DENY = new Set([
  "card_holder_name", "card_last_four", "card_last4", "last4", "payment_method_id",
  "invoice_url", "payment_link", "receipt_url", "redirect_url", "pdf_url",
  "email", "email_address", "name", "full_name", "first_name", "last_name",
  "phone", "phone_number", "street", "address", "address_line1", "address_line2",
  "city", "state", "zipcode", "zip", "postal_code",
  "custom_field_responses", "error_message", "ip_address", "user_agent",
]);

function extraKeep() {
  return String(process.env.PAYMENT_RAW_EVENT_EXTRA_KEEP || "")
    .split(",").map((s) => s.trim()).filter(Boolean);
}

/** `redacted` (default) keeps only allowlisted fields; `full` restores the old behaviour. */
export function rawEventMode() {
  return process.env.PAYMENT_RAW_EVENT_MODE === "full" ? "full" : "redacted";
}

/**
 * Redact a gateway payload.
 *
 * @returns a NEW object; the input is never mutated (callers hold the original payload and
 *          may still need it for the branching that decides whether to grant credits).
 */
export function redactGatewayPayload(payload, opts = {}) {
  const mode = opts.mode || rawEventMode();
  if (payload === null || payload === undefined) return {};
  if (mode === "full") return payload;

  const keep = new Set(KEEP);
  for (const k of (opts.extraKeep || extraKeep())) keep.add(k);

  const dropped = new Set();

  const walk = (node) => {
    if (Array.isArray(node)) return node.map(walk);
    if (node === null || typeof node !== "object") return node;
    const out = {};
    for (const [k, v] of Object.entries(node)) {
      if (DENY.has(k) || !keep.has(k)) { dropped.add(k); continue; }
      out[k] = walk(v);
    }
    return out;
  };

  const result = walk(payload);
  // Provenance, so a reader can tell redaction ran and exactly what it removed — without
  // storing any of the removed values. Names only.
  result._redacted = {
    mode,
    at: new Date().toISOString(),
    dropped: [...dropped].sort(),
  };
  return result;
}

/** Drop-in replacement for `JSON.stringify(rawEvent)` at every payload write site. */
export function stringifyRedacted(payload, opts = {}) {
  return JSON.stringify(redactGatewayPayload(payload, opts));
}

// Exported for the guard tests, so the lists themselves are assertable rather than
// only their effect on one sample payload.
export const _KEEP = KEEP;
export const _DENY = DENY;
