// ============================================================================
// payments_core.mjs — the ONE shared credit-grant core for the new payment
// rails (Dodo + Midtrans). Both rails call grant_entitlement(); neither
// duplicates credit logic. Stripe (billing.mjs) is intentionally left as-is.
//
// Crediting goes through the SAME durable layer as every other path
// (credit_apply SQL fn + credit_ledger + credit_balances), with the live Redis
// balance mirrored only when the cache already exists (never clobbers holds) —
// mirroring billing.mjs's proven pattern exactly.
//
// Exactly-once is doubly guaranteed:
//   1. payment_events (provider, idempotency_key) UNIQUE  — the event-row gate
//   2. credit_apply op_id = `${provider}:${idempotencyKey}` — the ledger gate
// and insert/lock-row + credit + flip-credited all happen in ONE transaction.
// ============================================================================
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { query, pool, setTenantContext } from "./db.js";
import { redis } from "./redis.js";

// ── Plan → credits map (single source, same as billing.mjs / python) ──────────
const _DEFAULT_TIER_CREDITS = { free: 100, starter: 2500, pro: 9000, enterprise: 31200 };
function _loadPricing() {
  const raw = process.env.PRICING_CONFIG_JSON;
  if (raw) {
    try { return JSON.parse(raw) || {}; }
    catch (e) { console.warn("[payments_core] PRICING_CONFIG_JSON ignored:", e.message); return {}; }
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
    catch (e) { console.warn(`[payments_core] pricing.json at ${p} ignored:`, e.message); return {}; }
  }
  return {};
}
export const _TIER_CREDITS = { ..._DEFAULT_TIER_CREDITS, ...(_loadPricing().tier_monthly_credits || {}) };

// Plan keys the client may send → internal tier (Studio→'enterprise', as in billing.mjs).
export const PLAN_TO_TIER = { starter: "starter", pro: "pro", studio: "enterprise" };
export const VALID_PLAN_KEYS = Object.keys(PLAN_TO_TIER);

/** Credits a plan grants on a successful payment (server-authoritative). */
export function creditsForPlan(planKey) {
  const tier = PLAN_TO_TIER[planKey];
  return tier ? (_TIER_CREDITS[tier] ?? 0) : 0;
}

// ── Durable credit + Redis mirror (same Lua guard as billing.mjs) ─────────────
const _INCR_IF_PRESENT =
  "if redis.call('EXISTS',KEYS[1])==1 then return redis.call('INCRBY',KEYS[1],ARGV[1]) else return -1 end";

