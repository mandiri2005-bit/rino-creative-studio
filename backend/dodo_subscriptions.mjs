// ============================================================================
// dodo_subscriptions.mjs — GLOBAL deployment recurring subscriptions (Dodo).
//
// Active ONLY when BILLING_MODE=subscription (the global product). The Indonesia
// one-time deployment (BILLING_MODE=one_time, Midtrans + grant_entitlement) never
// touches this module. Credits use RESET (use-it-or-lose-it) via
// payments_core.reset_entitlement — NOT the additive grant_entitlement.
//
// Design (verified against docs.dodopayments.com, not the spec's assumed names):
//   • SDK: `dodopayments` (default import), client init in dodo.mjs (shared).
//   • Create:   dodo.subscriptions.create({ product_id, quantity, customer, billing,
//               payment_link:true, return_url, metadata }) → { subscription_id, payment_link }.
//   • Portal:   dodo.customers.customerPortal.create(customerId, {return_url}) → { link }.
//   • Webhook:  dodo.webhooks.unwrap (via dodo.verifyEvent); envelope { type, data },
//               data.payload_type 'Subscription'|'Payment'.
//   • Period fields are previous_billing_date / next_billing_date (NOT
//     current_period_*); cancel flag is cancel_at_next_billing_date.
//   • Status set: pending|active|on_hold|cancelled|expired|failed (NO past_due).
//   • Dunning has NO discrete events — failure surfaces as on_hold; recovery as
//     active/renewed. A dunning-exhausted sub may sit in on_hold with no terminal
//     event → reconcileStuckSubscriptions() is the cron fallback (Rino's call).
//
// Idempotency: credit reset op_id is period-keyed
//   `dodo_sub:{subscription_id}:{periodEndEpoch}` → exactly one reset per period
// (replayed renewal = NO-OP). plan_changed carries the new plan in its op_id so it
// still applies within an already-reset period. expired/downgrade keys on :expired.
// ============================================================================
import fs from "fs";
import path from "path";
import crypto from "node:crypto";
import { fileURLToPath } from "url";
import { dodo, isConfigured, railEnabled, verifyEvent } from "./dodo.mjs";
import { reset_entitlement, topup_grant, mirrorCreditDelta } from "./payments_core.mjs";
import { query } from "./db.js";

// ── Billing mode (deployment switch) ──────────────────────────────────────────
// Subscription logic is INERT unless explicitly enabled. Default = one_time so a
// missing var on the Indonesia deployment can never accidentally arm subscriptions.
export function subscriptionMode() { return process.env.BILLING_MODE === "subscription"; }

// Fail-loud guard: when BILLING_MODE=subscription (global product), the config that
// actually LOADED must carry the global markers. If not, the loader silently fell
// back to the Indonesia config (config/pricing.json) → $0.01 economics + empty
// by-res map → ~80% silent margin leak. Refuse to start instead. Indonesia
// (BILLING_MODE != subscription) is a no-op. Presence-only checks (NO hardcoded
// $0.002) — these three keys are absent / Indonesia-default in config/pricing.json,
// so they cleanly detect a fallback. Call once at server startup (before listen).
// (_loadPricing is a hoisted function declaration below.)
export function assertGlobalConfigLoaded() {
  if (!subscriptionMode()) return;
  const cfg = _loadPricing();
  const missing = [];
  const sp = cfg.subscription_plans;
  if (!sp || typeof sp !== "object" || Object.keys(sp).length === 0) missing.push("subscription_plans (absent/empty)");
  const byres = cfg.video_usd_per_sec_by_res;
  if (!byres || typeof byres !== "object" || Object.keys(byres).length === 0) missing.push("video_usd_per_sec_by_res (absent/empty)");
  const cuv = cfg.credit_usd_value;
  if (cuv == null || Number(cuv) === 0.01) missing.push("credit_usd_value (absent or ==0.01 Indonesia default)");
  if (missing.length) {
    throw new Error(
      "GLOBAL CONFIG NOT LOADED — PRICING_CONFIG_JSON stale/unset, fallback ke ekonomi Indonesia. " +
      "Refusing start. Missing/invalid global config keys: " + missing.join("; ") +
      ". Fix: set PRICING_CONFIG_JSON (or PRICING_CONFIG_PATH) on this service to the current config/pricing.global.example.json."
    );
  }
}

