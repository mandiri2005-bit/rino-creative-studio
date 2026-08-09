// g3_lots.mjs — L2C · CR-29 items 1 + 2 + 5, runtime side.
//
// Decision records (all three ANSWERED 2026-08-09; no decision blocker remains):
//   D1 + D2  WIMBA_CONT_PROJECT/L2C-CR29-DECISIONS-1-2-ANSWERED-001.md
//   D3       WIMBA_CONT_PROJECT/L2C-CR29-DECISION-3-ANSWERED-001.md   <- AUTHORITATIVE
// Schema:  database/migrations/0086_cr29_items_1_2_5_lot_provenance_and_journals.sql
//
// ─────────────────────────────────────────────────────────────────────────────
// DEFAULT OFF. `G3_LOT_WRITER_ENABLED=1` arms it.
//
// This module ships INERT, exactly as 0085 shipped the top-up gate inactive, and
// for a harder reason than caution. PLAN-045 G3.1-c freezes the ingestion
// sequence — "grant commits, the credited anchor persists, the webhook responds"
// — and says in terms that it may not be "modified, reordered, wrapped in a new
// transaction, or EXTENDED". A lot write that can throw into that path extends
// it. So:
//
//   * flag OFF (production default): recordTopupLot() returns immediately. The
//     frozen sequence is byte-for-byte what it was at 51aa6ee.
//   * flag ON: the lot write runs AFTER the credited anchor and CANNOT throw
//     into the webhook. A failure is logged CRITICAL and left recoverable —
//     lots are derivable from the ledger row, so a missing lot is repairable,
//     while a 500 on an already-credited payment is not free of consequence.
//
// The one thing that is never best-effort is CORRECTNESS: if the authoritative
// timestamp is missing the writer fails closed and writes NO lot (Decision 2).
// It never invents a value to keep going.
// ─────────────────────────────────────────────────────────────────────────────
import { pool, query } from "./db.js";

export const G3_LOT_WRITER_ENABLED = () => process.env.G3_LOT_WRITER_ENABLED === "1";

// Dodo is Merchant of Record: it is the legal seller to the buyer, collects the
// customer's tax, and pays Wimba a Supplier Fee net of tax, discount and other
// deductions. Decision 3's governing assumption, encoded once.
const DODO_TERMS = { salesChannel: "dodo_mor", taxOwner: "provider" };

// ── Supplier fee extraction ──────────────────────────────────────────────────
// Decision 3 A1/B2: the cash-in anchor is what the provider PAYS WIMBA, never
// the customer's gross checkout. Dodo reports that as the settlement fields.
//
// 🔴 Returns null rather than guessing. `total_amount` is the CUSTOMER GROSS and
// is NOT a fallback — substituting it would recognise Dodo's tax, discount and
// fee as Wimba revenue, which is precisely the over-recognition Decision 3 B2
// identified. A null here makes the lot unpriced, which is loud and repairable;
// a wrong number is silent and is not.
export function extractSupplierFee(data) {
  const minor = data?.settlement_amount;
  const ccy = data?.settlement_currency;
  if (minor === null || minor === undefined || ccy === null || ccy === undefined) return null;
  const n = Number(minor);
  if (!Number.isFinite(n) || !Number.isInteger(n) || n < 0) return null;
  if (typeof ccy !== "string" || ccy.trim() === "") return null;
  return { minor: n, currency: ccy.trim().toUpperCase() };
}

// ── Item 1: pin the CR-29 business timestamp ─────────────────────────────────
// Passes payload.data.created_at through VERBATIM. The single strict parse lives
// in SQL (g3_pin_payment_at) so both paths share one implementation and neither
// can drift into a coercion ladder.
//
// Identity split, enforced by the caller and asserted by T79: the aggregate is
// keyed by PAYMENT identity (provider, provider_payment_id). webhookId is
// RECEIPT identity and is recorded only as pinning provenance — it may never
// stand in for providerPaymentId.
export async function pinPaymentAt({ provider, providerPaymentId, createdAtRaw, webhookId }) {
  if (!providerPaymentId) throw new Error("g3_pin_missing_payment_anchor");
  const r = await query(
    `SELECT payment_at, pinned, outcome FROM g3_pin_payment_at($1,$2,$3,$4)`,
    [provider, providerPaymentId, createdAtRaw ?? null, webhookId ?? null],
  );
  const row = r.rows[0] || {};
  if (row.outcome === "divergent_alert_only") {
    // ALERT ONLY. Never an overwrite, never a quarantine (MATRIX-045 T79).
    console.warn(
      `[g3][alert] payment_at divergence provider='${provider}' provider_payment_id='${providerPaymentId}' ` +
      `pinned='${row.payment_at}' observed_webhook_id='${webhookId}' action=ALERT_ONLY impact=none`,
      { event: "g3.payment_at_divergence", severity: "warning", provider, provider_payment_id: providerPaymentId },
    );
  }
  return { paymentAt: row.payment_at ?? null, pinned: !!row.pinned, outcome: row.outcome };
}

