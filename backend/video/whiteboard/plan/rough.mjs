// Rough.js hand-drawn STYLE PASS (Guide-2 §H). Runs in Node at resolve time (deterministic via a
// per-element seed → NO per-frame flicker), bakes wobbly path `d` strings into the resolved plan;
// the Remotion composition just self-draws them. roughjs is therefore a WORKER dep, never in the
// browser bundle. Opt-in: only runs when plan.style_pass.mode === "rough" (default stays clean).
import rough from "roughjs";

const gen = rough.generator();

// concat every stroke sub-path of a rough drawable into one `d`
function drawableToD(drawable) {
  return gen.toPaths(drawable).filter((p) => !p.fill || p.fill === "none").map((p) => p.d).join(" ");
}

// a wobbly rounded-ish rect in LOCAL card coords (0..w, 0..h), inset by the stroke so it stays inside
export function roughBorderPath(w, h, { seed = 1, roughness = 1.3, bowing = 1.1, strokeWidth = 4 } = {}) {
  const inset = Math.max(2, strokeWidth);
  const d = gen.rectangle(inset, inset, Math.max(1, w - inset * 2), Math.max(1, h - inset * 2),
    { roughness, bowing, seed });
  return drawableToD(d);
}

// a wobbly line in CANVAS coords (for the connector shaft)
export function roughLinePath(x1, y1, x2, y2, { seed = 1, roughness = 1.3, bowing = 1.4 } = {}) {
  return drawableToD(gen.line(x1, y1, x2, y2, { roughness, bowing, seed }));
}

// Mutate a RESOLVED plan in place: attach `_roughBorder` to each diagram element and `_roughShaft`
// to each connector, and flag plan.rough = true. No-op for non-diagram plans.
export function roughenResolved(plan, { seed = 7 } = {}) {
  if (!plan || (plan.mode !== "diagram")) return plan;
  const sp = plan.style_pass || {};
  const roughness = Number.isFinite(sp.roughness) ? sp.roughness : 1.3;
  const bowing = Number.isFinite(sp.bowing) ? sp.bowing : 1.1;
  const sw = plan.stylePack?.stroke?.width || 4;
  (plan.elements || []).forEach((el, i) => {
    if (!el.box) return;
    el._roughBorder = roughBorderPath(el.box.w, el.box.h, { seed: seed + i * 13, roughness, bowing, strokeWidth: sw });
  });
  (plan.connectors || []).forEach((c, i) => {
    const ax = c.from.x, ay = c.from.y, bx = c.to.x, by = c.to.y;
    const dx = bx - ax, dy = by - ay, len = Math.hypot(dx, dy) || 1, ux = dx / len, uy = dy / len;
    const horiz = Math.abs(ux) >= Math.abs(uy);
    const halfA = (horiz ? (c.from.w || 300) / 2 : (c.from.h || 300) / 2) + 18;
    const halfB = (horiz ? (c.to.w || 300) / 2 : (c.to.h || 300) / 2) + 18;
    const sx = ax + ux * halfA, sy = ay + uy * halfA, ex = bx - ux * halfB, ey = by - uy * halfB;
    c._roughShaft = roughLinePath(sx, sy, ex, ey, { seed: seed + 100 + i * 7, roughness, bowing: 1.5 });
    c._shaftEnd = { ex, ey, ux, uy }; // arrowhead anchor so the composition keeps a crisp head
  });
  plan.rough = true;
  return plan;
}