// ── Global plan config (config-driven; defaults = the locked USD/monthly table) ──
// Overridable via pricing.json `subscription_plans` (or PRICING_CONFIG_JSON env) so
// the global deployment re-prices without a code change. credits are the per-period
// RESET target; product_env names the env var holding the Dodo product id.
const _DEFAULT_SUB_PLANS = {
  free:    { credits: 500,    price_usd: 0,      product_env: null },
  starter: { credits: 5000,   price_usd: 9.99,   product_env: "DODO_PRODUCT_STARTER" },
  plus:    { credits: 20000,  price_usd: 39.99,  product_env: "DODO_PRODUCT_PLUS" },
  pro:     { credits: 50000,  price_usd: 99.99,  product_env: "DODO_PRODUCT_PRO" },
  ultra:   { credits: 100000, price_usd: 199.99, product_env: "DODO_PRODUCT_ULTRA" },
};
function _loadPricing() {
  const raw = process.env.PRICING_CONFIG_JSON;
  if (raw) { try { return JSON.parse(raw) || {}; } catch { return {}; } }
  const here = path.dirname(fileURLToPath(import.meta.url));
  const candidates = [
    process.env.PRICING_CONFIG_PATH,
    path.join(here, "..", "config", "pricing.json"),
    "/app/config/pricing.json",
    path.join(process.cwd(), "config", "pricing.json"),
  ];
  for (const p of candidates) {
    try { if (p && fs.existsSync(p)) return JSON.parse(fs.readFileSync(p, "utf8")) || {}; } catch { /* ignore */ }
  }
  return {};
}
export const SUBSCRIPTION_PLANS = { ..._DEFAULT_SUB_PLANS, ...(_loadPricing().subscription_plans || {}) };
// Paid plans only (client may subscribe to these; 'free' is grant-only, never bought).
export const VALID_SUB_PLANS = Object.keys(SUBSCRIPTION_PLANS).filter((k) => k !== "free");

// ── Top-up packs (ONE-TIME purchase; config-driven, defaults = locked table) ──
// Top-up ADDs to the topup bucket (does NOT change tier access). credits + USD price
// + the env var holding the Dodo one-time product id. Bigger pack = better rate.
const _DEFAULT_TOPUP_PACKS = {
  boost_10:  { credits: 5000,  price_usd: 10,  product_env: "DODO_PRODUCT_BOOST10" },
  boost_50:  { credits: 28000, price_usd: 50,  product_env: "DODO_PRODUCT_BOOST50" },
  boost_100: { credits: 60000, price_usd: 100, product_env: "DODO_PRODUCT_BOOST100" },
};
export const TOPUP_PACKS = { ..._DEFAULT_TOPUP_PACKS, ...(_loadPricing().topup_packs || {}) };
export const VALID_TOPUP_PACKS = Object.keys(TOPUP_PACKS);
export function isTopupPack(packKey) { return Object.prototype.hasOwnProperty.call(TOPUP_PACKS, packKey); }
export function topupPackCredits(packKey) { return TOPUP_PACKS[packKey]?.credits ?? 0; }
export function topupProductId(packKey) {
  const envName = TOPUP_PACKS[packKey]?.product_env;
  return envName ? (process.env[envName] || null) : null;
}

export function isSubPlan(planKey) { return Object.prototype.hasOwnProperty.call(SUBSCRIPTION_PLANS, planKey); }
/** Per-period RESET target (credits) for a plan key. */
export function subPlanCredits(planKey) { return SUBSCRIPTION_PLANS[planKey]?.credits ?? 0; }
/** Dodo product id for a plan key (from its configured env var), or null. */
export function subProductId(planKey) {
  const envName = SUBSCRIPTION_PLANS[planKey]?.product_env;
  return envName ? (process.env[envName] || null) : null;
}
/** Reverse map: Dodo product id → plan key (for plan_changed where metadata is stale). */
export function planForProduct(productId) {
  if (!productId) return null;
  for (const k of VALID_SUB_PLANS) { if (subProductId(k) === productId) return k; }
  return null;
}

const APP_BASE_URL = (process.env.APP_BASE_URL || "").replace(/\/+$/, "");
const SUB_RETURN_URL = process.env.DODO_SUB_RETURN_URL
  || `${APP_BASE_URL}/index.html?billing=success&rail=dodo_sub`;

