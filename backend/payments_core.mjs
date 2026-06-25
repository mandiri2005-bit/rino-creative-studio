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
import { query, pool } from "./db.js";
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
    // Set the RLS tenant context for this txn. MUST use set_config() (accepts a
    // bind param), NOT `SET LOCAL x = $1` — Postgres rejects params in SET
    // ("syntax error at or near $1"), which silently 500'd every grant.
    await client.query("SELECT set_config('app.current_tenant_id', $1, true)", [String(tenantId)]);

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

// ── Reverse a grant on refund / chargeback ────────────────────────────────────
// Negative credit_apply (reason='refund'), idempotent on refundOpId so a
// re-delivered refund never double-reverses; flips payment_events status +
// reversed_at. Finds the ORIGINAL credited row via the SECURITY-DEFINER resolver
// (the refund webhook has no tenant context): Midtrans → idempotency_key (order_id
// echoed on the refund notification); Dodo → provider_payment_id (data.payment_id
// on the refund/dispute event). Reverses the EXACT credits_granted recorded at
// grant time (immune to pricing-config drift).
export async function reverse_entitlement({
  provider, idempotencyKey = null, providerPaymentId = null,
  refundOpId, kind = "refund", rawEvent = {},
}) {
  if (!refundOpId) throw new Error("missing_refund_op");
  const look = await query(
    `SELECT id, tenant_id, user_id, credits_granted, credited, status
       FROM payment_event_for_reversal($1, $2, $3)`,
    [provider, idempotencyKey, providerPaymentId],
  );
  const row = look.rows[0];
  if (!row) {
    console.warn(`[reverse] no original payment for ${provider} ${idempotencyKey || providerPaymentId}`);
    return { reversed: false, reason: "not_found" };
  }
  if (!row.credited || !(row.credits_granted > 0)) {
    return { reversed: false, reason: "not_credited", status: row.status };
  }

  const credits = Math.trunc(row.credits_granted);
  const client = await pool.connect();
  let applied = false, balance = 0;
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.current_tenant_id', $1, true)", [String(row.tenant_id)]);
    // Negative grant, idempotent on refundOpId. credit_apply has no floor → a
    // refund after the credits were spent yields a negative balance (correct:
    // the tenant consumed credits they ultimately did not pay for).
    const cr = await client.query(
      `SELECT applied, balance FROM credit_apply($1,$2,$3,$4,$5,$6::jsonb)`,
      [row.tenant_id, row.user_id, -credits, "refund", refundOpId,
       JSON.stringify({ provider, kind, reversal_of: idempotencyKey || providerPaymentId, payment_event_id: row.id })],
    );
    applied = !!cr.rows[0]?.applied;
    balance = Number(cr.rows[0]?.balance ?? 0);
    const newStatus = kind === "chargeback" ? "disputed" : "refunded";
    if (applied) {
      // First reversal: capture the refund/dispute event payload (refund_id,
      // settlement_amount/currency, fee/tax, dispute details) for NET contra-
      // revenue (clawback ≠ net under MSA §9.7).
      await client.query(
        `UPDATE payment_events
            SET status=$2, reversed_at=now(),
                reversal_events = reversal_events || $3::jsonb, updated_at=now()
          WHERE id=$1`,
        [row.id, newStatus, JSON.stringify(rawEvent || {})],
      );
    } else {
      // Duplicate reversal (credits already reversed) — keep status idempotent,
      // do NOT re-append the event.
      await client.query(
        `UPDATE payment_events SET status=$2, updated_at=now() WHERE id=$1`,
        [row.id, newStatus],
      );
    }
    await client.query("COMMIT");
  } catch (e) {
    await client.query("ROLLBACK");
    throw e;
  } finally {
    client.release();
  }
  if (applied) {
    try { await redis.eval(_INCR_IF_PRESENT, 1, `bal:${row.tenant_id}:credits`, String(-credits)); }
    catch (e) { console.warn("[reverse] redis mirror failed:", e.message); }
  }
  return { reversed: true, applied, balance, credits };
}

