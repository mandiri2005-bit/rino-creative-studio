// ============================================================================
// billing.mjs — Stripe checkout, customer portal, and idempotent webhooks.
//
// Env-gated like Sentry: with no STRIPE_SECRET_KEY the module loads fine and
// every endpoint returns {error:"stripe_not_configured"} instead of crashing,
// so the app boots in dev without Stripe. Crediting goes through the SAME
// durable layer as the Python side (credit_apply SQL fn + credit_ledger), with
// the Redis balance mirrored only when the cache already exists (so outstanding
// holds are never clobbered). Webhooks are idempotent via processed_stripe_events.
//
// Required env (set in Stripe Dashboard, then .env):
//   STRIPE_SECRET_KEY            sk_...
//   STRIPE_WEBHOOK_SECRET        whsec_... (from `stripe listen` or the dashboard)
//   STRIPE_PRICE_STARTER/PRO/STUDIO     subscription price ids (price_...)
//   STRIPE_PRICE_PACK_SMALL/MEDIUM/LARGE one-time credit-pack price ids
//   STRIPE_PACK_SMALL/MEDIUM/LARGE_CREDITS  credits granted per pack (default 1000/5000/15000)
//   STRIPE_CHECKOUT_SUCCESS_URL / _CANCEL_URL / STRIPE_PORTAL_RETURN_URL
// ============================================================================
import Stripe from "stripe";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { query, _uuid5 } from "./db.js";
import { redis } from "./redis.js";

const KEY = process.env.STRIPE_SECRET_KEY || "";
export const stripe = KEY ? new Stripe(KEY) : null;
export const WEBHOOK_SECRET = process.env.STRIPE_WEBHOOK_SECRET || "";

const URLS = {
  success: process.env.STRIPE_CHECKOUT_SUCCESS_URL || "/index.html?billing=success",
  cancel:  process.env.STRIPE_CHECKOUT_CANCEL_URL  || "/index.html?billing=cancel",
  portal:  process.env.STRIPE_PORTAL_RETURN_URL    || "/index.html",
};

// ── Pricing config (single source of truth shared with python/credit_catalog.py) ─
// config/pricing.json drives per-tier monthly credits. Precedence: PRICING_CONFIG_JSON
// env (inline JSON) → file at PRICING_CONFIG_PATH or a candidate path → {}. Any failure
// falls back to the hardcoded defaults, so behaviour is unchanged without the file.
const _DEFAULT_TIER_CREDITS = { free: 100, starter: 2500, pro: 9000, enterprise: 31200 };
function _loadPricing() {
  const raw = process.env.PRICING_CONFIG_JSON;
  if (raw) {
    try { return JSON.parse(raw) || {}; }
    catch (e) { console.warn("[billing] PRICING_CONFIG_JSON ignored:", e.message); return {}; }
  }
  const here = path.dirname(fileURLToPath(import.meta.url));
  const candidates = [
    process.env.PRICING_CONFIG_PATH,
    path.join(here, "..", "config", "pricing.json"),
    path.join(here, "config", "pricing.json"),
    "/app/config/pricing.json",
    path.join(process.cwd(), "config", "pricing.json"),
  ];
  for (const p of candidates) {
    try { if (p && fs.existsSync(p)) return JSON.parse(fs.readFileSync(p, "utf8")) || {}; }
    catch (e) { console.warn(`[billing] pricing.json at ${p} ignored:`, e.message); return {}; }
  }
  return {};
}
// Monthly free/tier allowance — config-driven, per-key fallback to the defaults.
// Mirrors python credit_catalog.TIER_MONTHLY_CREDITS (Studio→'enterprise').
const TIER_CREDITS = { ..._DEFAULT_TIER_CREDITS, ...(_loadPricing().tier_monthly_credits || {}) };