// ── TASK 3: create a subscription → hosted checkout payment_link ──────────────
// Server is authoritative on plan→product. Tenant/user/plan ride in metadata so
// the webhook can resolve who to credit. Returns the payment_link to redirect to.
export async function createSubscription({ tenantId, userId, planKey, email, name, country }) {
  if (!dodo) throw new Error("dodo_not_configured");
  if (planKey === "free" || !VALID_SUB_PLANS.includes(planKey)) throw new Error("unknown_plan");
  const productId = subProductId(planKey);
  if (!productId) throw new Error("unknown_plan");           // product env not set for this plan

  const customer = email ? { email: String(email), ...(name ? { name: String(name) } : {}) } : undefined;
  if (!customer) throw new Error("missing_email");           // Dodo needs a customer to bill

  const sub = await dodo.subscriptions.create({
    product_id: productId,
    quantity: 1,
    customer,
    billing: { country: String(country || "US") },          // hosted checkout collects the rest
    payment_link: true,                                       // → response carries payment_link
    return_url: SUB_RETURN_URL,
    metadata: {
      tenant_id: String(tenantId),
      user_id: userId ? String(userId) : "",
      plan_key: String(planKey),
    },
  });
  // Seed a pending row so the webhook can always find the tenant by subscription_id —
  // even if Dodo's webhook payload doesn't propagate the metadata we set above.
  const subId = sub.subscription_id || null;
  if (subId) {
    await query(
      `INSERT INTO dodo_subscriptions
         (tenant_id, user_id, plan_key, dodo_subscription_id, dodo_customer_id, status)
       VALUES ($1,$2,$3,$4,$5,'pending')
       ON CONFLICT (dodo_subscription_id) DO NOTHING`,
      [tenantId, userId || null, planKey, subId, sub.customer?.customer_id || null],
      tenantId,
    ).catch(err => console.warn("[subscription/create] pending row seed failed:", err.message));
  }
  return {
    paymentLink: sub.payment_link || null,
    subscriptionId: subId,
    customerId: sub.customer?.customer_id || null,
  };
}

// ── TASK 3b: create a ONE-TIME top-up checkout (NOT a subscription) ───────────
// Hosted one-time checkout for a boost pack. metadata.kind='topup' so the
// payment.succeeded webhook routes to handleTopupPayment (not a package grant).
export async function createTopup({ tenantId, userId, packKey }) {
  if (!dodo) throw new Error("dodo_not_configured");
  if (!VALID_TOPUP_PACKS.includes(packKey)) throw new Error("unknown_pack");
  const productId = topupProductId(packKey);
  if (!productId) throw new Error("unknown_pack");                  // product env not set
  const session = await dodo.checkoutSessions.create({
    product_cart: [{ product_id: productId, quantity: 1 }],
    return_url: SUB_RETURN_URL,
    metadata: {
      tenant_id: String(tenantId),
      user_id: userId ? String(userId) : "",
      pack_key: String(packKey),
      kind: "topup",                                               // ← webhook discriminator
    },
  });
  return { checkoutUrl: session.checkout_url || session.payment_link || null };
}

// Top-up expiry = the renewal AFTER the imminent one (current_period_end + 1 cadence
// → min ~30 days, uniform per cycle). Top-up is PAID-ONLY (Free is rejected at
// /topup/create), so every buyer has a renewal cycle — there is NO Free branch.
// Returns an ISO string. (Plain Date math — backend code, not a workflow script.)
const _MS_30D = 30 * 24 * 3600 * 1000;
async function _computeTopupExpiry(tenantId) {
  const r = await query(
    `SELECT current_period_end FROM dodo_subscriptions
      WHERE tenant_id=$1 AND current_period_end IS NOT NULL
      ORDER BY (status IN ('active','on_hold')) DESC, current_period_end DESC LIMIT 1`,
    [tenantId], tenantId,
  );
  const cpe = r.rows[0]?.current_period_end;
  // expiry = max(period_end, now) + 30d. The now() floor stops a STALE period_end
  // (e.g. a stuck on_hold sub whose next_billing_date is in the past) from shortening
  // the top-up below the ~30-day guarantee; +30d (not setMonth) avoids JS month-
  // overflow. Matches the spec example (period ends 30 Jun → expiry 30 Jul).
  if (!cpe) console.warn(`[dodo_topup] no subscription period for tenant=${tenantId} — using now()+30d`);
  const base = cpe ? new Date(cpe).getTime() : Date.now();
  return new Date(Math.max(base, Date.now()) + _MS_30D).toISOString();
}

