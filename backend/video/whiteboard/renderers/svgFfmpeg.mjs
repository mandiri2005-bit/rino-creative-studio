// renderers/svgFfmpeg.mjs — Chromium-free render backend (Guide-2 §K).
//
// Renders a RESOLVED whiteboard plan (the SAME geometry resolvePlan feeds Remotion → consistent
// layout) to MP4 WITHOUT Chromium: build one SVG per frame (draw-on via stroke-dashoffset +
// opacity, camera transform, rough pass, raster-reveal wipe) → rasterize each to PNG with
// @resvg/resvg-js → ffmpeg the sequence. renderWhiteboardPlanSvg mirrors render.mjs's
// renderWhiteboardPlan (per-scene MP4 + audio → concat) but needs no browser.
//
// Opt-in: the dispatcher / WB_RENDER_BACKEND routes here; Remotion stays the default (roadmap §B).
import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync, copyFileSync } from "node:fs";
import { tmpdir, cpus } from "node:os";
import { join, dirname, extname } from "node:path";
import { resolvePlan } from "../plan/resolvePlan.mjs";
import { validateResolvedScene } from "../qa.mjs";

// keep these in sync with render.mjs (duplicated so this stays Chromium/@remotion-free)
const ASPECT = { "16:9": [1920, 1080], "9:16": [1080, 1920], "1:1": [1080, 1080], "4:5": [1080, 1350] };
// fps: WB drawing doesn't need 30 — 24 cuts ~20% of frames (= ~20% faster CPU-bound raster) with
// no perceptible loss on a whiteboard reveal. Env WB_FPS overrides (set 30 to revert). hd_plus stays
// 60 (premium smoothness). Audio is muxed by seconds → fps change can't desync it.
const WB_FPS = Math.max(1, Number(process.env.WB_FPS) || 24);
const TIER = { fast: { fps: WB_FPS, crf: 26 }, hd: { fps: WB_FPS, crf: 20 }, hd_plus: { fps: 60, crf: 18 } }; // crf lowered: crisper line-art (less "pixelated" during the draw)
const GENRE_MODE = { diagram: "diagram", detail: "raster", lineart: "icons", color: "color" };

let _roughen = null;
try { ({ roughenResolved: _roughen } = await import("../plan/rough.mjs")); } catch { /* roughjs optional */ }

const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
const lerp = (a, b, t) => a + (b - a) * t;
const progress = (t, sf, df, fps) => clamp((t - sf / fps) / Math.max(0.001, df / fps), 0, 1);
// labels: same humanist sans the Linux worker (+ Chromium) fall back to → parity, no bundled font
const FONT_STACK = "Inter, 'Liberation Sans', 'DejaVu Sans', 'Noto Sans', Arial, sans-serif";
// breath after a scene's narration; also absorbs sub-frame rounding so -shortest never clips the
// last phoneme. The per-scene video window is extended to (narration + this) when the narration is
// longer than the planned window → audio is NEVER truncated (fixes "kepotong di awal scene N").
const AUDIO_TAIL_PAD = Number(process.env.WB_AUDIO_TAIL_PAD || 0.12);

// Render a node label as WRAPPED lines that fit `maxW` px (≈ the node's own width) so long labels in
// a tight timeline/flow don't overflow into the neighbour's label ("diagram alur nimpa2" — Rino).
// One line if it fits; else word-wrap to ≤2 lines (overflow merged into line 2); shrink the font if a
// single word still overflows. Lines stack DOWNWARD from baseY (stays below the node).
function labelSvg(text, cx, baseY, maxW, { fontSize = 34, weight = 800, fill = "#1F2937", opacity = 1 } = {}) {
  const s = String(text || "").trim();
  if (!s) return "";
  const maxChars = Math.max(6, Math.floor(maxW / (fontSize * 0.55)));
  const T = (y, fs, str) => `<text x="${cx}" y="${y.toFixed(1)}" text-anchor="middle" font-family="${FONT_STACK}" font-weight="${weight}" font-size="${fs}" fill="${fill}" opacity="${opacity}">${esc(str)}</text>`;
  if (s.length <= maxChars) return T(baseY, fontSize, s);
  const words = s.split(/\s+/); const lines = []; let cur = "";
  for (const w of words) {
    if (!cur) cur = w;
    else if ((cur + " " + w).length <= maxChars) cur += " " + w;
    else { lines.push(cur); cur = w; }
  }
  if (cur) lines.push(cur);
  if (lines.length > 2) lines.splice(1, lines.length - 1, lines.slice(1).join(" ")); // merge overflow → line 2
  const longest = Math.max(...lines.map((l) => l.length), 1);
  const fs = longest > maxChars ? Math.max(20, Math.floor(fontSize * maxChars / longest)) : fontSize;
  const lh = fs * 1.12;
  return lines.map((l, i) => T(baseY + i * lh, fs, l)).join("");
}

