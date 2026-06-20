// Asset resolver (guide §J). The Visual Director emits an `asset_query` like
// "friendly AI assistant" — it does NOT know file names. This turns a query into a
// concrete manifest asset by scoring tag / id overlap, with a generic fallback.
// Pure functions → unit-testable. (Semantic embedding search is the later upgrade.)

import { readFileSync } from "node:fs";
import { join } from "node:path";

export function loadManifest(assetsDir) {
  const man = JSON.parse(readFileSync(join(assetsDir, "manifest.json"), "utf8"));
  return { ...man, _dir: assetsDir };
}

export function scoreAsset(query, asset) {
  const q = String(query || "").toLowerCase();
  let score = 0;
  for (const tag of asset.tags || []) {
    if (q.includes(String(tag).toLowerCase())) score += 5;
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