// ── Period key (idempotency): epoch-seconds of the period END (next_billing_date) ─
// Unique per period; replayed events for the same period collapse to one reset.
function _periodKey(data) {
  const boundary = data?.next_billing_date || data?.previous_billing_date || null;
  if (!boundary) return null;
  const t = Date.parse(boundary);
  return Number.isFinite(t) ? String(Math.floor(t / 1000)) : String(boundary);
}
function _toTs(v) { const t = v ? Date.parse(v) : NaN; return Number.isFinite(t) ? new Date(t).toISOString() : null; }

// Resolve the owning tenant for an unauthenticated webhook: prefer event metadata,
// else the SECURITY-DEFINER lookup keyed on the unguessable subscription_id.
async function _resolveTenant(data) {
  const md = data?.metadata || {};
  if (md.tenant_id) return { tenantId: md.tenant_id, userId: md.user_id || null, planKey: md.plan_key || null };
  const subId = data?.subscription_id;
  if (!subId) return { tenantId: null, userId: null, planKey: null };
  // metadata missing — fall back to DB lookup (seeded at checkout creation time).
  console.warn(`[dodo_sub] no metadata.tenant_id for sub=${subId}; metadata_keys=${JSON.stringify(Object.keys(md))}`);
  const r = await query(`SELECT tenant_id, user_id, plan_key, status FROM dodo_subscription_lookup($1)`, [subId]);
  const row = r.rows[0];
  if (row) console.log(`[dodo_sub] resolved tenant from DB: t=${row.tenant_id} plan=${row.plan_key}`);
  return row
    ? { tenantId: row.tenant_id, userId: row.user_id, planKey: row.plan_key }
    : { tenantId: null, userId: null, planKey: null };
}

// Idempotent upsert of the subscription row (RLS-FORCE → query() sets tenant ctx).
async function _upsertSub({ tenantId, userId, planKey, subId, customerId, status, periodStart, periodEnd, cancelAtEnd }) {
  await query(
    `INSERT INTO dodo_subscriptions
       (tenant_id, user_id, plan_key, dodo_subscription_id, dodo_customer_id, status,
        current_period_start, current_period_end, cancel_at_period_end)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
     ON CONFLICT (dodo_subscription_id) DO UPDATE SET
        plan_key             = COALESCE(EXCLUDED.plan_key, dodo_subscriptions.plan_key),
        user_id              = COALESCE(EXCLUDED.user_id, dodo_subscriptions.user_id),
        dodo_customer_id     = COALESCE(EXCLUDED.dodo_customer_id, dodo_subscriptions.dodo_customer_id),
        status               = EXCLUDED.status,
        current_period_start = COALESCE(EXCLUDED.current_period_start, dodo_subscriptions.current_period_start),
        current_period_end   = COALESCE(EXCLUDED.current_period_end, dodo_subscriptions.current_period_end),
        cancel_at_period_end = EXCLUDED.cancel_at_period_end,
        updated_at           = now()`,
    [tenantId, userId, planKey, subId, customerId, status, periodStart, periodEnd, !!cancelAtEnd],
    tenantId,
  );
}

// Mirror the active plan onto tenants.plan — the tier source the spend-gate reads
// (python _tier_for → ensure_tier → tier_at_least). Active/renewed/plan_changed set
// the paid tier so the user can reach that tier's models; expiry sets 'free'. on_hold
// and cancelled intentionally leave the plan untouched (access persists during dunning
// / until period end). With config-driven TIER_RANK, the plan name IS the tier name.
async function _setTenantPlan(tenantId, plan) {
  if (!tenantId || !plan) return;
  try {
    const r = await query(`UPDATE tenants SET plan=$2 WHERE id=$1`, [tenantId, plan], tenantId);
    if (r.rowCount === 0) console.error(`[dodo_sub] tenants.plan NOT updated (no row) t=${tenantId} plan=${plan}`);
  } catch (e) {
    // Loud, not swallowed: a CHECK violation here (e.g. an unmapped plan name) means
    // the paying user's tier never updates → wrong model gating. Surfacing it as an
    // error makes that fail visibly instead of silently locking the customer out.
    console.error(`[dodo_sub] tenants.plan update FAILED t=${tenantId} plan=${plan}: ${e.message}`);
  }
}

