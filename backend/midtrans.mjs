// ============================================================================
// midtrans.mjs — Midtrans (IDR rail: QRIS / VA / GoPay) via the hosted Snap flow.
//
// The second NEW rail. Grants credits through the SAME shared core
// payments_core.grant_entitlement() that Dodo uses. Stripe (billing.mjs) is
// untouched. Snap only — NOT Core API / BI-SNAP.
//
// Env-gated: with no MIDTRANS_SERVER_KEY the module loads fine and endpoints
// return {error:"midtrans_not_configured"}. Sandbox vs production is a single
// independent toggle (MIDTRANS_IS_PRODUCTION), separate from Dodo's.
//
// SECURITY — webhook identity:
//   Midtrans signs only SHA512(order_id + status_code + gross_amount + ServerKey).
//   custom_fields are NOT signed, so we never trust echoed identity. Instead we
//   write a PENDING row at create-transaction time (signed order_id → tenant),
//   and the webhook resolves the tenant from the SIGNED order_id via the
//   SECURITY DEFINER fn payment_event_lookup(). order_id is unguessable.
//
// Required env (per environment — sandbox and production keys DIFFER):
//   MIDTRANS_IS_PRODUCTION   'true' | 'false'   (false => Sandbox)
//   MIDTRANS_SERVER_KEY      sandbox/production Server Key
//   MIDTRANS_CLIENT_KEY      sandbox/production Client Key (frontend loads snap.js)
//   MIDTRANS_PRICE_STARTER/PRO/STUDIO  plan price in IDR (optional; sane defaults)
//   APP_BASE_URL             for the Snap "finish" callback URL
// ============================================================================
import midtransClient from "midtrans-client";
import crypto from "node:crypto";
import { query } from "./db.js";
import { grant_entitlement, recordPaymentEvent, creditsForPlan, VALID_PLAN_KEYS } from "./payments_core.mjs";

const SERVER_KEY = process.env.MIDTRANS_SERVER_KEY || "";
const CLIENT_KEY = process.env.MIDTRANS_CLIENT_KEY || "";
const IS_PRODUCTION = process.env.MIDTRANS_IS_PRODUCTION === "true"; // default false => Sandbox

export const snap = SERVER_KEY
  ? new midtransClient.Snap({ isProduction: IS_PRODUCTION, serverKey: SERVER_KEY, clientKey: CLIENT_KEY })
  : null;

export function isConfigured() { return !!snap; }
// The frontend needs the client key + env to load the correct snap.js.
export function publicConfig() {
  return { clientKey: CLIENT_KEY, isProduction: IS_PRODUCTION, configured: isConfigured() };
}

// Plan → price in IDR (whole rupiah). Server-authoritative. Env-overridable.
// Defaults mirror python credit_catalog.TIER_PRICE_IDR (starter/pro/enterprise).
const PLAN_PRICE_IDR = {
  starter: Number(process.env.MIDTRANS_PRICE_STARTER || 79000),
  pro:     Number(process.env.MIDTRANS_PRICE_PRO     || 199000),
  studio:  Number(process.env.MIDTRANS_PRICE_STUDIO  || 499000),
};
export function priceForPlan(planKey) { return PLAN_PRICE_IDR[planKey] || 0; }

const APP_BASE_URL = (process.env.APP_BASE_URL || "").replace(/\/+$/, "");

// order_id: unique + unguessable; carries NO trusted identity (tenant is resolved
// from this signed value via payment_event_lookup). Kept under Midtrans' 50-char limit.
export function _newOrderId(tenantId) {
  const short = String(tenantId).replace(/-/g, "").slice(0, 8);
  const rand = crypto.randomBytes(6).toString("hex");
  return `ceritai-${short}-${Date.now().toString(36)}-${rand}`;
}

