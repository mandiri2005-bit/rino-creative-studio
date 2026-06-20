// Lucide resolver (Node) — turns an English asset_query into the best-matching Lucide icon
// (1737 consistent ISC stroke icons already bundled at src/lucide/lucide-icons.json). This is
// the cheap, instant middle of the asset fallback ladder: manifest → THIS → Recraft → generic.
// Returns { name, viewBox, strokes:[{d,stroke,width}] } or null when nothing matches decently.

import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { nodeToD } from "../svg.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
// The lucide JSON sits at a different depth in the standalone vs the VI bundle.
const CANDIDATES = [
  join(HERE, "..", "..", "..", "src", "lucide", "lucide-icons.json"), // standalone scripts/lib/plan → src/lucide
  join(HERE, "..", "src", "lucide", "lucide-icons.json"),             // VI backend/video/whiteboard/plan → src/lucide
];

let _db = null;
function db() {
  if (_db) return _db;
  const path = CANDIDATES.find((p) => existsSync(p));
  if (!path) { _db = { icons: {}, aliases: {}, viewBox: "0 0 24 24" }; return _db; }
  const j = JSON.parse(readFileSync(path, "utf8"));
  _db = { icons: j.icons || {}, aliases: j.aliases || {}, viewBox: (j.meta && j.meta.viewBox) || "0 0 24 24" };
  return _db;
}

// Generic modifiers that must NOT drive a match (else "large intestine" → "a-large-small").
const STOP = new Set(["large", "small", "big", "little", "new", "old", "up", "down", "left", "right",
  "top", "bottom", "the", "and", "with", "for", "into", "from", "out", "off", "icon", "symbol"]);
const words = (q) => String(q || "").toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 2 && !STOP.has(w));

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
  // prefer icons fully explained by the query (penalise leftover/unrelated name parts)
  s -= (nameParts.length - matched);
  if (nameParts.length && nameParts.every((p) => qset.has(p))) s += 4; // clean whole-name hit
  return s;
}

// minScore avoids returning a weak/irrelevant icon (better to fall through to Recraft/generic).
export function resolveLucide(query, { ink = "#1F2937", width = 4, minScore = 3 } = {}) {
  const qWords = words(query);
  if (!qWords.length) return null;
  const { icons, aliases, viewBox } = db();

  let best = null, bestScore = 0;
  const consider = (name, icon) => {
    const sc = scoreIcon(qWords, name, icon);
    // higher score wins; on a tie prefer the SHORTER (simpler/base) icon name
    if (sc > bestScore || (sc === bestScore && sc > 0 && best && name.length < best.name.length)) {
      bestScore = sc; best = { name, icon };
    }
  };
  for (const [name, icon] of Object.entries(icons)) consider(name, icon);
  // aliases: alias string → canonical name; score the alias words against the canonical icon
  for (const [alias, canonical] of Object.entries(aliases)) {
    const icon = icons[canonical];
    if (icon) consider(alias, icon); // name=alias so its parts get matched too
  }
  if (!best || bestScore < minScore) return null;

  const realName = aliases[best.name] || best.name;
  const icon = icons[realName] || best.icon;
  const strokes = [];
  for (const [tag, attrs] of icon.p || []) {
    const d = tag === "path" ? attrs.d : nodeToD(tag, attrs);
    if (d) strokes.push({ d, stroke: ink, width });
  }
  return strokes.length ? { name: realName, viewBox, strokes } : null;
}