// pure-JS point-at-progress along a path `d` (Chromium-free replacement for getPointAtLength),
// cached per `d`. Drives the marker hand that follows the pen.
let _pathProps = null;
const _ptCache = new Map();
async function ensurePathLib() { if (!_pathProps) ({ svgPathProperties: _pathProps } = await import("svg-path-properties")); }
function pointOnPath(d, frac) {
  try {
    let p = _ptCache.get(d);
    if (!p) { p = new _pathProps(d); _ptCache.set(d, p); }
    const len = p.getTotalLength();
    if (!len) return null;
    return p.getPointAtLength(clamp(frac, 0, 1) * len);
  } catch { return null; }
}
// actual path length (resvg ignores pathLength, so dash-trace needs real lengths). Cached per `d`.
const _lenCache = new Map();
function pathLen(d) {
  let L = _lenCache.get(d);
  if (L == null) {
    try { let p = _ptCache.get(d); if (!p) { p = new _pathProps(d); _ptCache.set(d, p); } L = p.getTotalLength() || 100; }
    catch { L = 100; }
    _lenCache.set(d, L);
  }
  return L;
}
// vertical centre of a path (sampled) → time each region's draw by WHERE it is, so big shapes draw
// at their position (smooth top→bottom) instead of dumping early. Cached per `d`.
const _cyCache = new Map();
function centroidY(d) {
  let y = _cyCache.get(d);
  if (y == null) {
    try {
      let p = _ptCache.get(d); if (!p) { p = new _pathProps(d); _ptCache.set(d, p); }
      const L = p.getTotalLength() || 1; let s = 0, c = 0;
      for (const f of [0.1, 0.3, 0.5, 0.7, 0.9]) { const pt = p.getPointAtLength(f * L); if (pt) { s += pt.y; c++; } }
      y = c ? s / c : 0;
    } catch { y = 0; }
    _cyCache.set(d, y);
  }
  return y;
}

// the marker hand (forearm + fist + pen), nib at the draw point — ported from components/Hand.tsx
// (the HAND_IMAGE=null inline branch the plan composition uses). px,py = canvas pen-tip; size px.
function handSvg(px, py, size, nib) {
  const s = size / 256, ox = px - size * 0.03, oy = py - size * 0.03;
  return `<g transform="translate(${ox.toFixed(1)} ${oy.toFixed(1)}) scale(${s.toFixed(4)})">`
    + `<path d="M132 150 C165 180 195 205 225 228 C238 238 256 242 256 256 L150 256 C128 244 116 212 119 180 C121 165 125 156 132 150 Z" fill="#E6B089"/>`
    + `<path d="M256 256 L150 256 C176 240 192 214 201 193 L256 220 Z" fill="#3F6FB2"/>`
    + `<path d="M104 110 C108 92 132 86 148 96 C166 86 188 98 190 120 C206 128 209 154 193 166 C189 190 159 203 134 192 C112 199 92 180 94 158 C86 146 90 122 104 110 Z" fill="#E6B089"/>`
    + `<path d="M100 118 C86 112 75 126 85 140 C93 151 110 149 116 136 C114 126 108 120 100 118 Z" fill="#E6B089"/>`
    + `<path d="M120 116 C134 108 152 112 164 124" stroke="#C98F66" stroke-width="2.5" fill="none" stroke-linecap="round"/>`
    + `<path d="M116 134 C132 127 150 130 162 140" stroke="#C98F66" stroke-width="2.5" fill="none" stroke-linecap="round"/>`
    + `<path d="M21 11 L156 146 L146 156 L11 21 Z" fill="#303030"/>`
    + `<path d="M31 19 L150 138" stroke="#5a5a5a" stroke-width="2" stroke-linecap="round"/>`
    + `<path d="M21 11 L7 7 L11 21 Z" fill="${nib}"/></g>`;
}

// camera: mirror WhiteboardPlan.tsx cameraTransform, expressed as an SVG group transform
// (CSS translate+scale around centre → translate(W/2+dx,H/2+dy) scale(s) translate(-W/2,-H/2)).
function cameraSvg(camera, frame, fps, W, H) {
  const c0x = W / 2, c0y = H / 2;
  const stateOf = (c) => ({ dx: c0x - c.cx, dy: c0y - c.cy, s: c.scale || 1 });
  let st = { dx: 0, dy: 0, s: 1 };
  const active = (camera || []).find((c) => frame >= c.startFrame && frame <= c.endFrame);
  if (active) {
    const prev = [...camera].filter((c) => c.endFrame <= active.startFrame).pop();
    const from = prev ? stateOf(prev) : { dx: 0, dy: 0, s: 1 };
    const to = stateOf(active);
    const t = ease(clamp((frame - active.startFrame) / Math.max(1, active.endFrame - active.startFrame), 0, 1));
    st = { dx: lerp(from.dx, to.dx, t), dy: lerp(from.dy, to.dy, t), s: lerp(from.s, to.s, t) };
  } else {
    const past = [...(camera || [])].filter((c) => frame > c.endFrame).pop();
    if (past) st = stateOf(past);
  }
  return `translate(${(W / 2 + st.dx).toFixed(2)} ${(H / 2 + st.dy).toFixed(2)}) scale(${st.s.toFixed(4)}) translate(${-W / 2} ${-H / 2})`;
}