// ── TASK 4: handle a verified subscription event (the INTI) ───────────────────
// Called from the /dodo/webhook route for any `subscription.*` event. Returns a
// small result object; NEVER throws on a benign no-op. Credits move ONLY through
// reset_entitlement (idempotent per period). Kill switch is NOT checked here —
// in-flight subscriptions must keep settling (the spec's hard rule).
export async function handleSubscriptionEvent({ payload, webhookId, rawEvent }) {
  const type = payload?.type || "";
  const data = payload?.data || {};
  const subId = data?.subscription_id || null;
  if (!subId) return { handled: false, reason: "no_subscription_id", type };

  const { tenantId, userId, planKey: metaPlan } = await _resolveTenant(data);
  if (!tenantId) {
    // active should always carry our metadata; a renewal before the active row
    // exists is the only way here, which shouldn't happen. Ack to stop retries.
    console.warn(`[dodo_sub] ${type} sub=${subId} unresolved tenant id=${webhookId}`);
    return { handled: false, reason: "no_tenant", type };
  }

  // The authoritative plan for this event: a plan change names a new product;
  // otherwise use our stored/metadata plan.
  const eventPlan = planForProduct(data?.product_id) || metaPlan || null;
  const status = data?.status || null;
  const periodStart = _toTs(data?.previous_billing_date);
  const periodEnd = _toTs(data?.next_billing_date);
  const cancelAtEnd = data?.cancel_at_next_billing_date === true;
  const periodKey = _periodKey(data);

  const doReset = async (targetPlan, opId, reason) => {
    const credits = subPlanCredits(targetPlan);
    return reset_entitlement({
      userId, tenantId, targetCredits: credits, opId, reason,
      meta: { provider: "dodo_sub", subscription_id: subId, plan_key: targetPlan, webhook_id: webhookId, event: type },
    });
  };

  switch (type) {
    case "subscription.active": {
      // First successful charge → record + grant the first period's credits (RESET).
      const plan = eventPlan || metaPlan;
      await _upsertSub({ tenantId, userId, planKey: plan, subId, customerId: data?.customer?.customer_id || null,
                         status: status || "active", periodStart, periodEnd, cancelAtEnd });
      await _setTenantPlan(tenantId, plan);
      let reset = null;
      if (plan && periodKey) reset = await doReset(plan, `dodo_sub:${subId}:${periodKey}`, "monthly_grant");
      return { handled: true, type, action: "active", plan, reset };
    }
    case "subscription.renewed": {
      // New billing cycle → refresh credits to the plan amount (use-it-or-lose-it).
      const plan = eventPlan || metaPlan;
      await _upsertSub({ tenantId, userId, planKey: plan, subId, customerId: data?.customer?.customer_id || null,
                         status: status || "active", periodStart, periodEnd, cancelAtEnd });
      await _setTenantPlan(tenantId, plan);
      let reset = null;
      if (plan && periodKey) reset = await doReset(plan, `dodo_sub:${subId}:${periodKey}`, "monthly_grant");
      return { handled: true, type, action: "renewed", plan, reset };
    }
    case "subscription.plan_changed": {
      // Upgrade/downgrade → set new plan + RESET credits to the NEW plan amount.
      // Distinct op_id (new plan + period) so it applies even if this period was
      // already reset by active/renewed. Dodo handles proration billing.
      const plan = eventPlan || metaPlan;
      await _upsertSub({ tenantId, userId, planKey: plan, subId, customerId: data?.customer?.customer_id || null,
                         status: status || "active", periodStart, periodEnd, cancelAtEnd });
      await _setTenantPlan(tenantId, plan);
      let reset = null;
      if (plan) reset = await doReset(plan, `dodo_sub:${subId}:planchg:${periodKey || "na"}:${plan}`, "monthly_grant");
      return { handled: true, type, action: "plan_changed", plan, reset };
    }
    case "subscription.on_hold": {
      // Renewal payment failing → dunning in progress. Do NOT reset, do NOT cut
      // access; Dodo is retrying. reconcileStuckSubscriptions() is the safety net
      // for a sub that never leaves on_hold.
      await _upsertSub({ tenantId, userId, planKey: eventPlan, subId, customerId: data?.customer?.customer_id || null,
                         status: "on_hold", periodStart, periodEnd, cancelAtEnd });
      return { handled: true, type, action: "on_hold" };
    }
    case "subscription.cancelled": {
      // Cancellation requested → access + credits remain until period end (paid for).
      // No downgrade now; the terminal subscription.expired does that.
      await _upsertSub({ tenantId, userId, planKey: eventPlan, subId, customerId: data?.customer?.customer_id || null,
                         status: "cancelled", periodStart, periodEnd, cancelAtEnd: true });
      return { handled: true, type, action: "cancelled" };
    }
    case "subscription.expired": {
      // TERMINAL → downgrade to Free: plan=free, credits → 0 (Free 500 is signup-only,
      // never re-granted). Access falls back to the Free tier (cheap models) elsewhere.
      await _upsertSub({ tenantId, userId, planKey: "free", subId, customerId: data?.customer?.customer_id || null,
                         status: "expired", periodStart, periodEnd, cancelAtEnd });
      await _setTenantPlan(tenantId, "free");                  // downgrade tier source → Free models only
      const reset = await reset_entitlement({
        userId, tenantId, targetCredits: 0, opId: `dodo_sub:${subId}:expired`, reason: "lapse",
        meta: { provider: "dodo_sub", subscription_id: subId, plan_key: "free", webhook_id: webhookId, event: type },
      });
      return { handled: true, type, action: "expired", reset };
    }
    case "subscription.failed": {
      // Initial mandate setup failed (terminal, pre-activation). Record; no credits.
      await _upsertSub({ tenantId, userId, planKey: eventPlan, subId, customerId: data?.customer?.customer_id || null,
                         status: "failed", periodStart, periodEnd, cancelAtEnd });
      return { handled: true, type, action: "failed" };
    }
    case "subscription.updated":
    default: {
      // Catch-all field sync (status/period/cancel flag). No credit movement.
      await _upsertSub({ tenantId, userId, planKey: eventPlan, subId, customerId: data?.customer?.customer_id || null,
                         status: status || "active", periodStart, periodEnd, cancelAtEnd });
      return { handled: true, type, action: "updated" };
    }
  }
}

