// ─────────────────────────────────────────────────────────────────────────────
// tos_assent.mjs — CR-29 item 4: ToS assent capture and the top-up checkout gate.
//
// Contract: PLAN-045 §S10.G3.1-i. Schema: migration 0085.
//
// SHIPS INACTIVE. The gate's activation state lives in the database and the only
// row 0085 writes is `shadow_started`. In shadow mode `createGatedIntent` records
// what it observed and NEVER refuses, so wiring this in front of the provider call
// cannot take the top-up rail down.
//
// WHAT THIS IS NOT
//   * Not checkout idempotency — no client_operation_id, no replay handling. That
//     belongs to item 6's final contract.
//   * Not proof a provider checkout exists. An intent row records ASSENT EVIDENCE
//     at checkout-creation time. A failed provider call leaves an orphan intent:
//     that is expected. Never read the intent table as an inventory of checkouts.
//
// UNRESOLVED POLICY IS BLOCKING, NEVER SILENTLY DEFAULTED
//   Which signal determines an "Indonesian user" (P5 A2) is an OPEN owner/legal
//   decision. This module refuses to serve an artifact until TOS_DETERMINATION_RULE
//   names one. It does NOT guess from Accept-Language, IP, tenant default, billing
//   country or self-declaration. That refusal affects the assent surface ONLY —
//   the gate stays shadow and top-up is untouched.
// ─────────────────────────────────────────────────────────────────────────────
import { query } from "./db.js";

// The owner/legal decision (IT4-D1). No default: an unset value is a blocker,
// not an invitation to pick one.
export function determinationRule() {
  const r = String(process.env.TOS_DETERMINATION_RULE || "").trim();
  return r.length ? r : null;
}

// Identity of the deployment that served the bytes — memo §8.4's "identifier rilis".
// Distinct from tos_version, which is the LEGAL release identity.
export function servingDeploymentId() {
  return String(
    process.env.RAILWAY_DEPLOYMENT_ID || process.env.SERVING_DEPLOYMENT_ID ||
    process.env.RAILWAY_GIT_COMMIT_SHA || "unknown-deployment"
  ).trim();
}

/**
 * Resolve the artifact to present. Returns null when no version/artifact is
 * published — callers must treat that as "cannot present", never as "no consent
 * needed".
 *
 * `localeHint` is only ever used through the SELECTED determination rule. When the
 * rule is unset this function is not reachable: callers check determinationRule()
 * first and fail closed.
 */
export async function applicableArtifact(tenantId, localeHint) {
  const rule = determinationRule();
  if (!rule) throw new Error("tos_determination_rule_unset");

  // Currently published AND effective. A future effective_at never qualifies.
  const v = await query(
    `SELECT tos_version, effective_at
       FROM tos_versions
      WHERE effective_at <= now() AND superseded_by IS NULL
      ORDER BY effective_at DESC, tos_version DESC
      LIMIT 1`, [], tenantId);
  if (!v.rows.length) return null;
  const version = v.rows[0].tos_version;

  // Locale choice is the SELECTED rule's output. `explicit_user_locale` is the only
  // rule implemented; any other configured value is refused rather than approximated.
  let locale;
  if (rule === "explicit_user_locale") {
    locale = String(localeHint || "").trim();
    if (!locale) throw new Error("tos_locale_required_by_rule");
  } else {
    throw new Error(`tos_determination_rule_not_implemented:${rule}`);
  }

  const a = await query(
    `SELECT a.tos_version, a.locale, a.artifact_sha256, a.archive_uri,
            a.byte_size, a.content_type, s.public_route
       FROM tos_version_artifacts a
       JOIN tos_artifact_served_intervals s
         ON s.tos_version = a.tos_version AND s.locale = a.locale
        AND s.artifact_sha256 = a.artifact_sha256
      WHERE a.tos_version = $1 AND a.locale = $2 AND s.served_until IS NULL
      LIMIT 1`, [version, locale], tenantId);
  if (!a.rows.length) return null;

  return { ...a.rows[0], effective_at: v.rows[0].effective_at, determination_rule: rule };
}

/**
 * Record an Accept or Reject. Tenant and actor come from authenticated server
 * context ONLY; the database re-checks both (tenant against app.current_tenant_id,
 * actor against users belonging to that tenant) and raises otherwise.
 */
export async function recordAssent({ tenantId, actorId, tosVersion, locale, artifactSha256, event, surface }) {
  if (!["accepted", "rejected"].includes(event)) throw new Error("invalid_assent_event");
  const rule = determinationRule();
  if (!rule) throw new Error("tos_determination_rule_unset");

  const r = await query(
    `SELECT acceptance_event_id, event_seq, event, event_at
       FROM tos_record_assent($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
    [tenantId, actorId, tosVersion, locale, artifactSha256, event, surface, rule, servingDeploymentId()],
    tenantId);
  return r.rows[0];
}

/**
 * Run the gate and create the checkout intent. MUST be called AFTER the existing
 * non-free-plan eligibility check and BEFORE any provider API call — the whole
 * transaction commits first, then the caller talks to the provider.
 *
 * Shadow mode: records the false gate pin and returns normally.
 * Active mode:  fails closed; the thrown error is surfaced, never swallowed.
 */
export async function createGatedIntent({ tenantId, actorId, packKey }) {
  const r = await query(
    `SELECT checkout_intent_id, gate_active_at_create, tos_version, acceptance_event_id
       FROM topup_gate_create_intent($1,$2,$3)`,
    [tenantId, actorId, packKey], tenantId);
  return r.rows[0];
}

/** Current gate state, for health/ops surfaces. Never used to bypass the gate. */
export async function gateState() {
  const r = await query(
    `SELECT activation_seq, state, is_gate_active, occurred_at
       FROM g3_topup_gate_activation ORDER BY activation_seq DESC LIMIT 1`, [], null);
  return r.rows[0] || null;
}
