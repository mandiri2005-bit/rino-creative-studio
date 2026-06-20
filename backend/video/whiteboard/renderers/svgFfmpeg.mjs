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
      out.push(`<g opacity="${clamp(p * 2, 0, 1)}"><circle cx="${x}" cy="${y}" r="24" fill="${accent}"/><text x="${x}" y="${y + 9}" text-anchor="middle" font-family="${pack.font?.label || "Inter, sans-serif"}" font-weight="800" font-size="26" fill="#fff">${ei + 1}</text></g>`);
    }

    const iconH = b.h * (diagram ? 0.48 : 0.9), iconTop = diagram ? b.h * 0.16 : 0;

    if (el.raster) {
      // raster-reveal: photo wiped in left→right, the vectorized mask strokes drawn over it
      const id = `rv${clipId++}`;
      const rw = Math.round(b.w * p);
      out.push(`<clipPath id="${id}"><rect x="${x}" y="${y + iconTop}" width="${rw}" height="${iconH}"/></clipPath>`);
      out.push(`<image href="${el.raster}" x="${x}" y="${y + iconTop}" width="${b.w}" height="${iconH}" preserveAspectRatio="xMidYMid meet" clip-path="url(#${id})"/>`);
      const [, , mvw = 100, mvh = 100] = String(el.maskViewBox || "0 0 100 100").split(/\s+/).map(Number);
      const ms = Math.min(b.w / mvw, iconH / mvh);
      const mgx = x + (b.w - mvw * ms) / 2, mgy = y + iconTop + (iconH - mvh * ms) / 2;
      out.push(`<g transform="translate(${mgx} ${mgy}) scale(${ms})">`);
      for (const s of el.maskStrokes || []) {
        const L = 1200;
        out.push(`<path d="${s.d}" fill="none" stroke="${s.stroke || ink}" stroke-width="${s.width || sw}" stroke-linecap="round" stroke-dasharray="${L}" stroke-dashoffset="${(1 - p) * L}"/>`);
      }
      out.push(`</g>`);
    } else {
      // vector icon: filled shapes (Recraft/color/phosphor) under self-draw strokes
      const [, , vbw = 100, vbh = 100] = String(el.viewBox || "0 0 100 100").split(/\s+/).map(Number);
      const scale = Math.min(b.w / vbw, iconH / vbh);
      const gx = x + (b.w - vbw * scale) / 2, gy = y + iconTop + (iconH - vbh * scale) / 2;
      out.push(`<g transform="translate(${gx} ${gy}) scale(${scale})">`);
      for (const s of el.shapes || []) out.push(`<path d="${s.d}" fill="${s.fill || "none"}" stroke="${s.stroke || "none"}" stroke-width="${s.width || 0}" opacity="${p}"/>`);
      for (const s of el.strokes || []) {
        const L = 1200;
        out.push(`<path d="${s.d}" fill="none" stroke="${s.stroke || ink}" stroke-width="${s.width || sw}" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="${L}" stroke-dashoffset="${(1 - p) * L}"/>`);
      }
      out.push(`</g>`);
    }

    if (el.label) {
      const ly = diagram ? y + b.h * 0.68 + 28 : y + b.h + 34;
      out.push(`<text x="${b.x}" y="${ly}" text-anchor="middle" font-family="${pack.font?.label || "Inter, sans-serif"}" font-weight="${pack.font?.weight || 800}" font-size="${pack.font?.labelSize || 34}" fill="${ink}" opacity="${clamp((p - 0.4) * 2, 0, 1)}">${esc(el.label)}</text>`);
    }
  }
  // overlays (highlight circles)
  for (const ov of plan.overlays || []) {
    const p = progress(t, ov.startFrame, ov.durFrames, fps);
    if (p <= 0) continue;
    const rxr = (ov.box.w / 2 + 18), ryr = (ov.box.h / 2 + 18), L = Math.PI * 2 * Math.max(rxr, ryr);
    out.push(`<ellipse cx="${ov.box.x}" cy="${ov.box.y}" rx="${rxr}" ry="${ryr}" fill="none" stroke="${pack.palette?.highlight || accent}" stroke-width="${sw + 1}" stroke-dasharray="${L}" stroke-dashoffset="${(1 - p) * L}"/>`);
  }
  out.push(`</g></svg>`);
  return out.join("");
}

// Lazy rasterizer: @resvg/resvg-js → sharp → rsvg-convert CLI.
async function rasterizeSvg(svg, width, outPng) {
  try {
    const { Resvg } = await import("@resvg/resvg-js");
    writeFileSync(outPng, new Resvg(svg, { fitTo: { mode: "width", value: width } }).render().asPng());
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