// ── TASK 2/3 (UI): the tenant's current subscription state ────────────────────
// Read-only view for the Account page + the post-checkout return poll. Returns the
// latest dodo_subscriptions row (status/plan/period/cancel flag). Credit balance +
// sub/topup breakdown come separately from /credits/balance. has_subscription=false
// for a Free / never-subscribed tenant.
export async function getSubscriptionStatus({ tenantId }) {
  // Prefer the LIVE subscription, not merely the most-recently-touched row: a tenant
  // can hold several rows (plan changes, or — pre-launch — repeated Subscribe clicks).
  // A later subscription.cancelled webhook bumps a dead row's updated_at, so ordering
  // by updated_at alone would surface a cancelled plan. Rank active/on_hold first,
  // then the furthest period end (the most current cycle), then recency.
  const r = await query(
    `SELECT plan_key, status, current_period_start, current_period_end, cancel_at_period_end,
            dodo_customer_id IS NOT NULL AS has_customer
       FROM dodo_subscriptions
      WHERE tenant_id=$1
      ORDER BY (status IN ('active','on_hold')) DESC,
               current_period_end DESC NULLS LAST,
               updated_at DESC
      LIMIT 1`,
    [tenantId], tenantId,
  );
  const row = r.rows[0] || null;
  return {
    has_subscription: !!row,
    plan_key: row?.plan_key || null,
    status: row?.status || null,
    current_period_start: row?.current_period_start || null,
    current_period_end: row?.current_period_end || null,
    cancel_at_period_end: row?.cancel_at_period_end || false,
    has_customer: row?.has_customer || false,
  };
}

// ── TASK 5: hosted Customer Portal (cancel / update card / invoices) ──────────
// Resolves the tenant's Dodo customer id, then asks Dodo for a portal session link.
// No self-built manage UI — the frontend just redirects to this link.
export async function customerPortal({ tenantId }) {
  if (!dodo) throw new Error("dodo_not_configured");
  const r = await query(
    `SELECT dodo_customer_id FROM dodo_subscriptions
      WHERE tenant_id=$1 AND dodo_customer_id IS NOT NULL
      ORDER BY updated_at DESC LIMIT 1`,
    [tenantId], tenantId,
  );
  const customerId = r.rows[0]?.dodo_customer_id || null;
  if (!customerId) throw new Error("no_customer");
  const session = await dodo.customers.customerPortal.create(customerId,
    SUB_RETURN_URL ? { return_url: SUB_RETURN_URL } : {});
  return { link: session.link || null };
}