export async function recordPaymentTerms({
  provider, providerPaymentId, salesChannel, taxOwner, supplierFee, providerTaxMinor = null,
}) {
  await query(
    `SELECT g3_record_payment_terms($1,$2,$3,$4,$5,$6,$7)`,
    [provider, providerPaymentId, salesChannel, taxOwner,
     supplierFee?.minor ?? null, supplierFee?.currency ?? null, providerTaxMinor],
  );
}

// ── Items 1 + 2 + 5: the ordinary-grant path ─────────────────────────────────
// Called from handleTopupPayment AFTER the credited anchor. Never throws into
// the webhook (see the header): every failure is logged and swallowed at this
// boundary, and only here.
export async function recordTopupLot({
  tenantId, provider = "dodo", payload, webhookId, credits, ledgerOpId,
}) {
  if (!G3_LOT_WRITER_ENABLED()) return { skipped: true, reason: "flag_off" };

  const data = payload?.data || {};
  const providerPaymentId = data.payment_id;
  if (!providerPaymentId) return { skipped: true, reason: "no_payment_anchor" };

  try {
    // 1. Pin the business timestamp from the FIRST signature-valid delivery.
    //    `createdAtRaw` is passed byte-for-byte; nothing derives it.
    const pin = await pinPaymentAt({
      provider, providerPaymentId, createdAtRaw: data.created_at, webhookId,
    });

    // 2. Record the commercial terms the valuation depends on.
    await recordPaymentTerms({
      provider, providerPaymentId,
      salesChannel: DODO_TERMS.salesChannel, taxOwner: DODO_TERMS.taxOwner,
      supplierFee: extractSupplierFee(data),
      providerTaxMinor: Number.isInteger(Number(data.tax)) ? Number(data.tax) : null,
    });

    // 3. Fail closed. Decision 2: with no authoritative timestamp there is no
    //    lot, and no fallback exists. Loud, and repairable on a later delivery
    //    that does carry a parseable created_at.
    if (!pin.paymentAt) {
      console.error(
        `[g3][CRITICAL] no authoritative payment_at; NO LOT WRITTEN provider='${provider}' ` +
        `provider_payment_id='${providerPaymentId}' outcome='${pin.outcome}' tenant_id='${tenantId}' ` +
        `action=FAIL_CLOSED remediation=await_delivery_with_parseable_data.created_at`,
        { event: "g3.lot_fail_closed", severity: "critical", provider, provider_payment_id: providerPaymentId },
      );
      return { written: false, reason: "fail_closed_no_payment_at" };
    }

    // 4. Write the lot through the ONE writer both paths use.
    const r = await query(
      `SELECT lot_id, acquired_at, rate_date, is_priced, grandfathered, reason
         FROM g3_write_lot($1,'topup',$2,$3,'grant',$4,$5,NULL)`,
      [tenantId, credits, ledgerOpId, provider, providerPaymentId],
      tenantId,
    );
    const lot = r.rows[0] || {};

    // 5. Item 5 — cash-in posting, at the SUPPLIER FEE. Only when the lot is
    //    priced; an unpriced lot has no established amount to post and posting
    //    a guessed one is the failure Decision 3 B2 exists to prevent.
    if (lot.is_priced) {
      try {
        await query(`SELECT g3_post_cash_in($1,$2,$3,$4)`,
          [tenantId, provider, providerPaymentId, `g3_cashin:${providerPaymentId}`], tenantId);
      } catch (e) {
        console.error(
          `[g3][CRITICAL] cash-in posting failed provider_payment_id='${providerPaymentId}' ` +
          `lot='${lot.lot_id}' err='${e.message}' action=LOT_WRITTEN_POSTING_MISSING`,
          { event: "g3.cash_in_post_failed", severity: "critical" },
        );
      }
    } else {
      console.warn(
        `[g3] lot written UNPRICED provider_payment_id='${providerPaymentId}' lot='${lot.lot_id}' ` +
        `— no cash-in posted; supplier fee was not established (customer gross is not a substitute)`,
        { event: "g3.lot_unpriced", severity: "warning" },
      );
    }

    return {
      written: true, lotId: lot.lot_id, acquiredAt: lot.acquired_at, rateDate: lot.rate_date,
      isPriced: lot.is_priced, grandfathered: lot.grandfathered, reason: lot.reason,
    };
  } catch (e) {
    // The frozen sequence must not be extended: never rethrow from here.
    console.error(
      `[g3][CRITICAL] lot write failed, credit already granted provider='${provider}' ` +
      `provider_payment_id='${providerPaymentId}' tenant_id='${tenantId}' err='${e.message}' ` +
      `action=SWALLOWED_TO_PROTECT_FROZEN_ACK remediation=rederive_lot_from_ledger_op_id`,
      { event: "g3.lot_write_failed", severity: "critical", ledger_op_id: ledgerOpId },
    );
    return { written: false, reason: "error", error: e.message };
  }
}

