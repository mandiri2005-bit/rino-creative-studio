// Iconify gap-filler — ~188k commercial-safe icons (121 permissive UI sets) behind the curated
// Lucide/Tabler/Phosphor. A compact 7MB token index (names only) is loaded in RAM; the icon BODY
// is lazy-loaded from the @iconify/json package (on disk) only for the winning icon, so worker RAM
// stays small. Consulted ONLY when the curated libs miss → keeps the dominant style consistent
// while covering the long tail for free (no paid Recraft). Returns { lib, license, viewBox, shapes }.
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { parseSvg, parseSvgShapes } from "../svg.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

let _idx = null;
function idx() {
  if (_idx) return _idx;
  const cands = [join(HERE, "..", "..", "..", "src", "icons", "iconify-index.json"), join(HERE, "..", "src", "icons", "iconify-index.json")];
  const p = cands.find((x) => existsSync(x));
  try { _idx = p ? JSON.parse(readFileSync(p, "utf8")) : null; } catch { _idx = null; }
  if (!_idx) _idx = { setKeys: [], sets: {}, icons: [], tokenIndex: {} };
  return _idx;
}

// @iconify/json package dir (lazy body source). Empty string if the dep isn't installed.
let _pkgDir = null;
function pkgDir() {
  if (_pkgDir !== null) return _pkgDir;
  try { _pkgDir = dirname(require.resolve("@iconify/json/collections.json")); } catch { _pkgDir = ""; }
  return _pkgDir;
}
const _setCache = new Map();
function setData(prefix) {
  if (_setCache.has(prefix)) return _setCache.get(prefix);
  let d = null;
  const dir = pkgDir();
  if (dir) { try { d = JSON.parse(readFileSync(join(dir, "json", `${prefix}.json`), "utf8")); } catch { /* missing */ } }
  _setCache.set(prefix, d);
  return d;
}

const STOP = new Set(["large", "small", "big", "little", "new", "old", "up", "down", "left", "right",
  "top", "bottom", "the", "and", "with", "for", "into", "from", "out", "off", "icon", "symbol"]);
const words = (q) => String(q || "").toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 2 && !STOP.has(w));

function scoreName(qWords, name) {
  const parts = name.split(/[^a-z0-9]+/).filter((p) => p.length > 1);
  const qset = new Set(qWords);
  let s = 0, matched = 0;
  for (const p of parts) {
    if (qset.has(p)) { s += 5; matched++; }
    else if (qWords.some((w) => p.length > 3 && (p.includes(w) || w.includes(p)))) s += 1;
  }
  s -= (parts.length - matched);                 // penalise leftover/unrelated name parts
  if (parts.length && parts.every((p) => qset.has(p))) s += 4;  // clean whole-name hit
  return s;
}

// minScore higher than the curated libs (4) so iconify only wins clear matches (it's the fallback).
export function resolveIconify(query, { ink = "#1F2937", width = 4, minScore = 4 } = {}) {
  const ix = idx();
  const qWords = words(query);
  if (!qWords.length || !ix.icons.length) return null;
  const cand = new Set();
  for (const w of qWords) { const ids = ix.tokenIndex[w]; if (ids) for (const id of ids) cand.add(id); }
  if (!cand.size) return null;
  let best = null, bestScore = 0;
  for (const id of cand) {
    const [setIdx, name] = ix.icons[id];
    const sc = scoreName(qWords, name);
    if (sc > bestScore || (sc === bestScore && best && name.length < best.name.length)) { bestScore = sc; best = { setIdx, name }; }
  }
  if (!best || bestScore < minScore) return null;
  const prefix = ix.setKeys[best.setIdx];
  const meta = ix.sets[prefix] || { w: 24, h: 24, lic: "unknown" };
  const data = setData(prefix);
  const icon = data && data.icons && data.icons[best.name];
  if (!icon || !icon.body) return null;
  const w = icon.width || meta.w || 24, h = icon.height || meta.h || 24;
  const svg = `<svg viewBox="0 0 ${w} ${h}">${icon.body}</svg>`;
  // most Iconify icons are fill-based (like Phosphor) → render as filled shapes (monochrome to ink);
  // genuine stroke icons fall back to strokes so they self-draw.
  const sh = parseSvgShapes(svg, { dropBg: false });
  if (sh.shapes.length) {
    return { lib: `iconify:${prefix}`, license: meta.lic, name: best.name, viewBox: sh.viewBox, shapes: sh.shapes.map((s) => ({ ...s, fill: s.fill && s.fill !== "none" ? ink : (s.fill || ink) })) };
  }
  const ps = parseSvg(svg, { ink });
  const strokes = ps.strokes.map((s) => ({ d: s.d, stroke: s.stroke || ink, width }));
  return strokes.length ? { lib: `iconify:${prefix}`, license: meta.lic, name: best.name, viewBox: ps.viewBox, strokes } : null;
}

export function coveredByIconify(query) { return !!resolveIconify(query); }
