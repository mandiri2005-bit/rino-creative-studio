// ============================================================================
// mode_gate.mjs — config-driven Video-Instant MODE tier gate (GLOBAL deployment).
//
// Enforced at /api/video/assemble BEFORE any credit hold (403 feature_not_available),
// so a below-tier request never reaches hold→refund. This is the SECURITY layer —
// the UI disable+tooltip is only cosmetic. Recraft is gated HERE (Color=starter+,
// Realistis/detail=plus+); there is no Recraft model picker.
//
// CONFIG-DRIVEN + ISOLATED: the Indonesia one-time deployment's pricing.json has no
// `mode_min_tier`, so modeRequiredTier() returns null → the gate is a no-op there
// (mode gating off, status quo). Only the global pricing.json (mode_min_tier +
// tier_rank with the enterprise→rank-4 alias) arms it.
// ============================================================================
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

function _loadPricing() {
  const raw = process.env.PRICING_CONFIG_JSON;
  if (raw) { try { return JSON.parse(raw) || {}; } catch { return {}; } }
  const here = path.dirname(fileURLToPath(import.meta.url));
  const candidates = [
    process.env.PRICING_CONFIG_PATH,
    path.join(here, "..", "..", "config", "pricing.json"),
    "/app/config/pricing.json",
    path.join(process.cwd(), "config", "pricing.json"),
  ];
  for (const p of candidates) {
    try { if (p && fs.existsSync(p)) return JSON.parse(fs.readFileSync(p, "utf8")) || {}; } catch { /* ignore */ }
  }
  return {};
}
const _P = _loadPricing();
// Default = Indonesia 4-tier ladder; global pricing.json overrides with the 5-tier
// (+ enterprise aliased to rank 4 = ultra, so the hardcoded 4K lock maps to Ultra).
const TIER_RANK = { free: 0, starter: 1, pro: 2, enterprise: 3, ...(_P.tier_rank || {}) };
const MODE_MIN_TIER = { ...(_P.mode_min_tier || {}) };   // empty on Indonesia → no-op

export function tierAtLeast(have, need) {
  if (!need) return true;
  return (TIER_RANK[have ?? "free"] ?? 0) >= (TIER_RANK[need] ?? 0);
}

/** Required tier for a VI mode, or null when mode-gating is OFF (Indonesia / no config).
 *  Whiteboard genres are keyed 'wb:<genre>'. An UNKNOWN mode (while gating is on) is
 *  fail-closed to 'pro'. */
export function modeRequiredTier(visualMode, whiteboardGenre) {
  if (Object.keys(MODE_MIN_TIER).length === 0) return null;            // gating off
  const key = visualMode === "whiteboard" ? ("wb:" + (whiteboardGenre || "lineart")) : String(visualMode || "");
  return MODE_MIN_TIER[key] || "pro";                                  // fail-closed
}

export function modeGateActive() { return Object.keys(MODE_MIN_TIER).length > 0; }
