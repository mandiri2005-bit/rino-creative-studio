// Unified FREE icon resolver — Lucide (1737) + Tabler (5093) STROKE icons that self-draw, plus
// Phosphor (1512) FILLED icons as a last free tier. Picks the best match across all three so the
// (paid) Recraft generate-on-miss only fires for TRUE gaps. The big cost lever: more free icons
// → near-zero Recraft vector spend. Returns { lib, name, viewBox, strokes? , shapes? } or null.
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { nodeToD } from "../svg.mjs";
import { resolveIconify } from "./iconify.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
// JSONs sit at a different depth in the standalone (scripts/lib/plan) vs the VI bundle
// (backend/video/whiteboard/plan). Try both.
function findJson(rel) {
  const cands = [join(HERE, "..", "..", "..", rel), join(HERE, "..", rel)];
  return cands.find((p) => existsSync(p)) || null;
}
function load(rel) {
  const p = findJson(rel);
  try { return p ? JSON.parse(readFileSync(p, "utf8")) : null; } catch { return null; }
}

let _libs = null;
function libs() {
  if (_libs) return _libs;
  const lu = load("src/lucide/lucide-icons.json");
  const tb = load("src/icons/tabler-icons.json");
  const ph = load("src/icons/phosphor-icons.json");
  _libs = [
    // license per lib — all commercial-safe (recorded so generated-asset provenance is complete)
    lu && { name: "lucide", license: "ISC", kind: "stroke", icons: lu.icons || {}, aliases: lu.aliases || {}, viewBox: (lu.meta && lu.meta.viewBox) || "0 0 24 24", pref: 2 },
    tb && { name: "tabler", license: "MIT", kind: "stroke", icons: tb.icons || {}, aliases: {}, viewBox: (tb.meta && tb.meta.viewBox) || "0 0 24 24", pref: 2 },
    // Phosphor is FILLED → slight negative pref so a stroke icon wins ties (keeps the line look)
    ph && { name: "phosphor", license: "MIT", kind: "fill", icons: ph.icons || {}, aliases: {}, viewBox: (ph.meta && ph.meta.viewBox) || "0 0 256 256", pref: -1 },
  ].filter(Boolean);
  return _libs;
}

const STOP = new Set(["large", "small", "big", "little", "new", "old", "up", "down", "left", "right",
  "top", "bottom", "the", "and", "with", "for", "into", "from", "out", "off", "icon", "symbol"]);
const words = (q) => String(q || "").toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 2 && !STOP.has(w));

// Common narration words that MISS the icon libs (or hit a bad logo, e.g. river→la:red-river) but
// have an obvious FREE-icon synonym → keeps terrain/nature/scene concepts off paid Recraft and off a
// misleading generic (the "hills → bohlam" report). Matched on the whole lowercased+trimmed query;
// each target verified to resolve to a free Lucide/Tabler/Phosphor icon.
const QUERY_ALIASES = {
  // terrain
  hill: "mountain", hills: "mountain", hillside: "mountain", "rolling hills": "mountain", valley: "mountain", cliff: "mountain", peak: "mountain", mountains: "mountain",
  // rural / farm
  countryside: "farm", rural: "farm", farmland: "farm", pasture: "farm", ranch: "farm",
  meadow: "plant", grassland: "plant",
  "rice field": "wheat", "rice fields": "wheat", paddy: "wheat", sawah: "wheat", harvest: "wheat", crops: "wheat",
  // water
  river: "waves", stream: "waves", creek: "waves", brook: "waves", ocean: "waves", sea: "waves", lake: "waves", pond: "waves", canal: "waves",
  // woods + foliage
  woods: "trees", woodland: "trees", grove: "trees", orchard: "trees", jungle: "trees", rainforest: "trees",
  eucalyptus: "leaf", "gum leaves": "leaf", "gum tree": "tree", foliage: "leaf", herb: "leaf",
  // vineyard
  vineyard: "grape", grapevine: "grape", grapes: "grape",
};
const aliasQuery = (q) => QUERY_ALIASES[String(q || "").trim().toLowerCase()] || q;