// ── Reset credits to a plan target (use-it-or-lose-it) — subscription billing ──
// The RESET counterpart to grant_entitlement's ADD. Used ONLY by the GLOBAL
// subscription rail (BILLING_MODE=subscription, dodo_subscriptions.mjs); the
// Indonesia one-time path (grant_entitlement → credit_apply ADD) is UNTOUCHED.
//
// Atomic, mirroring grant_entitlement's pattern exactly:
//   pool.connect → BEGIN → set tenant ctx → credit_reset (SET balance=target) →
//   COMMIT → mirror the SIGNED delta into Redis (INCRBY-if-present, preserving holds).
//
// Idempotent on opId — one reset per subscription period (replay = NO-OP). The
// caller resolves targetCredits from the global subscription plan config and keeps
// payments_core decoupled from that config (so this stays a pure credit primitive).
//
// Implemented via credit_reset_subscription (0041): the SUB portion is SET to the
// target (use-it-or-lose-it); the TOPUP bucket survives unless already past its own
// expiry — or forfeitTopup=true (refund/abuse only; NOT on subscription.expired, since
// a paid top-up outlives a sub lapse). balance = target + surviving topup.
// Returns { applied, balance, delta } where delta = new total − pre total (signed).
export async function reset_entitlement({
  userId, tenantId, targetCredits, opId, reason = "monthly_grant", meta = {}, forfeitTopup = false,
}) {
  if (!tenantId) throw new Error("missing_tenant");
  if (!opId) throw new Error("missing_op_id");
  if (!(targetCredits >= 0)) throw new Error("invalid_target_credits");
  const target = Math.trunc(targetCredits);

  const client = await pool.connect();
  let applied = false, balance = 0, delta = 0;
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.current_tenant_id', $1, true)", [String(tenantId)]);
    // SET sub := target, keep non-expired topup. Idempotent on (tenant_id, opId).
    const r = await client.query(
      `SELECT applied, balance, delta FROM credit_reset_subscription($1,$2,$3,$4,$5,$6,$7::jsonb)`,
      [tenantId, userId || null, target, !!forfeitTopup, opId, reason, JSON.stringify(meta || {})],
    );
    applied = !!r.rows[0]?.applied;
    balance = Number(r.rows[0]?.balance ?? 0);
    delta   = Number(r.rows[0]?.delta ?? 0);
    await client.query("COMMIT");
  } catch (e) {
    await client.query("ROLLBACK");
    throw e;
  } finally {
    client.release();
  }
  // Mirror the SIGNED delta into the live Redis balance ONLY when present (never
  // clobber holds). delta can be negative (downgrade / unspent forfeiture).
  if (applied && delta !== 0) {
    try { await redis.eval(_INCR_IF_PRESENT, 1, `bal:${tenantId}:credits`, String(delta)); }
    catch (e) { console.warn("[reset] redis mirror failed:", e.message); }
  }
  return { applied, balance, delta };
}

// ── Grant one-time top-up credits (the SECOND bucket) — subscription rail ──────
// ADD `amount` to the topup bucket (balance + topup_balance), set topup_expires_at
// to the FURTHEST of the existing and the new expiry (extend, never shorten). The
// total `balance` grows by `amount` so the existing gate/Redis see it immediately.
// Idempotent on opId (the Dodo webhook-id). Returns { applied, balance, delta }.
// expiresAt is an ISO string / Date — the caller computes it (= renewal after the
// imminent one for subscribers; +30d for Free).
export async function topup_grant({
  userId, tenantId, amount, opId, expiresAt, meta = {},
}) {
  if (!tenantId) throw new Error("missing_tenant");
  if (!opId) throw new Error("missing_op_id");
  if (!(amount > 0)) throw new Error("invalid_topup_amount");
  const amt = Math.trunc(amount);

  const client = await pool.connect();
  let applied = false, balance = 0, delta = 0;
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.current_tenant_id', $1, true)", [String(tenantId)]);
    const r = await client.query(
      `SELECT applied, balance, delta FROM credit_topup_grant($1,$2,$3,$4,$5,$6::jsonb)`,
      [tenantId, userId || null, amt, opId, expiresAt || null, JSON.stringify(meta || {})],
    );
    applied = !!r.rows[0]?.applied;
    balance = Number(r.rows[0]?.balance ?? 0);
    delta   = Number(r.rows[0]?.delta ?? 0);
    await client.query("COMMIT");
  } catch (e) {
    await client.query("ROLLBACK");
    throw e;
  } finally {
    client.release();
  }
  // Mirror the added credits into the live Redis balance (only if present).
  if (applied && delta !== 0) {
    try { await redis.eval(_INCR_IF_PRESENT, 1, `bal:${tenantId}:credits`, String(delta)); }
    catch (e) { console.warn("[topup] redis mirror failed:", e.message); }
  }
  return { applied, balance, delta };
}

// Mirror a signed credit delta into the live Redis balance cache, only when the key
// already exists (never clobber holds or seed a cold cache). Used by the topup
// expiry sweep cron, which forfeits credits durably and must reflect that live.
export async function mirrorCreditDelta(tenantId, delta) {
  const d = Math.trunc(Number(delta) || 0);
  if (!tenantId || d === 0) return;
  try { await redis.eval(_INCR_IF_PRESENT, 1, `bal:${tenantId}:credits`, String(d)); }
  catch (e) { console.warn("[mirror] redis delta failed:", e.message); }
}