// ── Cron fallback: downgrade subs Dodo left stuck (Rino's decision) ───────────
// Dodo does NOT guarantee a terminal event after dunning exhaustion — a sub can sit
// in on_hold forever. This sweeps on_hold/cancelled subs whose period end is well
// past, verifies the live status via the API, and downgrades the truly-dead ones to
// Free (credits → 0). Idempotent (the expired op_id + ON CONFLICT upsert). Intended
// to run daily; safe to run more often. Returns a summary for logging.
export async function reconcileStuckSubscriptions({ graceDays = 3, limit = 200 } = {}) {
  if (!subscriptionMode()) return { skipped: "not_subscription_mode" };
  if (!isConfigured()) return { skipped: "dodo_not_configured" };
  const cutoffMs = Date.now() - graceDays * 24 * 60 * 60 * 1000;
  // Cross-tenant scan via the SECURITY-DEFINER fn (the table is FORCE-RLS so a plain
  // app_user query would see nothing). The fn only reads; the per-row downgrade below
  // re-enters per-tenant (RLS-scoped) through _upsertSub/reset_entitlement.
  const r = await query(
    `SELECT tenant_id, user_id, dodo_subscription_id, current_period_end
       FROM dodo_subscriptions_due_for_reconcile($1, $2)`,
    [Math.floor(cutoffMs / 1000), limit],
  );
  let downgraded = 0, checked = 0;
  for (const row of r.rows) {
    checked++;
    try {
      const live = await dodo.subscriptions.retrieve(row.dodo_subscription_id);
      const liveStatus = live?.status || null;
      // Only downgrade if Dodo agrees the sub is no longer paying. retrieve() returns
      // one of pending|active|on_hold|cancelled|failed|expired ('renewed' is a webhook
      // type, never a status). Skip active (paying) and pending (mid-reactivation).
      if (liveStatus === "active" || liveStatus === "pending") continue;
      await _upsertSub({
        tenantId: row.tenant_id, userId: row.user_id, planKey: "free",
        subId: row.dodo_subscription_id, customerId: null, status: "expired",
        periodStart: null, periodEnd: null, cancelAtEnd: true,
      });
      await _setTenantPlan(row.tenant_id, "free");
      await reset_entitlement({
        userId: row.user_id, tenantId: row.tenant_id, targetCredits: 0,
        opId: `dodo_sub:${row.dodo_subscription_id}:reconcile_expired`, reason: "lapse",
        meta: { provider: "dodo_sub", subscription_id: row.dodo_subscription_id, source: "reconcile", live_status: liveStatus },
      });
      downgraded++;
    } catch (e) {
      console.warn(`[dodo_sub] reconcile failed sub=${row.dodo_subscription_id}: ${e.message}`);
    }
  }
  return { checked, downgraded, scanned: r.rows.length };
}

// ── TASK 4 (top-up branch): handle a ONE-TIME payment.succeeded with kind=topup ─
// Routed here from the /dodo/webhook dispatch (NOT a subscription event). ADDs the
// pack's credits to the topup bucket with the computed expiry. Idempotent on the
// webhook-id. Kill switch is NOT checked (in-flight payment must settle).
export async function handleTopupPayment({ payload, webhookId, rawEvent }) {
  const data = payload?.data || {};
  const md = data.metadata || {};
  const tenantId = md.tenant_id || null;
  const packKey = md.pack_key || null;
  if (!tenantId) { console.warn(`[dodo_topup] no tenant_id id=${webhookId}`); return { handled: false, reason: "no_tenant" }; }
  if (!isTopupPack(packKey)) { console.warn(`[dodo_topup] unknown pack=${packKey} id=${webhookId}`); return { handled: false, reason: "unknown_pack", packKey }; }
  const amount = topupPackCredits(packKey);
  if (!(amount > 0)) return { handled: false, reason: "zero_credits", packKey };
  const expiresAt = await _computeTopupExpiry(tenantId);
  const res = await topup_grant({
    userId: md.user_id || null, tenantId, amount,
    opId: `dodo_topup:${webhookId}`, expiresAt,
    meta: { provider: "dodo", pack_key: packKey, payment_id: data.payment_id || null, webhook_id: webhookId },
  });
  return { handled: true, action: "topup", packKey, amount, expiresAt, ...res };
}

