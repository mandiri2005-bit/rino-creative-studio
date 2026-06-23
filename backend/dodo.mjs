// ============================================================================
// dodo.mjs — Dodo Payments (Merchant-of-Record / global card rail).
//
// One of two NEW rails (Dodo + Midtrans) that BOTH grant credits through the
// single shared core payments_core.grant_entitlement(). This module owns only
// the Dodo-specific bits: the SDK client, plan→product mapping, hosted-checkout
// creation, and Standard-Webhooks signature verification. Stripe (billing.mjs)
// is untouched.
//
// Env-gated: with no DODO_PAYMENTS_API_KEY the module loads fine and every
// endpoint returns {error:"dodo_not_configured"} so the app boots without Dodo.
//
// The webhook is the SINGLE SOURCE OF TRUTH for granting credits — never the
// frontend redirect. Identity is carried via checkout metadata (§4).
//
// Required env (per Dodo environment — test_ and live_ values DIFFER):
//   DODO_PAYMENTS_API_KEY        test_... / live_...
//   DODO_PAYMENTS_WEBHOOK_SECRET whsec_...  (Dashboard → Webhooks → signing secret)
//   DODO_ENVIRONMENT             'test_mode' | 'live_mode'  (must match the key type)
//   DODO_PRODUCT_ID_STARTER/PRO/STUDIO   product ids created in the Dodo dashboard
//   APP_BASE_URL                 e.g. https://app.ceritai.com  (checkout return URL)
//   DODO_CHECKOUT_RETURN_URL / DODO_CHECKOUT_CANCEL_URL  (optional explicit overrides)
// ============================================================================
import DodoPayments from "dodopayments";
import { grant_entitlement, recordPaymentEvent } from "./payments_core.mjs";

const API_KEY = process.env.DODO_PAYMENTS_API_KEY || "";
// Default to test_mode unless explicitly live — never accidentally go live.
const ENVIRONMENT = process.env.DODO_ENVIRONMENT === "live_mode" ? "live_mode" : "test_mode";
export const WEBHOOK_SECRET = process.env.DODO_PAYMENTS_WEBHOOK_SECRET || "";

export const dodo = API_KEY
  ? new DodoPayments({ bearerToken: API_KEY, webhookKey: WEBHOOK_SECRET || null, environment: ENVIRONMENT })
  : null;

export function isConfigured() { return !!dodo; }

// Kill switch (per-rail). Default ACTIVE when unset — a forgotten var must NOT
// cause an outage. Only an explicit "false" disables. Global PAYMENTS_ENABLED
// overrides. Enforced at the CREATE endpoint, never the webhook.
export function railEnabled() {
  return process.env.PAYMENTS_ENABLED !== "false" && process.env.DODO_ENABLED !== "false";
}

// Plan key → Dodo product id (per-environment; created in the dashboard).
const PLAN_PRODUCT = {
  starter: process.env.DODO_PRODUCT_ID_STARTER || "",
  pro:     process.env.DODO_PRODUCT_ID_PRO || "",
  studio:  process.env.DODO_PRODUCT_ID_STUDIO || "",
};
/** Dodo product id for a plan key, or null if unknown / unconfigured. */
export function productForPlan(planKey) {
  return PLAN_PRODUCT[planKey] || null;
}

// Checkout return URLs — default to the same ?billing= flow the studio handles,
// optionally absolute via APP_BASE_URL.
const APP_BASE_URL = (process.env.APP_BASE_URL || "").replace(/\/+$/, "");
const RETURN_URL = process.env.DODO_CHECKOUT_RETURN_URL
  || `${APP_BASE_URL}/index.html?billing=success&rail=dodo`;
const CANCEL_URL = process.env.DODO_CHECKOUT_CANCEL_URL
  || `${APP_BASE_URL}/index.html?billing=cancel&rail=dodo`;

// ── Public: create a hosted Checkout for a plan ───────────────────────────────
// Server maps plan_key → product_id and is the sole authority on what it grants.
// Tenant/user are carried end-to-end via checkout metadata so the webhook knows
// who to credit without trusting the payer's email (§4).
export async function createCheckout({ tenantId, userId, planKey }) {
  if (!dodo) throw new Error("dodo_not_configured");
  const productId = productForPlan(planKey);
  if (!productId) throw new Error("unknown_plan");
  const session = await dodo.checkoutSessions.create({
    product_cart: [{ product_id: productId, quantity: 1 }],
    return_url: RETURN_URL,
    cancel_url: CANCEL_URL,
    metadata: {
      tenant_id: String(tenantId),
      user_id: userId ? String(userId) : "",
      plan_key: String(planKey),
    },
  });
  return session.checkout_url;
}

// ── ±5-minute replay window on the `webhook-timestamp` header (§6.3) ──────────
// Belt-and-suspenders: the SDK also enforces timestamp tolerance during verify.
const REPLAY_WINDOW_SEC = 5 * 60;
export function _freshTimestamp(tsHeader, nowSec = Math.floor(Date.now() / 1000)) {
  const ts = Number(tsHeader);
  if (!Number.isFinite(ts)) return false;
  return Math.abs(nowSec - ts) <= REPLAY_WINDOW_SEC;
}

// ── Public: verify a raw webhook body via the SDK (Standard Webhooks) ─────────
// MUST receive the RAW request body string. Throws on bad/missing signature →
// the route returns 403. We never hand-roll HMAC.
export function verifyEvent(rawBody, headers) {
  if (!dodo) throw new Error("dodo_not_configured");
  return dodo.webhooks.unwrap(rawBody, { headers }); // { business_id, type, timestamp, data }
}

// ── Public: handle a verified Dodo event, idempotently via the shared core ────
// webhookId = the `webhook-id` header (the idempotency key).
export async function handleEvent({ payload, webhookId, rawEvent }) {
  const type = payload?.type || "";
  const data = payload?.data || {};
  const md = data.metadata || {};
  const tenantId = md.tenant_id || null;
  const amount = Number.isFinite(data.total_amount) ? data.total_amount : null;

  if (type === "payment.succeeded") {
    if (!tenantId) {
      console.warn(`[dodo] payment.succeeded without tenant_id metadata id=${webhookId}`);
      return { handled: false, reason: "no_tenant" };
    }
    const res = await grant_entitlement({
      provider: "dodo",
      idempotencyKey: webhookId,            // dual-key: also payment_events UNIQUE gate
      tenantId,
      userId: md.user_id || null,
      planKey: md.plan_key || null,
      amount,
      currency: data.currency || null,
      providerPaymentId: data.payment_id || null,
      rawEvent,
    });
    return { handled: true, ...res };
  }

  if (type === "payment.failed") {
    if (tenantId) {
      await recordPaymentEvent({
        provider: "dodo", idempotencyKey: webhookId, tenantId,
        userId: md.user_id || null, planKey: md.plan_key || null,
        providerPaymentId: data.payment_id || null, amount,
        currency: data.currency || null, status: "failed", rawEvent,
      });
    }
    return { handled: true, status: "failed" };
  }

  // refund.* / dispute.* / subscription.* / unknown → acknowledge + log.
  // Credit reversal on refunds and subscription lifecycle are deferred (out of
  // scope); the Dodo dashboard is the source of truth meanwhile. 200 prevents
  // pointless retries for events we don't act on.
  console.log(`[dodo] recorded (no-op) event=${type} id=${webhookId} payment=${data.payment_id || "?"}`);
  return { handled: true, recorded: true, type };
}
