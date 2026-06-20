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
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname, extname } from "node:path";
import { resolvePlan } from "../plan/resolvePlan.mjs";
import { validateResolvedScene } from "../qa.mjs";

// keep these in sync with render.mjs (duplicated so this stays Chromium/@remotion-free)
const ASPECT = { "16:9": [1920, 1080], "9:16": [1080, 1920], "1:1": [1080, 1080], "4:5": [1080, 1350] };
const TIER = { fast: { fps: 30, crf: 28 }, hd: { fps: 30, crf: 23 }, hd_plus: { fps: 60, crf: 20 } };
const GENRE_MODE = { diagram: "diagram", detail: "raster", lineart: "icons", color: "icons" };

let _roughen = null;
try { ({ roughenResolved: _roughen } = await import("../plan/rough.mjs")); } catch { /* roughjs optional */ }

const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
const lerp = (a, b, t) => a + (b - a) * t;
const progress = (t, sf, df, fps) => clamp((t - sf / fps) / Math.max(0.001, df / fps), 0, 1);
// labels: same humanist sans the Linux worker (+ Chromium) fall back to → parity, no bundled font
const FONT_STACK = "Inter, 'Liberation Sans', 'DejaVu Sans', 'Noto Sans', Arial, sans-serif";

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
      const brush = Math.max(8, Math.round(mvw / 34));
      const rep = (d) => { const m = /[Mm]\s*(-?[\d.]+)[\s,]+(-?[\d.]+)/.exec(d); return m ? { x: +m[1], y: +m[2] } : { x: mvx + mvw / 2, y: mvy + mvh / 2 }; };
      const NB = 4, bandH = mvh / NB;
      const units = [
        ...(el.maskShapes || []).map((s) => ({ ...rep(s.d), el: "shape", d: s.d })),
        ...(el.maskStrokes || []).map((s) => ({ ...rep(s.d), el: "stroke", d: s.d })),
      ].sort((a, c) => {
        const ba = clamp(Math.floor((a.y - mvy) / bandH), 0, NB - 1), bb = clamp(Math.floor((c.y - mvy) / bandH), 0, NB - 1);
        return ba !== bb ? ba - bb : (ba % 2 === 0 ? a.x - c.x : c.x - a.x);
      });
      if (units.length === 0) {
        // NO mask (vectorize unavailable, e.g. flux without recraft) → reveal the FULL image with a
        // left→right wipe so the paid raster is never lost / blanked.
        const id = `rw${clipId++}`, rw = Math.round(b.w * p);
        out.push(`<clipPath id="${id}"><rect x="${x}" y="${y + iconTop}" width="${rw}" height="${iconH}"/></clipPath>`);
        out.push(`<image href="${el.raster}" x="${x}" y="${y + iconTop}" width="${b.w}" height="${iconH}" preserveAspectRatio="xMidYMid meet" clip-path="url(#${id})"/>`);
        if (el.label) {
          const ly2 = diagram ? y + b.h * 0.68 + 28 : y + b.h + 34;
          out.push(`<text x="${b.x}" y="${ly2}" text-anchor="middle" font-family="${FONT_STACK}" font-weight="${pack.font?.weight || 800}" font-size="${pack.font?.labelSize || 34}" fill="${ink}" opacity="${clamp((p - 0.4) * 2, 0, 1)}">${esc(el.label)}</text>`);
        }
        continue;
      }
      const N = Math.max(1, units.length), SPAN = 0.92, WIN = 0.06;
      const id = `rv${clipId++}`;
      out.push(`<g transform="translate(${mgx} ${mgy}) scale(${ms})"><mask id="${id}" maskUnits="userSpaceOnUse">`);
      units.forEach((u, i) => {
        const op = clamp((p - (i / N) * SPAN) / WIN, 0, 1);
        if (op <= 0) return;
        out.push(u.el === "shape"
          ? `<path d="${u.d}" fill="white" opacity="${op.toFixed(3)}"/>`
          : `<path d="${u.d}" fill="none" stroke="white" stroke-width="${brush}" stroke-linecap="round" stroke-linejoin="round" opacity="${op.toFixed(3)}"/>`);
      });
      out.push(`</mask><image href="${el.raster}" x="${mvx}" y="${mvy}" width="${mvw}" height="${mvh}" preserveAspectRatio="xMidYMid meet" mask="url(#${id})"/></g>`);
      if (p > 0.005 && p < 0.985 && N > 0) {
        const cur = Math.min(1, p / SPAN) * (N - 1), i0 = Math.floor(cur), i1 = Math.min(N - 1, i0 + 1), fr = cur - i0;
        const hx = units[i0].x + (units[i1].x - units[i0].x) * fr, hy = units[i0].y + (units[i1].y - units[i0].y) * fr;
        hands.push({ x: mgx + hx * ms, y: mgy + hy * ms, size: Math.max(120, iconH * 0.5), nib: ink });
      }
    } else {
      // vector icon: filled shapes (Recraft/color/phosphor) under self-draw strokes that draw
      // SEQUENTIALLY (match Remotion: stroke i over [start+i·per, +per]); the hand rides the active one.
      const [, , vbw = 100, vbh = 100] = String(el.viewBox || "0 0 100 100").split(/\s+/).map(Number);
      const scale = Math.min(b.w / vbw, iconH / vbh);
      const gx = x + (b.w - vbw * scale) / 2, gy = y + iconTop + (iconH - vbh * scale) / 2;
      out.push(`<g transform="translate(${gx} ${gy}) scale(${scale})">`);
      for (const s of el.shapes || []) out.push(`<path d="${s.d}" fill="${s.fill || "none"}" stroke="${s.stroke || "none"}" stroke-width="${s.width || 0}" opacity="${p}"/>`);
      const strokes = el.strokes || [];
      const n = Math.max(1, strokes.length), per = el.draw.durFrames / n;
      strokes.forEach((s, si) => {
        const ts = progress(t, el.draw.startFrame + si * per, per, fps);
        if (ts <= 0) return;
        const L = 1200;
        out.push(`<path d="${s.d}" fill="none" stroke="${s.stroke || ink}" stroke-width="${s.width || sw}" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="${L}" stroke-dashoffset="${(1 - ts) * L}"/>`);
        if (ts > 0.02 && ts < 0.985) {
          const pt = pointOnPath(s.d, ts);
          if (pt) hands.push({ x: gx + pt.x * scale, y: gy + pt.y * scale, size: Math.max(90, iconH * 0.55), nib: s.stroke || ink });
        }
      });
      out.push(`</g>`);
    }

    if (el.label) {
      const ly = diagram ? y + b.h * 0.68 + 28 : y + b.h + 34;
      out.push(`<text x="${b.x}" y="${ly}" text-anchor="middle" font-family="${FONT_STACK}" font-weight="${pack.font?.weight || 800}" font-size="${pack.font?.labelSize || 34}" fill="${ink}" opacity="${clamp((p - 0.4) * 2, 0, 1)}">${esc(el.label)}</text>`);
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

const ff = (args) => new Promise((res, rej) => {
  const p = spawn("ffmpeg", args);
  let err = "";
  p.stderr.on("data", (d) => { err += d; });
  p.on("error", rej); p.on("close", (c) => (c === 0 ? res() : rej(new Error(`ffmpeg ${c}: ${err.slice(-300)}`))));
});

// Render ONE resolved scene → MP4 (frames → rasterize → ffmpeg, optional audio).
export async function renderSceneSvgFfmpeg(plan, outMp4, { fps = 30, crf = 23, audioPath = null } = {}) {
  const frames = plan.durationInFrames || Math.round((plan.duration || 4) * fps);
  await ensurePathLib().catch(() => {}); // for the following-hand pen tip (graceful if absent)
  const dir = mkdtempSync(join(tmpdir(), "wbsvg-"));
  try {
    for (let f = 0; f < frames; f++) {
      await rasterizeSvg(buildSceneSvg(plan, f, fps), plan.canvas.width, join(dir, `f${String(f).padStart(5, "0")}.png`));
    }
    // every scene mp4 carries an audio track (real TTS, or a silent one) so the concat is uniform
    const args = ["-y", "-framerate", String(fps), "-i", join(dir, "f%05d.png")];
    if (audioPath) args.push("-i", audioPath);
    else args.push("-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100");
    args.push("-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", String(crf),
      "-c:a", "aac", "-shortest", outMp4);
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
  try {
    for (let i = 0; i < scenes.length; i++) {
      const sc = scenes[i];
      const sceneDur = Math.max(0.5, Number(sc.duration) || 0.5);
      let plan = null;
      try {
        if (sc.planJson) {
          const raw = typeof sc.planJson === "string" ? JSON.parse(sc.planJson) : sc.planJson;
          const mode = GENRE_MODE[meta.whiteboardGenre] || "icons";
          plan = resolvePlan(rescaleTiming({ ...raw, mode }, sceneDur), { assetsDir, fps, strict: false });
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
      if (!plan) plan = { fps, duration: sceneDur, durationInFrames: Math.max(1, Math.round(sceneDur * fps)), canvas: { width, height }, elements: [], overlays: [], camera: [] };
      const qa = validateResolvedScene(plan); // §N non-fatal QA gate
      if (!qa.ok || qa.warnings.length) console.warn(`[whiteboard-plan-svg ${meta.jobId || ""}/${i}] resolved-scene QA: ${[...qa.errors, ...qa.warnings].slice(0, 4).join("; ")}`);
      const mp4 = join(work, `scene-${i}.mp4`);
      await renderSceneSvgFfmpeg(plan, mp4, { fps, crf: tier.crf, audioPath: sc.audioPath || null });
      sceneMp4s.push(mp4);
    }
    // concat (re-encode for safe joins across scenes)
    const listFile = join(work, "list.txt");
    writeFileSync(listFile, sceneMp4s.map((m) => `file '${m}'`).join("\n"));
    mkdirSync(dirname(outPath), { recursive: true });
    await ff(["-y", "-f", "concat", "-safe", "0", "-i", listFile, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", String(tier.crf), "-c:a", "aac", outPath]);
    const duration = scenes.reduce((a, s) => a + Math.max(0.5, Number(s.duration) || 0.5), 0);
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
