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
//   This module refuses to serve an artifact until TOS_DETERMINATION_RULE names a
//   rule it actually implements. It does NOT guess from Accept-Language, IP, tenant
//   default, billing country or self-declaration. That refusal affects the assent
//   surface ONLY — the gate stays shadow and top-up is untouched.
//
//   The CURRENT release is global and English-only (owner decision 2026-08-09), so
//   the selected rule is `global_default_en` and the locale is a constant of the
//   release. Which signal would determine an "Indonesian user" (P5 A2) is STILL an
//   open owner/legal question — it is simply not asked while one locale exists.
//   Future-Indonesian-release steps: ~/docs/IMPORTANT-wimba-frontend-deploy-runbook.md.
// ─────────────────────────────────────────────────────────────────────────────
import { query, queryWithActor } from "./db.js";

// The owner/legal decision (IT4-D1). No default: an unset value is a blocker,
// not an invitation to pick one.
export function determinationRule() {
  const r = String(process.env.TOS_DETERMINATION_RULE || "").trim();
  return r.length ? r : null;
}

// Identity of the deployment that served the BYTES the user read — memo §8.4's
// "identifier rilis". Distinct from tos_version, which is the LEGAL release identity.
//
// 🔴 This is the CLOUDFLARE PAGES deployment that publishes the ToS artifact, NOT the
// Railway deployment running this API. The two are different systems on different
// release cycles: Railway can redeploy a hundred times while the served bytes never
// change, and the artifact can be republished while this API stays put. Recording a
// Railway id here would make every assent row cite provenance for a system that did
// not serve the document.
//
// There is deliberately NO FALLBACK CHAIN. A fallback cannot fail loudly, so an
// unset variable would silently degrade the evidence to whatever happened to be in
// the environment — which is the failure mode 0085's placeholder CHECK on
// serving_deployment_id exists to prevent. Unset means the assent surface refuses;
// it never means "guess".
export function servingDeploymentId() {
  const id = String(process.env.TOS_ARTIFACT_DEPLOYMENT_ID || "").trim();
  if (!id) throw new Error("tos_serving_deployment_id_unset");
  return id;
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
      WHERE published_at <= now() AND effective_at <= now() AND superseded_by IS NULL
      ORDER BY effective_at DESC, tos_version DESC
      LIMIT 1`, [], tenantId);
  if (!v.rows.length) return null;
  const version = v.rows[0].tos_version;

  // Locale choice is the SELECTED rule's output. Any rule that is not implemented
  // here is refused rather than approximated.
  let locale;
  if (rule === "global_default_en") {
    // Wimba ships ONE global English-only agreement. The locale is a constant of the
    // release, never a function of the request: `localeHint` is accepted by the
    // signature and DELIBERATELY IGNORED, and no IP address, Accept-Language header,
    // geolocation, billing country or tenant default is consulted anywhere on this
    // path. A user who asks for `id` gets the `en` artifact or nothing — never a
    // silent fallback to some other locale's bytes.
    locale = "en";
  } else if (rule === "explicit_user_locale") {
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
      ORDER BY s.served_from DESC, s.public_route
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

  const r = await queryWithActor(
    `SELECT acceptance_event_id, event_seq, event, event_at
       FROM tos_record_assent($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
    [tenantId, actorId, tosVersion, locale, artifactSha256, event, surface, rule, servingDeploymentId()],
    tenantId, actorId);
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
  const r = await queryWithActor(
    `SELECT checkout_intent_id, gate_active_at_create, tos_version, acceptance_event_id
       FROM topup_gate_create_intent($1,$2,$3)`,
    [tenantId, actorId, packKey], tenantId, actorId);
  return r.rows[0];
}

/** Current gate state, for health/ops surfaces. Never used to bypass the gate. */
export async function gateState() {
  const r = await query(
    `SELECT activation_seq, state, is_gate_active, occurred_at
       FROM g3_topup_gate_activation ORDER BY activation_seq DESC LIMIT 1`, [], null);
  return r.rows[0] || null;
}
