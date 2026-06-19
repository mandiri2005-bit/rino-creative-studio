// Measures a path in the browser (Remotion renders in real Chromium) so the
// following-hand can sit on the current draw point. Uses a detached <path>, cached
// per `d`, and getPointAtLength — no need to mount the real path or read a ref.

const cache = new Map<string, SVGPathElement | null>();

function measured(d: string): SVGPathElement | null {
  if (typeof document === "undefined") return null; // not during Node metadata phase
  if (cache.has(d)) return cache.get(d) ?? null;
  let el: SVGPathElement | null = null;
  try {
    el = document.createElementNS("http://www.w3.org/2000/svg", "path");
    el.setAttribute("d", d);
    void el.getTotalLength(); // throws on bad path -> null
  } catch {
    el = null;
  }
  cache.set(d, el);
  return el;
}

// Total length of a path (viewBox units). 0 if unmeasurable. Used to allocate draw
// time across an illustration's strokes proportionally to their length.
export function lengthOf(d: string): number {
  const el = measured(d);
  if (!el) return 0;
  try {
    return el.getTotalLength();
  } catch {
    return 0;
  }
}

// Point (in viewBox coords) at a 0..1 fraction along the path. null if unmeasurable.
export function pointAtProgress(
  d: string,
  progress: number
): { x: number; y: number } | null {
  const el = measured(d);
  if (!el) return null;
  let len = 0;
  try {
    len = el.getTotalLength();
  } catch {
    return null;
  }
  if (!len) return null;
  const clamped = Math.max(0, Math.min(1, progress));
  const p = el.getPointAtLength(clamped * len);
  return { x: p.x, y: p.y };
}
