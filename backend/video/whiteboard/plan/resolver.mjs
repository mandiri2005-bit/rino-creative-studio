// Asset resolver (guide §J). The Visual Director emits an `asset_query` like
// "friendly AI assistant" — it does NOT know file names. This turns a query into a
// concrete manifest asset by scoring tag / id overlap, with a generic fallback.
// Pure functions → unit-testable. (Semantic embedding search is the later upgrade.)

import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { coveredByIconLibs } from "./iconlibs.mjs";

export function loadManifest(assetsDir) {
  const man = JSON.parse(readFileSync(join(assetsDir, "manifest.json"), "utf8"));
  return { ...man, _dir: assetsDir };
}

// The bundled curated assets live next to this module (../assets/whiteboard).
const ASSETS_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "assets", "whiteboard");
let _defaultMan = null;
export function defaultManifest() {
  if (!_defaultMan) _defaultMan = loadManifest(ASSETS_DIR);
  return _defaultMan;
}

// Is this query already covered by the FREE libs (curated manifest OR Lucide/Tabler/Phosphor)?
// Gates the (paid) Recraft generate-on-miss → keeps API spend to TRUE gaps. With Tabler (5093) +
// Phosphor (1512) added, coverage jumps ~1.7k→~8.3k icons, so Recraft vector almost never fires.
export function coveredByLibrary(query, manifest) {
  const m = manifest || defaultManifest();
  const r = resolveAsset(query, m);
  if (r && !r.fallback) return true;
  return coveredByIconLibs(query);
}

export function scoreAsset(query, asset) {
  const q = String(query || "").toLowerCase();
  const qWords = new Set(q.split(/[^a-z0-9]+/).filter(Boolean));
  let score = 0;
  for (const tag of asset.tags || []) {
    const t = String(tag || "").toLowerCase().trim();
    if (!t) continue;
    // WHOLE-WORD match. A naive q.includes(tag) made the short ai_agent tags "ai" and "bot" match as
    // SUBSTRINGS of common words — tr-AI-n, moun-TAI-n, br-AI-n, ch-AI-n, AI-rplane, bot-TLE — so the
    // robot (ai_agent) hijacked train/mountain/brain/chain/airplane/bottle. (Rino: "masih ada robot")
    const hit = t.includes(" ") ? q.includes(t) : qWords.has(t);
    if (hit) score += 5;
  }
  const idWords = String(asset.id || "").replaceAll("_", " ");
  if (idWords && q.includes(idWords)) score += 10;
  return score;
}

// Returns { asset, score, fallback } — fallback=true when nothing matched and we fell
// back to generic_concept (so the caller / QA can flag weak coverage).
export function resolveAsset(assetQuery, manifest) {
  const ranked = (manifest.assets || [])
    .map((asset) => ({ asset, score: scoreAsset(assetQuery, asset) }))
    .sort((a, b) => b.score - a.score);

  if (ranked.length && ranked[0].score > 0) {
    return { asset: ranked[0].asset, score: ranked[0].score, fallback: false };
  }
  const generic = (manifest.assets || []).find((a) => a.id === "generic_concept");
  return { asset: generic || null, score: 0, fallback: true };
}

// Convenience: resolve + return the absolute SVG path (or null if no asset at all).
export function resolveAssetPath(assetQuery, manifest) {
  const r = resolveAsset(assetQuery, manifest);
  if (!r.asset) return { ...r, path: null };
  return { ...r, path: join(manifest._dir, r.asset.path) };
}