// ── Public: create a Snap transaction for a plan (Clerk-authed route) ─────────
// Writes the PENDING payment_events row FIRST (the signed order_id→tenant map),
// then asks Snap for a token. Grants nothing here — the webhook does that.
export async function createTransaction({ tenantId, userId, planKey, email }) {
  if (!snap) throw new Error("midtrans_not_configured");
  const gross = priceForPlan(planKey);
  if (!VALID_PLAN_KEYS.includes(planKey) || gross <= 0) throw new Error("unknown_plan");
  const orderId = _newOrderId(tenantId);

  // Durable pending row BEFORE calling Snap — so the webhook can always resolve who/what.
  await recordPaymentEvent({
    provider: "midtrans", idempotencyKey: orderId, tenantId,
    userId: userId || null, planKey, amount: gross, currency: "IDR",
    status: "pending", rawEvent: { stage: "create-transaction" },
  });

  const parameter = {
    transaction_details: { order_id: orderId, gross_amount: gross },
    item_details: [{ id: planKey, price: gross, quantity: 1,
                     name: `Ceritai ${planKey} (${creditsForPlan(planKey)} kredit)` }],
    credit_card: { secure: true },
    ...(email ? { customer_details: { email: String(email) } } : {}),
    ...(APP_BASE_URL ? { callbacks: { finish: `${APP_BASE_URL}/index.html?billing=success&rail=midtrans` } } : {}),
  };
  const tx = await snap.createTransaction(parameter);
  return { token: tx.token, redirectUrl: tx.redirect_url, orderId };
}

// ── Verify a notification: SHA512(order_id+status_code+gross_amount+ServerKey) ─
// Timing-safe compare against the body's signature_key. Returns boolean.
export function verifyNotification(body) {
  if (!SERVER_KEY) return false;
  const orderId = body?.order_id, statusCode = body?.status_code;
  const gross = body?.gross_amount, sig = body?.signature_key;
  if (!orderId || !statusCode || gross == null || !sig) return false;
  const expected = crypto.createHash("sha512")
    .update(`${orderId}${statusCode}${gross}${SERVER_KEY}`)
    .digest("hex");
  const a = Buffer.from(expected, "utf8");
  const b = Buffer.from(String(sig), "utf8");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

// Midtrans transaction_status → our payment_events.status (non-grant states).
const STATUS_MAP = {
  pending: "pending", authorize: "pending",
  deny: "failed", failure: "failed",
  cancel: "cancelled", expire: "expired",
  refund: "refunded", partial_refund: "refunded",
  chargeback: "disputed", partial_chargeback: "disputed",
};

// ── Handle a verified notification, idempotently via the shared core ──────────
// Grant ONLY on settlement, or capture+fraud_status=accept (cards).
export async function handleNotification(body) {
  const orderId = body?.order_id;
  if (!orderId) return { handled: false, reason: "no_order_id" };
  const txStatus = body?.transaction_status;
  const fraud = body?.fraud_status;

  // Resolve the owning tenant from the SIGNED order_id (webhook has no tenant ctx).
  const look = await query(
    `SELECT tenant_id, user_id, plan_key, credited, status FROM payment_event_lookup($1,$2)`,
    ["midtrans", orderId],
  );
  const row = look.rows[0];
  if (!row) {
    console.warn(`[midtrans] notification for unknown order_id=${orderId}`);
    return { handled: false, reason: "unknown_order" };
  }

  const grantWorthy = txStatus === "settlement" || (txStatus === "capture" && fraud === "accept");
  if (grantWorthy) {
    const res = await grant_entitlement({
      provider: "midtrans", idempotencyKey: orderId,
      tenantId: row.tenant_id, userId: row.user_id, planKey: row.plan_key,
      amount: Number(body.gross_amount) || null, currency: body.currency || "IDR",
      providerPaymentId: body.transaction_id || null, rawEvent: body,
    });
    return { handled: true, granted: true, ...res };
  }

  // Non-grant states → update status only (never touches a credited row).
  const mapped = STATUS_MAP[txStatus] || "pending";
  await recordPaymentEvent({
    provider: "midtrans", idempotencyKey: orderId, tenantId: row.tenant_id,
    userId: row.user_id, planKey: row.plan_key, amount: Number(body.gross_amount) || null,
    currency: body.currency || "IDR", providerPaymentId: body.transaction_id || null,
    status: mapped, rawEvent: body,
  });
  return { handled: true, granted: false, status: mapped };
}