// One frame as an SVG string. Pure → unit-testable without a rasterizer.
export function buildSceneSvg(plan, frame, fps = 30) {
  const t = frame / fps;
  const { width: W, height: H } = plan.canvas;
  const pack = plan.stylePack || {};
  const ink = pack.palette?.ink || "#1A1A1A";
  const accent = pack.palette?.accent || "#2C6CA8";
  const board = pack.board || "#FBFBF7";
  const sw = pack.stroke?.width || 4;
  const diagram = (plan.mode || "icons") === "diagram";
  const out = [`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">`,
    `<rect width="${W}" height="${H}" fill="${board}"/>`,
    `<g transform="${cameraSvg(plan.camera, frame, fps, W, H)}">`];
  let clipId = 0;
  const hands = []; // marker-hand draws, collected then rendered on top (inside the camera g)

  // arrows (under cards): rough shaft if present, else straight; draw-on via dashoffset
  for (const c of plan.connectors || []) {
    const p = progress(t, c.startFrame, c.durFrames, fps);
    if (p <= 0) continue;
    const ax = c.from.x, ay = c.from.y, bx = c.to.x, by = c.to.y;
    const dx = bx - ax, dy = by - ay, len = Math.hypot(dx, dy) || 1, ux = dx / len, uy = dy / len;
    const horiz = Math.abs(ux) >= Math.abs(uy);
    const ha = (horiz ? (c.from.w || 300) / 2 : (c.from.h || 300) / 2) + 18;
    const hb = (horiz ? (c.to.w || 300) / 2 : (c.to.h || 300) / 2) + 18;
    const sx = ax + ux * ha, sy = ay + uy * ha, ex = bx - ux * hb, ey = by - uy * hb;
    const ah = 22;
    const lx = ex - ah * (ux - uy * 0.6), ly = ey - ah * (uy + ux * 0.6);
    const rx = ex - ah * (ux + uy * 0.6), ry = ey - ah * (uy - ux * 0.6);
    const shaft = c._roughShaft || `M ${sx} ${sy} L ${ex} ${ey}`;
    const d = `${shaft} M ${ex} ${ey} L ${lx} ${ly} M ${ex} ${ey} L ${rx} ${ry}`;
    const L = (Math.hypot(ex - sx, ey - sy) + ah * 2) * (c._roughShaft ? 1.6 : 1) + 1;
    out.push(`<path d="${d}" fill="none" stroke="${accent}" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="${L}" stroke-dashoffset="${(1 - p) * L}"/>`);
  }

  const els = plan.elements || [];
  for (let ei = 0; ei < els.length; ei++) {
    const el = els[ei];
    const p = progress(t, el.draw.startFrame, el.draw.durFrames, fps);
    if (p <= 0) continue;
    const b = el.box; if (!b) continue;
    const x = b.x - b.w / 2, y = b.y - b.h / 2;

    if (diagram) {
      if (el._roughBorder) {
        const L = (b.w + b.h) * 2.4;
        out.push(`<rect x="${x}" y="${y}" width="${b.w}" height="${b.h}" rx="24" fill="${accent}12" opacity="${clamp(p * 2, 0, 1)}"/>`);
        out.push(`<g transform="translate(${x} ${y})"><path d="${el._roughBorder}" fill="none" stroke="${accent}" stroke-width="3" stroke-dasharray="${L}" stroke-dashoffset="${(1 - p) * L}"/></g>`);
      } else {
        out.push(`<rect x="${x}" y="${y}" width="${b.w}" height="${b.h}" rx="24" fill="${accent}12" stroke="${accent}" stroke-width="3" opacity="${clamp(p * 2, 0, 1)}"/>`);
      }
      // numbered badge (matches the Remotion card): accent circle, white index, top-left corner
      out.push(`<g opacity="${clamp(p * 2, 0, 1)}"><circle cx="${x}" cy="${y}" r="24" fill="${accent}"/><text x="${x}" y="${y + 9}" text-anchor="middle" font-family="${FONT_STACK}" font-weight="800" font-size="26" fill="#fff">${ei + 1}</text></g>`);
    }

    const iconH = b.h * (diagram ? 0.48 : 0.9), iconTop = diagram ? b.h * 0.16 : 0;

    if (el.raster) {
      // raster-reveal (mirror RasterRevealIllustration): the real photo revealed through a
      // self-drawing vector MASK, units ordered in a top→bottom reading SNAKE, hand on the frontier.
      const [mvx = 0, mvy = 0, mvw = 100, mvh = 100] = String(el.maskViewBox || "0 0 100 100").split(/\s+/).map(Number);
      const ms = Math.min(b.w / mvw, iconH / mvh);
      const mgx = x + (b.w - mvw * ms) / 2, mgy = y + iconTop + (iconH - mvh * ms) / 2;
      const rep = (d) => { const m = /[Mm]\s*(-?[\d.]+)[\s,]+(-?[\d.]+)/.exec(d); return m ? { x: +m[1], y: +m[2] } : { x: mvx + mvw / 2, y: mvy + mvh / 2 }; };
      // units in DRAW ORDER — maskShapes are pre-ordered NEAREST-NEIGHBOUR by traceMaskB64, so the pen
      // moves continuously between adjacent forms (natural). No re-sort.
      const units = [
        ...(el.maskShapes || []).map((s) => ({ ...rep(s.d), el: "shape", d: s.d })),
        ...(el.maskStrokes || []).map((s) => ({ ...rep(s.d), el: "stroke", d: s.d })),
      ];
      if (units.length === 0) {
        // NO mask → reveal the FULL image with a left→right wipe so the paid raster is never lost.
        const id = `rw${clipId++}`, rw = Math.round(b.w * p);
        out.push(`<clipPath id="${id}"><rect x="${x}" y="${y + iconTop}" width="${rw}" height="${iconH}"/></clipPath>`);
        out.push(`<image href="${el.raster}" x="${x}" y="${y + iconTop}" width="${b.w}" height="${iconH}" preserveAspectRatio="xMidYMid meet" clip-path="url(#${id})"/>`);
        if (el.label) {
          const ly2 = diagram ? y + b.h * 0.68 + 28 : y + b.h + 34;
          out.push(labelSvg(el.label, b.x, ly2, b.w, { fontSize: pack.font?.labelSize || 34, weight: pack.font?.weight || 800, fill: ink, opacity: clamp((p - 0.4) * 2, 0, 1) }));
        }
        continue;
      }
      // GOLPO-STYLE draw: forms are drawn ONE AFTER ANOTHER along a nearest-neighbour path — for each,
      // the pen TRACES its ink outline then its colour fills. The pen rides the actual line being
      // drawn (pointOnPath), so it follows the artwork like a hand — NOT a mechanical sweep.
      const SPAN = 0.92, N = units.length;
      const inkW = Math.max(1.6, mvw / 340);
      // each form's draw TIME ∝ its outline length → big forms take longer (natural) + EVEN area
      // pacing (no front-loading from a few big shapes filling the canvas early).
      const lens = units.map((u) => pathLen(u.d));
      const totLen = lens.reduce((a, c) => a + c, 0) || 1;
      let _cum = 0; const startF = lens.map((l) => { const s = (_cum / totLen) * SPAN; _cum += l; return s; });
      const durF = lens.map((l) => Math.max(0.012, (l / totLen) * SPAN) * 1.6); // *1.6 = slight overlap
      const spOf = (i) => clamp((p - startF[i]) / durF[i], 0, 1);
      const id = `rv${clipId++}`;
      out.push(`<g transform="translate(${mgx} ${mgy}) scale(${ms})">`);
      out.push(`<mask id="${id}" maskUnits="userSpaceOnUse">`);
      // catch-up band trails the draw → fills LIGHT regions trace misses (pale koala), so nothing
      // stays empty; the per-form fills give the leading edge near the pen.
      const catchH = Math.max(0, clamp(p / SPAN, 0, 1) - 0.15) * mvh;
      if (catchH > 0) out.push(`<rect x="${mvx}" y="${mvy}" width="${mvw}" height="${catchH.toFixed(1)}" fill="white"/>`);
      units.forEach((u, i) => { const op = clamp((spOf(i) - 0.4) / 0.6, 0, 1); if (op > 0) out.push(`<path d="${u.d}" fill="white" opacity="${op.toFixed(3)}"/>`); });
      out.push(`</mask><image href="${el.raster}" x="${mvx}" y="${mvy}" width="${mvw}" height="${mvh}" preserveAspectRatio="xMidYMid meet" mask="url(#${id})"/>`);
      units.forEach((u, i) => {
        const sp = spOf(i); if (sp <= 0) return;
        const traceP = clamp(sp / 0.5, 0, 1);
        const L = pathLen(u.d);
        out.push(`<path d="${u.d}" fill="none" stroke="${ink}" stroke-width="${inkW}" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="${L.toFixed(1)}" stroke-dashoffset="${((1 - traceP) * L).toFixed(1)}" opacity="0.82"/>`);
      });
      out.push(`</g>`);
      // pen rides the LEADING form being traced (real point on its line) → follows the NN path
      if (p > 0.005 && p < 0.985) {
        let act = -1;
        for (let i = 0; i < N; i++) { const sp = spOf(i); if (sp > 0.01 && sp < 0.99) act = i; }
        if (act >= 0) { const u = units[act]; const pt = pointOnPath(u.d, clamp(spOf(act) / 0.5, 0, 1)) || { x: u.x, y: u.y }; hands.push({ x: mgx + pt.x * ms, y: mgy + pt.y * ms, size: Math.max(120, iconH * 0.5), nib: ink }); }
      }
    } else {
      // color genre: soft colour chip behind the icon (icon stroke is the same colour)
      if (el.chip) {
        out.push(`<rect x="${(x + b.w * 0.16).toFixed(1)}" y="${(y + iconTop + iconH * 0.02).toFixed(1)}" width="${(b.w * 0.68).toFixed(1)}" height="${(iconH * 0.96).toFixed(1)}" rx="28" fill="${el.chip}22" opacity="${clamp(p * 2, 0, 1)}"/>`);
      }
      // vector icon: filled shapes (Recraft/color/phosphor) under self-draw strokes that draw
      // SEQUENTIALLY (match Remotion: stroke i over [start+i·per, +per]); the hand rides the active one.
      const [, , vbw = 100, vbh = 100] = String(el.viewBox || "0 0 100 100").split(/\s+/).map(Number);
      const scale = Math.min(b.w / vbw, iconH / vbh);
      const gx = x + (b.w - vbw * scale) / 2, gy = y + iconTop + (iconH - vbh * scale) / 2;
      out.push(`<g transform="translate(${gx} ${gy}) scale(${scale})">`);
      // DRAW every unit sequentially + hand on the active one. FILL icons (Phosphor / Iconify /
      // Recraft) used to just fade in (opacity=p) → looked "revealed", not drawn. Now a fill shape
      // TRACES its outline (dashoffset) then fills in over the back half of its window — so iconify
      // icons self-draw like the Lucide/Tabler line icons instead of popping.
      const units = [
        ...(el.shapes || []).map((s) => ({ ...s, kind: "shape" })),
        ...(el.strokes || []).map((s) => ({ ...s, kind: "stroke" })),
      ];
      const n = Math.max(1, units.length), per = el.draw.durFrames / n;
      units.forEach((s, si) => {
        const ts = progress(t, el.draw.startFrame + si * per, per, fps);
        if (ts <= 0) return;
        // REAL outline length, not a fixed 1200 — resvg ignores pathLength, so a 1200 dash on a vb24
        // iconify path (~100 units long) showed the whole outline INSTANTLY ("already drawn"). With the
        // true length the dash-offset traces it progressively → the pen actually DRAWS the icon (incl.
        // a compound fill path: its subpaths trace in sequence), like Lucide's strokes.
        const L = pathLen(s.d);
        if (s.kind === "shape") {
          // A fill icon draws as a thin BLACK outline (linework) + a visible COLOUR fill — "garis hitam
          // + warna", not a solid recoloured blob. col = the fill colour (palette for mono, own colour
          // for multi-colour). Outline width is viewBox-NORMALISED + a touch thinner (Rino: "garis
          // hitamnya dikecilin") — sw=4 is for vb100, so a raw 4 on a vb24 icon would fill the shape.
          const col = s.fill && s.fill !== "none" ? s.fill : ink;
          const ow = Math.max(0.5, vbw * 3 / 100);
          out.push(`<path d="${s.d}" fill="${col}" fill-opacity="${(clamp((ts - 0.5) * 2, 0, 1) * 0.55).toFixed(3)}" stroke="${ink}" stroke-width="${ow.toFixed(2)}" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="${L}" stroke-dashoffset="${((1 - ts) * L).toFixed(1)}"/>`);
        } else {
          out.push(`<path d="${s.d}" fill="none" stroke="${s.stroke || ink}" stroke-width="${s.width || sw}" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="${L}" stroke-dashoffset="${((1 - ts) * L).toFixed(1)}"/>`);
        }
        if (ts > 0.02 && ts < 0.985) {
          const pt = pointOnPath(s.d, ts);
          if (pt) hands.push({ x: gx + pt.x * scale, y: gy + pt.y * scale, size: Math.max(90, iconH * 0.55), nib: (s.kind === "shape" ? (s.fill && s.fill !== "none" ? s.fill : ink) : (s.stroke || ink)) });
        }
      });
      out.push(`</g>`);
    }

    if (el.label) {
      const ly = diagram ? y + b.h * 0.68 + 28 : y + b.h + 34;
      out.push(labelSvg(el.label, b.x, ly, b.w, { fontSize: pack.font?.labelSize || 34, weight: pack.font?.weight || 800, fill: ink, opacity: clamp((p - 0.4) * 2, 0, 1) }));
    }
  }
  // overlays (highlight circles)
  for (const ov of plan.overlays || []) {
    const p = progress(t, ov.startFrame, ov.durFrames, fps);
    if (p <= 0) continue;
    const rxr = (ov.box.w / 2 + 18), ryr = (ov.box.h / 2 + 18), L = Math.PI * 2 * Math.max(rxr, ryr);
    out.push(`<ellipse cx="${ov.box.x}" cy="${ov.box.y}" rx="${rxr}" ry="${ryr}" fill="none" stroke="${pack.palette?.highlight || accent}" stroke-width="${sw + 1}" stroke-dasharray="${L}" stroke-dashoffset="${(1 - p) * L}"/>`);
  }
  for (const h of hands) out.push(handSvg(h.x, h.y, h.size, h.nib)); // marker hand(s) on top
  out.push(`</g></svg>`);
  return out.join("");
}