// ── Top-up expiry sweep (cron, OPTIONAL drift-safety) ─────────────────────────
// Top-up is paid-only and the renewal webhook already sweeps a subscriber's expired
// top-up at RESET, so this is NOT required for the Free case (Free has no top-up).
// It remains a light daily safety net for two situations: (1) a user who DOWNGRADED
// to Free while a paid top-up was still alive (forfeit_topup=false keeps it) — when
// that top-up later passes its own expiry there's no renewal to sweep it; (2) general
// drift. The SECURITY-DEFINER fn forfeits expired topup_balance durably; here we
// mirror each forfeiture into the live Redis balance.
export async function sweepExpiredTopup({ limit = 500 } = {}) {
  const r = await query(
    `SELECT tenant_id, forfeited, new_balance FROM credit_sweep_expired_topup($1)`, [limit],
  );
  for (const row of r.rows) {
    await mirrorCreditDelta(row.tenant_id, -Number(row.forfeited || 0));   // total dropped by the forfeited topup
  }
  return { swept: r.rows.length, rows: r.rows };
}

// ── TASK 6: Free tier — once per email (lifetime, cross-account) + anti-abuse ──
// Free = subPlanCredits('free') credits, granted ONE time per verified email, ever.
// NO monthly refresh (no scheduler). Enforced by the GLOBAL free_grants guard +
// claim_free_grant (SECURITY DEFINER). Model gating (no Veo/Sora/4K on Free) reuses
// the existing python metering.ensure_tier — Free maps to tier rank 0.
const FREE_GRANT_CREDITS = Number(process.env.FREE_GRANT_CREDITS || subPlanCredits("free") || 500);

// Small built-in disposable-domain blocklist; extend via DISPOSABLE_EMAIL_DOMAINS
// (comma-separated). A bot-farm signup with a throwaway domain gets no Free credits.
const _DEFAULT_DISPOSABLE = [
  "mailinator.com","guerrillamail.com","10minutemail.com","tempmail.com","temp-mail.org",
  "trashmail.com","yopmail.com","getnada.com","sharklasers.com","throwawaymail.com",
  "maildrop.cc","dispostable.com","fakeinbox.com","mintemail.com","mohmal.com","emailondeck.com",
];
function _disposableSet() {
  const extra = (process.env.DISPOSABLE_EMAIL_DOMAINS || "")
    .split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  return new Set([..._DEFAULT_DISPOSABLE, ...extra]);
}
export function isDisposableDomain(domain) {
  if (!domain) return true;                        // no domain → treat as disposable (reject)
  return _disposableSet().has(String(domain).toLowerCase());
}

// Canonicalise + hash an email so dot/+tag Gmail variants collapse to ONE identity.
// Gmail-only canonicalisation (dots are insignificant ONLY on gmail/googlemail; for
// other providers a dot can be a different mailbox). Returns sha256(normalised) so
// no raw email is stored in the guard table (PII).
export function normalizeEmailHash(email) {
  const e = String(email || "").trim().toLowerCase();
  const at = e.lastIndexOf("@");
  if (at <= 0) return null;
  let domain = e.slice(at + 1);
  if (!domain) return null;
  let local = e.slice(0, at);
  if (domain === "gmail.com" || domain === "googlemail.com") {
    local = local.split("+")[0].replace(/\./g, "");
    domain = "gmail.com";                          // googlemail.com is an alias of gmail.com
  }
  return crypto.createHash("sha256").update(`${local}@${domain}`).digest("hex");
}

// Grant Free credits once per email (idempotent). Returns { status, balance }:
//   'granted' (first time) | 'already_claimed' | 'disposable_blocked' | 'invalid_email'.
export async function grantFreeOnce({ email, userId, tenantId }) {
  const at = String(email || "").lastIndexOf("@");
  const domain = at > 0 ? String(email).slice(at + 1).toLowerCase() : "";
  if (isDisposableDomain(domain)) return { status: "disposable_blocked", balance: 0 };
  const hash = normalizeEmailHash(email);
  if (!hash) return { status: "invalid_email", balance: 0 };
  const r = await query(
    `SELECT status, balance FROM claim_free_grant($1,$2,$3,$4,$5)`,
    [hash, userId || null, tenantId, domain, FREE_GRANT_CREDITS],
    tenantId,
  );
  return r.rows[0] || { status: "unknown", balance: 0 };
}