// ── THE shared grant. Both Dodo and Midtrans webhooks call this. ──────────────
// Atomic: insert-or-lock the payment_events row keyed by (provider, idempotency_key),
// grant via credit_apply (op_id = provider:idempotencyKey), flip credited=true —
// all in ONE transaction. Then mirror Redis (after commit), like billing.mjs.
//
// Handles BOTH rail shapes:
//   - Dodo: no pre-existing row → INSERT creates it, then credit + flip.
//   - Midtrans: a PENDING row was written at create-transaction time → the INSERT
//     conflicts, we lock the existing row, then credit + flip.
// Returns { applied:boolean, balance:number, duplicate?:boolean }.
export async function grant_entitlement({
  userId, tenantId, planKey, provider, idempotencyKey,
  amount = null, currency = null, providerPaymentId = null, rawEvent = {},
}) {
  if (!tenantId) throw new Error("missing_tenant");
  if (!provider || !idempotencyKey) throw new Error("missing_idempotency_key");
  const credits = creditsForPlan(planKey);

  const client = await pool.connect();
  let applied = false, balance = 0;
  try {
    await client.query("BEGIN");
    await setTenantContext(client, tenantId);

    // 1. Insert-or-find the event row. ON CONFLICT covers Midtrans's pre-written
    //    pending row and any double-delivery.
    const ins = await client.query(
      `INSERT INTO payment_events
         (tenant_id, user_id, provider, idempotency_key, provider_payment_id,
          plan_key, amount, currency, status, raw_event)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'succeeded',$9::jsonb)
       ON CONFLICT (provider, idempotency_key) DO NOTHING
       RETURNING id, credited`,
      [tenantId, userId, provider, idempotencyKey, providerPaymentId,
       planKey, amount, currency, JSON.stringify(rawEvent)],
    );
    let rowId, alreadyCredited;
    if (ins.rows.length) {
      rowId = ins.rows[0].id; alreadyCredited = ins.rows[0].credited;
    } else {
      // Pre-existing row (Midtrans pending, or a sibling delivery mid-flight) → lock it.
      const sel = await client.query(
        `SELECT id, credited FROM payment_events
          WHERE provider=$1 AND idempotency_key=$2 FOR UPDATE`,
        [provider, idempotencyKey],
      );
      if (!sel.rows.length) {
        // ON CONFLICT said a row exists but it's not visible here (no DELETE path
        // exists, so this should be impossible). Fail loudly → ROLLBACK the whole
        // txn (incl. any credit_apply) rather than commit a half-state.
        throw new Error(`payment_event vanished for ${provider}:${idempotencyKey}`);
      }
      rowId = sel.rows[0].id; alreadyCredited = sel.rows[0].credited;
    }

    if (alreadyCredited) {
      await client.query("COMMIT");           // already processed → grant nothing
      return { applied: false, duplicate: true, balance: 0 };
    }

    // 2. Grant in the SAME transaction. credit_apply is itself idempotent on
    //    (tenant_id, op_id) → second guard against retries under a different key.
    if (credits > 0) {
      const opId = `${provider}:${idempotencyKey}`;
      const cr = await client.query(
        `SELECT applied, balance FROM credit_apply($1,$2,$3,$4,$5,$6::jsonb)`,
        [tenantId, userId, Math.trunc(credits), "topup", opId,
         JSON.stringify({ provider, plan_key: planKey, provider_payment_id: providerPaymentId, idempotency_key: idempotencyKey })],
      );
      applied = !!cr.rows[0]?.applied;
      balance = Number(cr.rows[0]?.balance ?? 0);
    } else {
      console.warn(`[payments_core] grant for plan=${planKey} maps to 0 credits (provider=${provider} key=${idempotencyKey})`);
    }

    // 3. Flip the credited guard + backfill audit fields, in the same transaction.
    //    rowCount must be 1 — a 0 here means the row id is bogus OR (more usefully)
    //    the RLS tenant context didn't match the row, which would otherwise silently
    //    lose the flip while credit_apply already moved the balance. Fail → ROLLBACK.
    const upd = await client.query(
      `UPDATE payment_events
          SET status='succeeded', credited=$2, credits_granted=$3,
              provider_payment_id=COALESCE(provider_payment_id,$4),
              amount=COALESCE(amount,$5), currency=COALESCE(currency,$6),
              updated_at=now()
        WHERE id=$1`,
      [rowId, credits > 0, credits > 0 ? Math.trunc(credits) : null, providerPaymentId, amount, currency],
    );
    if (upd.rowCount === 0) throw new Error(`failed to flip credited for payment_event id=${rowId}`);
    await client.query("COMMIT");
  } catch (e) {
    await client.query("ROLLBACK");
    throw e;
  } finally {
    client.release();
  }

  // 4. Mirror into the live Redis balance ONLY when newly applied (never clobber holds).
  if (applied) {
    try { await redis.eval(_INCR_IF_PRESENT, 1, `bal:${tenantId}:credits`, String(Math.trunc(credits))); }
    catch (e) { console.warn("[payments_core] redis mirror failed:", e.message); }
  }
  return { applied, balance, credited: credits > 0 };
}

// ── Upsert a non-crediting / pending payment-event row (audit + Midtrans pending) ─
// Used by: Midtrans create-transaction (status='pending') and either rail's
// non-success events (failed/expired/cancelled). NEVER touches a row already
// credited (the WHERE guard) so it can't undo a settled payment.
export async function recordPaymentEvent({
  userId, tenantId, provider, idempotencyKey, providerPaymentId = null,
  planKey = null, amount = null, currency = null, status, rawEvent = {},
}) {
  if (!tenantId || !provider || !idempotencyKey) throw new Error("missing_record_fields");
  await query(
    `INSERT INTO payment_events
       (tenant_id, user_id, provider, idempotency_key, provider_payment_id,
        plan_key, amount, currency, status, raw_event)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)
     ON CONFLICT (provider, idempotency_key) DO UPDATE
        SET status     = EXCLUDED.status,
            raw_event  = EXCLUDED.raw_event,
            provider_payment_id = COALESCE(payment_events.provider_payment_id, EXCLUDED.provider_payment_id),
            updated_at = now()
      WHERE payment_events.credited = FALSE`,
    [tenantId, userId, provider, idempotencyKey, providerPaymentId,
     planKey, amount, currency, status, JSON.stringify(rawEvent)],
    tenantId,
  );
}