// Lazy rasterizer: @resvg/resvg-js → sharp → rsvg-convert CLI.
async function rasterizeSvg(svg, width, outPng) {
  try {
    const { Resvg } = await import("@resvg/resvg-js");
    // load the worker's system fonts (fonts-dejavu-core/liberation/noto) so labels render in the
    // same humanist sans Chromium falls back to → font parity without bundling a font file.
    const opts = { fitTo: { mode: "width", value: width }, font: { loadSystemFonts: true, defaultFontFamily: "DejaVu Sans" } };
    writeFileSync(outPng, new Resvg(svg, opts).render().asPng());
    return;
  } catch (e) { if (!/Cannot find|ERR_MODULE/.test(String(e.message))) throw e; }
  try {
    const sharp = (await import("sharp")).default;
    await sharp(Buffer.from(svg)).png().toFile(outPng); return;
  } catch (e) { if (!/Cannot find|ERR_MODULE/.test(String(e.message))) throw e; }
  await new Promise((res, rej) => {
    const tmp = outPng + ".svg"; writeFileSync(tmp, svg);
    const p = spawn("rsvg-convert", ["-w", String(width), "-o", outPng, tmp]);
    p.on("error", rej); p.on("close", (c) => (c === 0 ? res() : rej(new Error(`no rasterizer (install @resvg/resvg-js / sharp / librsvg)`))));
  });
}