// ── Price catalog (built from env; entries with no price id are dropped) ──────
// Subscription credit allowances come from TIER_CREDITS above (Studio→'enterprise').
const _subs = [
  { key: "starter",    name: "Starter", plan: "starter",    credits: TIER_CREDITS.starter,    priceId: process.env.STRIPE_PRICE_STARTER },
  { key: "pro",        name: "Pro",     plan: "pro",        credits: TIER_CREDITS.pro,        priceId: process.env.STRIPE_PRICE_PRO },
  { key: "studio",     name: "Studio",  plan: "enterprise", credits: TIER_CREDITS.enterprise, priceId: process.env.STRIPE_PRICE_STUDIO },
];
const _packs = [
  { key: "pack_small",  name: "1,000 credits",  credits: Number(process.env.STRIPE_PACK_SMALL_CREDITS  || 1000),  priceId: process.env.STRIPE_PRICE_PACK_SMALL },
  { key: "pack_medium", name: "5,000 credits",  credits: Number(process.env.STRIPE_PACK_MEDIUM_CREDITS || 5000),  priceId: process.env.STRIPE_PRICE_PACK_MEDIUM },
  { key: "pack_large",  name: "15,000 credits", credits: Number(process.env.STRIPE_PACK_LARGE_CREDITS  || 15000), priceId: process.env.STRIPE_PRICE_PACK_LARGE },
];
export const TIER_CATALOG = _subs.filter(t => t.priceId);
export const PACK_CATALOG  = _packs.filter(p => p.priceId);

const SUB_BY_PRICE  = Object.fromEntries(TIER_CATALOG.map(t => [t.priceId, t]));
const PACK_BY_PRICE = Object.fromEntries(PACK_CATALOG.map(p => [p.priceId, p]));

export function isConfigured() { return !!stripe; }

// ── Durable credit + Redis mirror (mirrors python credits.grant/topup) ────────
const _INCR_IF_PRESENT =
  "if redis.call('EXISTS',KEYS[1])==1 then return redis.call('INCRBY',KEYS[1],ARGV[1]) else return -1 end";

export async function creditTenant(tenantId, amount, reason, opId, { userId = null, metadata = {} } = {}) {
  const res = await query(
    `SELECT applied, balance FROM credit_apply($1,$2,$3,$4,$5,$6::jsonb)`,
    [tenantId, userId, Math.trunc(amount), reason, opId, JSON.stringify(metadata)],
    tenantId,
  );
  const row = res.rows[0] || { applied: false, balance: 0 };
  if (row.applied) {
    try { await redis.eval(_INCR_IF_PRESENT, 1, `bal:${tenantId}:credits`, String(Math.trunc(amount))); }
    catch (e) { console.warn("[billing] redis mirror failed:", e.message); }
  }
  return row;   // { applied, balance }
}

// Stripe basil/dahlia (SDK v22) moved current_period_* OFF the Subscription and
// ONTO each SubscriptionItem. Read from the item, fall back to the old shape.
export function subPeriodStart(sub) {
  return sub?.items?.data?.[0]?.current_period_start ?? sub?.current_period_start ?? 0;
}
export function subPeriodEnd(sub) {
  return sub?.items?.data?.[0]?.current_period_end ?? sub?.current_period_end ?? 0;
}

// ── Subscription mirror (plan + period) into the subscriptions table ──────────
async function upsertSubscription(tenantId, sub, plan) {
  const priceId = sub.items?.data?.[0]?.price?.id || "";
  const productId = sub.items?.data?.[0]?.price?.product || "";
  await query(
    `INSERT INTO subscriptions
       (tenant_id, stripe_customer_id, stripe_subscription_id, stripe_price_id,
        stripe_product_id, plan, status, current_period_start, current_period_end)
     VALUES ($1,$2,$3,$4,$5,$6,$7, to_timestamp($8), to_timestamp($9))
     ON CONFLICT (stripe_subscription_id) DO UPDATE SET
        plan=EXCLUDED.plan, status=EXCLUDED.status, stripe_price_id=EXCLUDED.stripe_price_id,
        current_period_start=EXCLUDED.current_period_start,
        current_period_end=EXCLUDED.current_period_end, updated_at=now()`,
    [tenantId, String(sub.customer), String(sub.id), priceId, String(productId),
     plan, sub.status || "active", subPeriodStart(sub), subPeriodEnd(sub)],
    tenantId,
  );
  // keep tenants.plan in sync (drives tier-based features/retention)
  await query(`UPDATE tenants SET plan=$1 WHERE id=$2`, [plan, tenantId], tenantId);
}