// ── Item 2: the second processing path — quarantine, and its release ─────────
// A post-boundary top-up whose ToS metadata is absent or unresolvable is
// quarantined: no lot, no guessed version, and THE PAYMENT IS NEVER FORFEITED.
// Release is a manual, attributed, per-row disposition.
export async function quarantineTopup({
  provider = "dodo", providerPaymentId, tenantId, reason, metadataAsReceived = {},
}) {
  await query(
    `INSERT INTO g3_topup_quarantine (provider, provider_payment_id, tenant_id, reason, metadata_as_received)
     VALUES ($1,$2,$3,$4,$5::jsonb)
     ON CONFLICT (provider, provider_payment_id) WHERE released_at IS NULL DO NOTHING`,
    [provider, providerPaymentId, tenantId ?? null, reason, JSON.stringify(metadataAsReceived || {})],
  );
  console.warn(
    `[g3][quarantine] provider_payment_id='${providerPaymentId}' reason='${reason}' ` +
    `action=NO_LOT_WRITTEN payment_forfeited=false remediation=manual_disposition`,
    { event: "g3.topup_quarantined", severity: "warning" },
  );
}

// Precondition A lives in SQL: this calls the SAME g3_write_lot against the SAME
// stored payment_at, so acquired_at and rate_date cannot diverge from what the
// ordinary path produced. It does not re-read the webhook and does not get its
// own provenance class — the released lot inherits the class its payment fixed.
export async function releaseQuarantinedTopup({
  provider = "dodo", providerPaymentId, tenantId, credits, ledgerOpId, releasedBy, disposition,
}) {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.current_tenant_id', $1, true)", [String(tenantId)]);
    const r = await client.query(
      `SELECT lot_id, acquired_at, rate_date, is_priced, grandfathered, reason
         FROM g3_release_quarantined_topup($1,$2,$3,$4,$5,$6,$7)`,
      [provider, providerPaymentId, tenantId, credits, ledgerOpId, releasedBy, disposition],
    );
    const lot = r.rows[0] || {};
    if (lot.is_priced) {
      await client.query(`SELECT g3_post_cash_in($1,$2,$3,$4)`,
        [tenantId, provider, providerPaymentId, `g3_cashin:${providerPaymentId}`]);
    }
    await client.query("COMMIT");
    return {
      released: true, lotId: lot.lot_id, acquiredAt: lot.acquired_at, rateDate: lot.rate_date,
      isPriced: lot.is_priced, grandfathered: lot.grandfathered, reason: lot.reason,
    };
  } catch (e) {
    await client.query("ROLLBACK");
    throw e;   // a manual ops action MUST surface its failure
  } finally {
    client.release();
  }
}

// ── Item 5: consumption is the only revenue moment ───────────────────────────
export async function postConsumption({
  tenantId, lotId, credits, revenueAccount, opId, occurredAt,
}) {
  const r = await query(
    `SELECT g3_post_consumption($1,$2,$3,$4,$5,$6) AS entry_id`,
    [tenantId, lotId, credits, revenueAccount, opId, occurredAt], tenantId,
  );
  return r.rows[0]?.entry_id ?? null;
}

// Decision 3 A2/A3: HELD and RELEASED post NOTHING to the general ledger. These
// exist so the contract is stated in code and testable, not merely documented.
export const postHeld = async () => ({ posted: false, reason: "no_gl_entry_by_policy_A2" });
export const postReleased = async () => ({ posted: false, reason: "no_gl_entry_by_policy_A3" });