// Rasterize a list of {svg,outPng} frames. PARALLEL via a bounded worker pool when worthwhile, else
// sequential. Output is identical either way (resvg is deterministic). Bounded by WB_RASTER_CONCURRENCY
// (default ~min(4, cores-2)) so concurrent renders don't oversubscribe the worker's CPUs (the encoder-
// semaphore lesson). ANY pool failure → fall back to the proven sequential path (never breaks a render).
async function rasterizeMany(jobs, width) {
  const def = Math.min(4, Math.max(2, (cpus().length || 4) - 2));
  const K = Math.max(1, Number(process.env.WB_RASTER_CONCURRENCY || def));
  if (K <= 1 || jobs.length < 2) { for (const j of jobs) await rasterizeSvg(j.svg, width, j.outPng); return; }
  try {
    const { Worker } = await import("node:worker_threads");
    const workerUrl = new URL("./raster-worker.mjs", import.meta.url);
    const pool = Array.from({ length: Math.min(K, jobs.length) }, () => new Worker(workerUrl));
    let next = 0, done = 0;
    await new Promise((resolve, reject) => {
      const feed = (w) => { if (next < jobs.length) { const j = jobs[next++]; w.postMessage({ svg: j.svg, width, outPng: j.outPng }); } };
      for (const w of pool) {
        w.on("message", (m) => { if (!m.ok) return reject(new Error(m.err)); if (++done === jobs.length) resolve(); else feed(w); });
        w.on("error", reject);
        // a worker that dies SILENTLY (e.g. native segfault) emits 'exit' without 'message' → would
        // otherwise hang the pool forever. Reject on any abnormal exit before all frames are done →
        // the catch below re-renders the rest sequentially. (never hang a render)
        w.on("exit", (code) => { if (code !== 0 && done < jobs.length) reject(new Error(`raster worker exited ${code}`)); });
      }
      for (const w of pool) feed(w);
    }).finally(async () => { await Promise.all(pool.map((w) => w.terminate().catch(() => {}))); });
  } catch (e) {
    console.warn(`[wb-svg] parallel raster fell back to sequential: ${e.message}`);
    for (const j of jobs) await rasterizeSvg(j.svg, width, j.outPng);
  }
}