// ── Public: create a Checkout Session for a tier or a credit pack ─────────────
export async function createCheckoutSession({ tenantId, userId, priceId, email }) {
  if (!stripe) throw new Error("stripe_not_configured");
  const sub  = SUB_BY_PRICE[priceId];
  const pack = PACK_BY_PRICE[priceId];
  if (!sub && !pack) throw new Error("unknown_price");
  const isSub = !!sub;
  const session = await stripe.checkout.sessions.create({
    mode: isSub ? "subscription" : "payment",
    line_items: [{ price: priceId, quantity: 1 }],
    success_url: URLS.success,
    cancel_url: URLS.cancel,
    client_reference_id: tenantId,
    ...(email ? { customer_email: email } : {}),
    metadata: {
      tenant_id: tenantId, user_id: userId || "",
      kind: isSub ? "subscription" : "pack",
      credits: String(isSub ? sub.credits : pack.credits),
      plan: isSub ? sub.plan : "",
    },
    ...(isSub ? { subscription_data: { metadata: { tenant_id: tenantId, plan: sub.plan } } } : {}),
  });
  return session.url;
}

// ── Public: Customer Portal (manage / cancel subscription) ────────────────────
export async function createPortalSession({ tenantId }) {
  if (!stripe) throw new Error("stripe_not_configured");
  const r = await query(
    `SELECT stripe_customer_id FROM subscriptions
      WHERE tenant_id=$1 AND stripe_customer_id NOT LIKE 'cus_free_%'
      ORDER BY created_at DESC LIMIT 1`, [tenantId], tenantId);
  const customer = r.rows[0]?.stripe_customer_id;
  if (!customer) throw new Error("no_customer");
  const session = await stripe.billingPortal.sessions.create({
    customer, return_url: URLS.portal,
  });
  return session.url;
}

// Lazily seed a tenant's tier allowance on first balance read, exactly like the
// Python side (credits._ensure_cached). op_id 'signup_grant' is shared with
// Python so it's idempotent — whichever path touches the balance first seeds it,
// the other is a no-op. Without this, the billing page showed 0 for a fresh user
// until a Python (chat) op happened to seed it → the 0-vs-99 desync.
async function seedIfEmpty(tenantId, plan) {
  const r = await query(`SELECT 1 FROM credit_balances WHERE tenant_id=$1`, [tenantId], tenantId);
  if (r.rows.length) return;
  await creditTenant(tenantId, TIER_CREDITS[plan] ?? TIER_CREDITS.free, "signup_grant",
    "signup_grant", { metadata: { seeded_by: "billing_status" } });
}

// ── Public: billing status for the UI (balance + plan + catalog) ──────────────
export async function getBillingStatus(tenantId) {
  const [sub, tenant] = await Promise.all([
    query(`SELECT plan,status,current_period_end FROM subscriptions
             WHERE tenant_id=$1 ORDER BY created_at DESC LIMIT 1`, [tenantId], tenantId),
    query(`SELECT plan FROM tenants WHERE id=$1`, [tenantId], tenantId),
  ]);
  const plan = sub.rows[0]?.plan || tenant.rows[0]?.plan || "free";
  await seedIfEmpty(tenantId, plan);
  const bal = await query(`SELECT balance FROM credit_balances WHERE tenant_id=$1`, [tenantId], tenantId);
  return {
    balance: Number(bal.rows[0]?.balance ?? 0),
    plan,
    status: sub.rows[0]?.status || "active",
    period_end: sub.rows[0]?.current_period_end || null,
    tiers: TIER_CATALOG.map(({ priceId, ...t }) => ({ ...t, priceId })),
    packs: PACK_CATALOG.map(({ priceId, ...p }) => ({ ...p, priceId })),
    configured: isConfigured(),
  };
}

