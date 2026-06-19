import data from "../lucide/lucide-icons.json";
import type { Illustration, Stroke } from "../types";

// Browser-safe Lucide adapter: maps an icon name OR a loose concept to a self-drawable
// Illustration. Converts every Lucide SVG node (path/line/polyline/polygon/circle/
// ellipse/rect) into a single `d` path so the self-draw engine can stroke it. No fs,
// no network — the 1737-icon bundle is import-inlined by the bundler.

type Node = [string, Record<string, string>];
const D = data as unknown as {
  meta?: { viewBox?: string };
  aliases?: Record<string, string>;
  icons: Record<string, { p: Node[]; t?: string[] }>;
};
const ICONS = D.icons || {};
const ALIASES = D.aliases || {};
const VIEWBOX = D.meta?.viewBox || "0 0 24 24";

const num = (a: Record<string, string>, k: string) => parseFloat(a[k] ?? "0");

function pointsToD(points: string, close: boolean): string {
  const v = points.trim().split(/[\s,]+/).map(Number);
  let d = "";
  for (let i = 0; i + 1 < v.length; i += 2) d += `${i === 0 ? "M" : "L"}${v[i]} ${v[i + 1]} `;
  return d.trim() + (close ? " Z" : "");
}

function nodeToD(tag: string, a: Record<string, string>): string | null {
  switch (tag) {
    case "path":
      return a.d || null;
    case "line":
      return `M${num(a, "x1")} ${num(a, "y1")} L${num(a, "x2")} ${num(a, "y2")}`;
    case "polyline":
      return a.points ? pointsToD(a.points, false) : null;
    case "polygon":
      return a.points ? pointsToD(a.points, true) : null;
    case "circle": {
      const cx = num(a, "cx"), cy = num(a, "cy"), r = num(a, "r");
      return `M${cx - r} ${cy} a ${r} ${r} 0 1 0 ${2 * r} 0 a ${r} ${r} 0 1 0 ${-2 * r} 0`;
    }
    case "ellipse": {
      const cx = num(a, "cx"), cy = num(a, "cy"), rx = num(a, "rx"), ry = num(a, "ry");
      return `M${cx - rx} ${cy} a ${rx} ${ry} 0 1 0 ${2 * rx} 0 a ${rx} ${ry} 0 1 0 ${-2 * rx} 0`;
    }
    case "rect": {
      const x = num(a, "x"), y = num(a, "y"), w = num(a, "width"), h = num(a, "height");
      let rx = a.rx != null ? num(a, "rx") : 0;
      let ry = a.ry != null ? num(a, "ry") : rx;
      if (!rx) rx = ry;
      if (!rx && !ry) return `M${x} ${y} h${w} v${h} h${-w} Z`;
      return (
        `M${x + rx} ${y} h${w - 2 * rx} a${rx} ${ry} 0 0 1 ${rx} ${ry} ` +
        `v${h - 2 * ry} a${rx} ${ry} 0 0 1 ${-rx} ${ry} h${-(w - 2 * rx)} ` +
        `a${rx} ${ry} 0 0 1 ${-rx} ${-ry} v${-(h - 2 * ry)} a${rx} ${ry} 0 0 1 ${rx} ${-ry} Z`
      );
    }
    default:
      return null;
  }
}

// name -> canonical (exact / alias), else best keyword/name match across the catalogue.
function resolveName(q: string): string | null {
  const query = q.trim().toLowerCase().replace(/\s+/g, "-");
  if (!query) return null;
  if (ICONS[query]) return query;
  if (ALIASES[query]) return ALIASES[query];
  const bare = query.replace(/-/g, " ");
  let best: string | null = null;
  let bestScore = 0;
  for (const name in ICONS) {
    let s = 0;
    if (name === query) s += 100;
    else if (name.startsWith(query)) s += 60;
    else if (name.includes(query)) s += 35;
    for (const kw of ICONS[name].t || []) {
      if (kw === bare) s += 30;
      else if (bare.includes(kw) || kw.includes(bare)) s += 12;
    }
    if (s > bestScore) {
      bestScore = s;
      best = name;
    }
  }
  return best;
}

const cache = new Map<string, Illustration | null>();

// Resolve a Lucide icon name or concept to a self-drawable Illustration (viewBox
// "0 0 24 24", one stroke per node). Returns null if nothing matches.
export function lucideToIllustration(nameOrConcept: string): Illustration | null {
  if (!nameOrConcept) return null;
  if (cache.has(nameOrConcept)) return cache.get(nameOrConcept) ?? null;
  const name = resolveName(nameOrConcept);
  const rec = name ? ICONS[name] : null;
  let illo: Illustration | null = null;
  if (rec && Array.isArray(rec.p)) {
    const strokes: Stroke[] = [];
    for (const [tag, attrs] of rec.p) {
      const d = nodeToD(tag, attrs || {});
      if (d) strokes.push({ d });
    }
    if (strokes.length) illo = { viewBox: VIEWBOX, strokes };
  }
  cache.set(nameOrConcept, illo);
  return illo;
}