const ff = (args) => new Promise((res, rej) => {
  const p = spawn("ffmpeg", args);
  let err = "";
  p.stderr.on("data", (d) => { err += d; });
  p.on("error", rej); p.on("close", (c) => (c === 0 ? res() : rej(new Error(`ffmpeg ${c}: ${err.slice(-300)}`))));
});

// probe a media file's duration (seconds) — resolves null on ANY failure (never throws), so a
// missing/racing audio file just falls back to the planned window instead of breaking the render.
const ffprobeDur = (file) => new Promise((res) => {
  const p = spawn("ffprobe", ["-v", "error", "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1", file]);
  let out = "";
  p.stdout.on("data", (d) => (out += d));
  p.on("error", () => res(null));
  p.on("close", () => { const v = parseFloat(String(out).trim()); res(Number.isFinite(v) ? v : null); });
});

// Render ONE resolved scene → MP4 (frames → rasterize → ffmpeg, optional audio).
export async function renderSceneSvgFfmpeg(plan, outMp4, { fps = 30, crf = 23, audioPath = null } = {}) {
  const planFrames = plan.durationInFrames || Math.round((plan.duration || 4) * fps);
  // The video window MUST cover the actual narration. Measure the very file we're about to mux and
  // EXTEND the window (freeze the finished board for the extra frames) when the narration is longer
  // than the planned window. Defense-in-depth: correct even when upstream sc.duration under-estimated
  // (durationActual → estSeconds fallback was clipping the scene tail via -shortest). Only EXTENDS,
  // never shrinks, so a correctly-sized scene is untouched. (Rino: "audio kepotong di awal scene 9")
  let frames = planFrames;
  if (audioPath) {
    const audioLen = await ffprobeDur(audioPath);
    if (Number.isFinite(audioLen) && audioLen > 0) {
      const need = Math.ceil((audioLen + AUDIO_TAIL_PAD) * fps);
      if (need > frames) frames = need;
    }
  }
  await ensurePathLib().catch(() => {}); // for the following-hand pen tip (graceful if absent)
  const dir = mkdtempSync(join(tmpdir(), "wbsvg-"));
  try {
    // Build each frame's SVG on the main thread (cheap, deterministic) and DEDUP: a frame whose SVG is
    // byte-identical to the previous one (the freeze tail once all draws/camera finish — especially the
    // audio-extension frames) is COPIED, not re-rasterized. The remaining UNIQUE frames are rasterized
    // in PARALLEL via a bounded worker pool (resvg is CPU-bound + synchronous → plain async can't
    // parallelise). Output is byte-identical to the old sequential path (resvg is deterministic) — this
    // only makes producing the PNGs faster, and falls back to sequential on any pool issue.
    const pad = (f) => join(dir, `f${String(f).padStart(5, "0")}.png`);
    const renders = []; // unique frames to rasterize: {svg, outPng}
    const copies = [];  // dedup'd frames: {from, to}
    let prevSvg = null, prevPng = null;
    for (let f = 0; f < frames; f++) {
      const svg = buildSceneSvg(plan, f, fps);
      const out = pad(f);
      if (svg === prevSvg) { copies.push({ from: prevPng, to: out }); }
      else { renders.push({ svg, outPng: out }); prevSvg = svg; prevPng = out; }
    }
    await rasterizeMany(renders, plan.canvas.width);    // parallel pool (+ sequential fallback)
    for (const c of copies) copyFileSync(c.from, c.to); // freeze frames: cheap file copy, no resvg
    // every scene mp4 carries an audio track (real TTS, or a silent one) so the concat is uniform
    const args = ["-y", "-framerate", String(fps), "-i", join(dir, "f%05d.png")];
    if (audioPath) args.push("-i", audioPath);
    else args.push("-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100");
    // afade-in (10ms): ramp each scene's narration ONSET so the concat seam (previous scene's apad
    // silence → this scene's first sample) has no amplitude STEP → kills the residual boundary click
    // ("audio patah sedikit"), with NO length change and NO A/V drift (unlike acrossfade, which shifts
    // every later scene earlier and desyncs the drawing). apad then pads trailing silence so -shortest
    // trims the audio to the EXACT (now audio-covering) video length → narration never truncated, and
    // the half-frame video/audio quantisation drift stays absorbed. 44.1k stereo → uniform concat input.
    args.push("-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", String(crf),
      "-af", "afade=t=in:st=0:d=0.01,apad", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest", outMp4);
    await ff(args);
    return { path: outMp4, frames };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

// Full video: mirror render.mjs renderWhiteboardPlan but via SVG/FFmpeg. Resolves each scene's
// plan (same resolvePlan + rough pass), renders per-scene MP4 (+audio), concats → ONE MP4.
export async function renderWhiteboardPlanSvg(scenes, meta, outPath, opts = {}) {
  const [width, height] = ASPECT[meta.aspectRatio] || ASPECT["16:9"];
  const tier = TIER[meta.tier] || TIER.hd;
  const fps = tier.fps;
  const assetsDir = opts.assetsDir || join(dirname(new URL(import.meta.url).pathname), "..", "assets", "whiteboard");
  const work = mkdtempSync(join(tmpdir(), "wbplan-svg-"));
  const sceneMp4s = [];
  let totalFrames = 0;
  try {
    for (let i = 0; i < scenes.length; i++) {
      const sc = scenes[i];
      const sceneDur = Math.max(0.5, Number(sc.duration) || 0.5);
      let plan = null;
      try {
        if (sc.planJson) {
          const raw = typeof sc.planJson === "string" ? JSON.parse(sc.planJson) : sc.planJson;
          const mode = GENRE_MODE[meta.whiteboardGenre] || "icons";
          // pass the REAL canvas (incl. portrait 9:16) INTO resolvePlan so its layout is aspect-aware
          // (the VD plan hardcodes canvas 1920x1080; for 16:9 this is identical → no behaviour change).
          plan = resolvePlan(rescaleTiming({ ...raw, mode, canvas: { width, height } }, sceneDur), { assetsDir, fps, strict: false });
          plan.canvas = { width, height };
          const roughMode = (process.env.WB_STYLE || "").toLowerCase() === "rough" || raw.style_pass?.mode === "rough";
          if (roughMode && plan.mode === "diagram" && _roughen) {
            try { plan.style_pass = { mode: "rough", ...(raw.style_pass || {}) }; _roughen(plan); } catch { /* clean */ }
          }
        }
      } catch (e) {
        console.warn(`[whiteboard-plan-svg ${meta.jobId || ""}/${i}] resolve failed: ${e.message} → blank`);
        plan = null;
      }
      if (!plan) {
        const fd = Math.max(1, Math.round(sceneDur * fps));
        const txt = String(sc.text || sc.visualPrompt || "").trim().split(/\s+/).slice(0, 14).join(" ");
        plan = { fps, duration: sceneDur, durationInFrames: fd, canvas: { width, height }, mode: "icons",
          elements: txt ? [{ id: "fallback", type: "text", box: { x: Math.round(width / 2), y: Math.round(height / 2), w: Math.round(width * 0.7), h: 160 },
            label: txt, viewBox: "0 0 100 100", strokes: [], draw: { startFrame: 0, durFrames: Math.max(6, Math.round(fd * 0.45)) } }] : [],
          overlays: [], camera: [] };
      }
      const qa = validateResolvedScene(plan); // §N non-fatal QA gate
      if (!qa.ok || qa.warnings.length) console.warn(`[whiteboard-plan-svg ${meta.jobId || ""}/${i}] resolved-scene QA: ${[...qa.errors, ...qa.warnings].slice(0, 4).join("; ")}`);
      const mp4 = join(work, `scene-${i}.mp4`);
      const r = await renderSceneSvgFfmpeg(plan, mp4, { fps, crf: tier.crf, audioPath: sc.audioPath || null });
      totalFrames += r.frames; // ACTUAL frames (may be > planned when narration drove a window extension)
      sceneMp4s.push(mp4);
      // live render progress (best-effort; the worker writes it to the job meta for the UI bar)
      if (opts.onProgress) { try { await opts.onProgress(i + 1, scenes.length); } catch { /* non-fatal */ } }
    }
    // concat via the FILTER (decode + sample-accurate join), NOT the demuxer: the demuxer left an AAC
    // encoder-priming gap/click at EVERY scene boundary ("audio patah di pergantian scene"). The
    // filter concatenates the decoded audio samples → gapless. (One scene → just copy.)
    mkdirSync(dirname(outPath), { recursive: true });
    if (sceneMp4s.length === 1) {
      await ff(["-y", "-i", sceneMp4s[0], "-c", "copy", outPath]);
    } else {
      const inputs = sceneMp4s.flatMap((m) => ["-i", m]);
      const fc = sceneMp4s.map((_, i) => `[${i}:v:0][${i}:a:0]`).join("") + `concat=n=${sceneMp4s.length}:v=1:a=1[v][a]`;
      await ff(["-y", ...inputs, "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", String(tier.crf), "-c:a", "aac", "-ar", "44100", "-ac", "2", outPath]);
    }
    // ACTUAL rendered length (sum of real per-scene frames) — covers any audio-driven window
    // extension so the video meter (workers.mjs) and QA expectedDuration match the probed mp4.
    const duration = totalFrames / fps;
    return { duration };
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}

// local copy of render.mjs rescalePlanTiming (kept here so this module is Chromium-free)
function rescaleTiming(plan, actualDuration) {
  const src = Number(plan?.duration) || actualDuration || 1;
  const f = actualDuration > 0 && src > 0 ? actualDuration / src : 1;
  const scale = (arr) => (arr || []).map((b) => ({
    ...b,
    ...(typeof b.start === "number" ? { start: Math.max(0, +(b.start * f).toFixed(3)) } : {}),
    ...(typeof b.end === "number" ? { end: Math.min(actualDuration, +(b.end * f).toFixed(3)) } : {}),
  }));
  return { ...plan, duration: actualDuration, beats: scale(plan?.beats), camera: scale(plan?.camera) };
}