// ── Public: handle a verified Stripe event, idempotently ──────────────────────
// Returns {handled, duplicate}. Crediting + subscription mirror happen here.
export async function handleStripeEvent(event) {
  // Idempotency gate: first writer wins; double-delivery is a no-op.
  const ins = await query(
    `INSERT INTO processed_stripe_events (stripe_event_id, event_type)
       VALUES ($1,$2) ON CONFLICT (stripe_event_id) DO NOTHING
     RETURNING stripe_event_id`,
    [event.id, event.type]);
  if (!ins.rows.length) return { handled: false, duplicate: true };

  if (event.type === "checkout.session.completed") {
    const s = event.data.object;
    const tenantId = s.metadata?.tenant_id || s.client_reference_id;
    const userId   = s.metadata?.user_id || null;
    if (!tenantId) return { handled: false, reason: "no_tenant" };

    if (s.mode === "payment") {                       // credit pack
      const credits = Number(s.metadata?.credits || 0);
      if (credits > 0) {
        await creditTenant(tenantId, credits, "topup", event.id,
          { userId, metadata: { stripe: "pack", session: s.id } });
      }
    } else if (s.mode === "subscription") {           // tier purchase
      const plan = s.metadata?.plan || "starter";
      if (s.subscription) {
        const sub = await stripe.subscriptions.retrieve(String(s.subscription));
        await upsertSubscription(tenantId, sub, plan);
        const credits = Number(s.metadata?.credits || 0);
        if (credits > 0) {
          // idempotent per billing period — renewals grant again via invoice.paid
          await creditTenant(tenantId, credits, "monthly_grant",
            `monthly_grant:${sub.id}:${subPeriodEnd(sub)}`,
            { userId, metadata: { plan, sub: sub.id } });
        }
      }
    }
    return { handled: true };
  }

  if (event.type === "invoice.paid" || event.type === "invoice.payment_succeeded") {
    const inv = event.data.object;
    const subId = inv.subscription;
    if (!subId) return { handled: false, reason: "no_sub" };
    const sub = await stripe.subscriptions.retrieve(String(subId));
    const tenantId = sub.metadata?.tenant_id;
    const plan = sub.metadata?.plan
      || SUB_BY_PRICE[sub.items?.data?.[0]?.price?.id]?.plan || "starter";
    if (!tenantId) return { handled: false, reason: "no_tenant" };
    await upsertSubscription(tenantId, sub, plan);
    const credits = SUB_BY_PRICE[sub.items?.data?.[0]?.price?.id]?.credits || 0;
    if (credits > 0) {
      await creditTenant(tenantId, credits, "monthly_grant",
        `monthly_grant:${sub.id}:${subPeriodEnd(sub)}`,
        { metadata: { plan, sub: sub.id, renewal: true } });
    }
    return { handled: true };
  }

  if (event.type === "customer.subscription.deleted") {
    const sub = event.data.object;
    const tenantId = sub.metadata?.tenant_id;
    if (tenantId) {
      await query(`UPDATE subscriptions SET status='cancelled', cancelled_at=now()
                    WHERE stripe_subscription_id=$1`, [String(sub.id)], tenantId);
      await query(`UPDATE tenants SET plan='free' WHERE id=$1`, [tenantId], tenantId);
    }
    return { handled: true };
  }

  return { handled: false, reason: "unhandled_type" };
}