// Sci-fi / robot guard. In a cultural-story whiteboard explainer a "robot/alien/android" icon is
// essentially ALWAYS a creative misfire by the Visual Director (e.g. it emits "alien" for "a newcomer
// in a different land" → tabler:alien, which reads as a robot — Rino: "masih ada robot"). These words
// are blocked from the icon match so the resolver falls through to the human-readable LABEL instead
// (resolvePlan candidate chain). isBlockedQuery() also tells coveredByLibrary() to report TRUE so the
// PAID Recraft generate-on-miss is skipped (otherwise it would just draw the robot itself).
const SCIFI_BLOCK = new Set(["alien", "aliens", "robot", "robots", "android", "androids", "cyborg",
  "cyborgs", "droid", "droids", "humanoid", "humanoids", "automaton", "automatons", "ufo", "ufos",
  "mech", "mecha", "bot", "bots", "terminator", "extraterrestrial"]);
export function isBlockedQuery(q) {
  const t = String(q || "").trim().toLowerCase();
  if (!t) return false;
  if (SCIFI_BLOCK.has(t)) return true;
  return t.split(/[^a-z0-9]+/).some((w) => SCIFI_BLOCK.has(w));
}

function scoreIcon(qWords, name, icon) {
  const nameParts = name.split("-").filter((p) => p.length > 1);
  const qset = new Set(qWords);
  let s = 0, matched = 0;
  for (const np of nameParts) {
    if (qset.has(np)) { s += 5; matched++; }
    else if (qWords.some((w) => np.length > 3 && (np.includes(w) || w.includes(np)))) s += 1;
  }
  for (const w of qWords) {
    for (const t of icon.t || []) { if (t === w) { s += 3; break; } }
    if ((icon.t || []).some((t) => t !== w && (t.includes(w) || w.includes(t)))) s += 1;
  }
  s -= (nameParts.length - matched);
  if (nameParts.length && nameParts.every((p) => qset.has(p))) s += 4;
  return s;
}

// best (icon, score) within ONE lib
function bestInLib(qWords, lib) {
  let best = null, bestScore = 0;
  const consider = (name, icon) => {
    const sc = scoreIcon(qWords, name, icon);
    if (sc > bestScore || (sc === bestScore && sc > 0 && best && name.length < best.name.length)) {
      bestScore = sc; best = { name, icon };
    }
  };
  for (const [name, icon] of Object.entries(lib.icons)) consider(name, icon);
  for (const [alias, canonical] of Object.entries(lib.aliases || {})) {
    const icon = lib.icons[canonical];
    if (icon) consider(alias, icon);
  }
  return best ? { ...best, score: bestScore } : null;
}

// Resolve a query to the best free icon across all libs. minScore avoids weak matches (→ Recraft).
export function resolveIcon(query, { ink = "#1F2937", width = 4, minScore = 3 } = {}) {
  if (isBlockedQuery(query)) return null; // never render a robot/alien — fall through to the label
  query = aliasQuery(query); // route common synonyms to a known free icon BEFORE scoring
  const qWords = words(query);
  if (!qWords.length) return null;
  let winner = null;
  for (const lib of libs()) {
    const b = bestInLib(qWords, lib);
    if (!b) continue;
    const effective = b.score + (lib.pref || 0); // stroke libs edge out a filled tie
    if (!winner || effective > winner.effective) winner = { ...b, lib, effective };
  }
  // curated libs (Lucide/Tabler/Phosphor) missed → fall to the ~188k Iconify gap-filler (free).
  if (!winner || winner.score < minScore) return resolveIconify(query, { ink, width });
  const { lib } = winner;
  const realName = (lib.aliases && lib.aliases[winner.name]) || winner.name;
  const icon = lib.icons[realName] || winner.icon;

  if (lib.kind === "fill") {
    const shapes = (icon.d || []).map((d) => ({ d, fill: ink }));
    return shapes.length ? { lib: lib.name, license: lib.license, name: realName, viewBox: lib.viewBox, shapes } : null;
  }
  const strokes = [];
  for (const [tag, attrs] of icon.p || []) {
    const d = tag === "path" ? attrs.d : nodeToD(tag, attrs);
    if (d) strokes.push({ d, stroke: ink, width });
  }
  return strokes.length ? { lib: lib.name, license: lib.license, name: realName, viewBox: lib.viewBox, strokes } : null;
}

// is this query covered by ANY free lib? (gates the paid Recraft generate-on-miss)
export function coveredByIconLibs(query) {
  return !!resolveIcon(query);
}
