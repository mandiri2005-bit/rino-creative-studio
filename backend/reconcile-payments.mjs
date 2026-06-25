#!/usr/bin/env node
// ============================================================================
// reconcile-payments.mjs — operator/cron job that heals payments whose webhook
// was missed or arrived too late (audit finding #2: stale-timestamp 403, lost
// deliveries, downtime). Run ON THE RUNTIME (env already set):
//
//   node reconcile-payments.mjs                 # DRY RUN — report only
//   node reconcile-payments.mjs --fix           # actually settle missed Midtrans
//   node reconcile-payments.mjs --fix --minutes=30
//
// MIDTRANS: scans stale PENDING rows, re-checks each via the status API, and
//   settles the ones that actually paid. SAFE to auto-grant — the op_id is
//   `midtrans:<order_id>` (payment-scoped), so this and a late real
//   notification converge → credit_apply is idempotent, no double-credit.
//
// DODO: lists recent succeeded payments and ALERTS on any with no credited
//   payment_events row. NOT auto-granted — Dodo's op_id is `dodo:<webhook-id>`
//   (per-delivery); a reconciliation grant would use a different op_id than a
//   late real webhook → double-credit risk. Surface for manual review instead.
// ============================================================================
import midtransClient from "midtrans-client";
import { query } from "./db.js";
import * as midtrans from "./midtrans.mjs";
import { dodo } from "./dodo.mjs";
import * as subscriptions from "./dodo_subscriptions.mjs";

const FIX = process.argv.includes("--fix");
const MINS = Number((process.argv.find((a) => a.startsWith("--minutes=")) || "").split("=")[1] || 60);

const core = process.env.MIDTRANS_SERVER_KEY
  ? new midtransClient.CoreApi({
      isProduction: process.env.MIDTRANS_IS_PRODUCTION === "true",
      serverKey: process.env.MIDTRANS_SERVER_KEY,
      clientKey: process.env.MIDTRANS_CLIENT_KEY,
    })
  : null;

async function reconcileMidtrans() {
  if (!core) { console.log("[recon] midtrans not configured — skip"); return; }
  const stale = (await query("SELECT * FROM stale_pending_payments('midtrans', $1)", [MINS])).rows;
  console.log(`[recon] midtrans: ${stale.length} pending older than ${MINS}m`);
  let settled = 0, stillPending = 0, failed = 0;
  for (const row of stale) {
    let st;
    try { st = await core.transaction.status(row.idempotency_key); }
    catch (e) { console.warn(`[recon] midtrans status ${row.idempotency_key}: ${e.message}`); failed++; continue; }
    const tx = st?.transaction_status, fr = st?.fraud_status;
    const grantWorthy = tx === "settlement" || (tx === "capture" && fr === "accept");
    if (grantWorthy) {
      settled++;
      if (FIX) {
        const r = await midtrans.handleNotification(st); // idempotent via order_id op_id
        console.log(`[recon] settled order=${row.idempotency_key} ->`, JSON.stringify(r));
      } else {
        console.log(`[recon] WOULD settle order=${row.idempotency_key} (status=${tx})`);
      }
    } else {
      stillPending++;
    }
  }
  console.log(`[recon] midtrans: ${FIX ? "settled" : "would-settle"}=${settled} stillPending=${stillPending} failed=${failed}`);
}

async function reconcileDodo() {
  if (!dodo) { console.log("[recon] dodo not configured — skip"); return; }
  let checked = 0, orphans = 0;
  try {
    for await (const p of dodo.payments.list({})) {
      if (p.status !== "succeeded") continue;
      checked++;
      const look = (await query("SELECT credited FROM payment_event_for_reversal('dodo', $1, $2)", [null, p.payment_id])).rows[0];
      if (!look || !look.credited) {
        orphans++;
        console.warn(`[recon] DODO ORPHAN payment_id=${p.payment_id} amount=${p.total_amount} ${p.currency} created=${p.created_at} — NOT credited (manual review)`);
      }
      if (checked >= 200) break; // bound the scan
    }
  } catch (e) { console.warn(`[recon] dodo list error: ${e.message}`); }
  console.log(`[recon] dodo: checked=${checked} orphans=${orphans} (alert-only; no auto-grant)`);
}

// SUBSCRIPTION (global): the dunning-exhaustion fallback. Dodo may leave a sub in
// on_hold forever with no terminal event; this downgrades on_hold/cancelled subs
// whose period end is well past (verified live via the API). Self-gates: a no-op
// unless BILLING_MODE=subscription. SAFE to auto-run — idempotent (expired op_id).
async function reconcileSubscriptions() {
  if (!subscriptions.subscriptionMode()) { console.log("[recon] subscription mode off — skip subs"); return; }
  if (!FIX) { console.log("[recon] subs: dry-run (pass --fix to downgrade stuck on_hold/cancelled subs)"); return; }
  try {
    const r = await subscriptions.reconcileStuckSubscriptions({ graceDays: Number(process.env.SUB_RECONCILE_GRACE_DAYS || 3) });
    console.log(`[recon] subs:`, JSON.stringify(r));
  } catch (e) { console.warn(`[recon] subs error: ${e.message}`); }
  // Optional drift-safety: forfeit any expired top-up (covers a downgraded user whose
  // paid top-up outlived the sub, + general drift). Subscriber renewals already sweep.
  try {
    const s = await subscriptions.sweepExpiredTopup({});
    console.log(`[recon] topup-sweep:`, JSON.stringify({ swept: s.swept }));
  } catch (e) { console.warn(`[recon] topup-sweep error: ${e.message}`); }
}

(async () => {
  console.log(`[recon] start ${FIX ? "(FIX)" : "(dry-run)"} minutes=${MINS}`);
  await reconcileMidtrans();
  await reconcileDodo();
  await reconcileSubscriptions();
  console.log("[recon] done");
  process.exit(0);
})().catch((e) => { console.error("[recon] fatal:", e); process.exit(1); });
