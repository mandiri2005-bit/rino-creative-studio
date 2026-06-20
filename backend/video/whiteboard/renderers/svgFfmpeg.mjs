// renderers/svgFfmpeg.mjs — Chromium-free render backend (Guide-2 §K). SCAFFOLD / EXPERIMENTAL.
//
// Renders a RESOLVED whiteboard plan (the SAME geometry resolvePlan feeds Remotion, so layout is
// consistent) to an MP4 WITHOUT Chromium: build one SVG per frame (draw-on via stroke-dashoffset
// + opacity from the plan beats), rasterize each to PNG, ffmpeg the sequence. The frame BUILDER is
// pure/verifiable in Node today; rasterize needs a system rasterizer (resvg/sharp/rsvg-convert).
//
// STATUS: not wired into the worker and not production-verified (no rasterizer in the current
// image). It exists so the no-Chromium path is real and reviewable; the dispatcher defaults to
// Remotion until this is proven (roadmap §B). Covers the DIAGRAM path (cards+labels+arrows+icon
// strokes); raster-reveal, camera moves and the rough pass are TODO.
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// reveal progress 0..1 for an element/connector at time t given its draw window
function progress(t, startFrame, durFrames, fps) {
  const start = startFrame / fps, end = (startFrame + Math.max(1, durFrames)) / fps;
  return clamp((t - start) / Math.max(0.001, end - start), 0, 1);
}

// One frame as an SVG string. Pure → unit-testable without a rasterizer.
export function buildSceneSvg(plan, frame, fps = 30) {
  const t = frame / fps;
  const { width, height } = plan.canvas;
  const pack = plan.stylePack || {};
  const ink = pack.palette?.ink || "#1A1A1A";
  const accent = pack.palette?.accent || "#2C6CA8";
  const board = pack.board || "#FBFBF7";
  const sw = pack.stroke?.width || 4;
  const parts = [`<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`,
    `<rect width="${width}" height="${height}" fill="${board}"/>`];

  // arrows first (under cards), draw-on via dashoffset
  for (const c of plan.connectors || []) {
    const p = progress(t, c.startFrame, c.durFrames, fps);
    if (p <= 0) continue;
    const L = Math.hypot(c.to.x - c.from.x, c.to.y - c.from.y) || 1;
    parts.push(`<line x1="${c.from.x}" y1="${c.from.y}" x2="${c.to.x}" y2="${c.to.y}" stroke="${accent}" stroke-width="${sw}" stroke-linecap="round" stroke-dasharray="${L}" stroke-dashoffset="${(1 - p) * L}"/>`);
  }

  for (const el of plan.elements || []) {
    const p = progress(t, el.draw.startFrame, el.draw.durFrames, fps);
    if (p <= 0) continue;
    const b = el.box; if (!b) continue;
    const x = b.x - b.w / 2, y = b.y - b.h / 2;
    const diagram = (plan.mode || "icons") === "diagram";
    if (diagram) {
      parts.push(`<rect x="${x}" y="${y}" width="${b.w}" height="${b.h}" rx="24" fill="${accent}12" stroke="${accent}" stroke-width="3" opacity="${clamp(p * 2, 0, 1)}"/>`);
    }
    // icon strokes (draw-on). viewBox-scaled into the upper icon area.
    const iconH = b.h * (diagram ? 0.48 : 0.9), iconTop = diagram ? b.h * 0.16 : 0;
    const [, , vbw = 100, vbh = 100] = String(el.viewBox || "0 0 100 100").split(/\s+/).map(Number);
    const scale = Math.min(b.w / vbw, iconH / vbh);
    const gx = x + (b.w - vbw * scale) / 2, gy = y + iconTop + (iconH - vbh * scale) / 2;
    parts.push(`<g transform="translate(${gx} ${gy}) scale(${scale})">`);
    for (const s of el.shapes || []) parts.push(`<path d="${s.d}" fill="${s.fill || "none"}" opacity="${p}"/>`);
    for (const s of el.strokes || []) {
      const L = 1200; // heuristic length (no getTotalLength outside a browser)
      parts.push(`<path d="${s.d}" fill="none" stroke="${s.stroke || ink}" stroke-width="${(s.width || sw)}" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="${L}" stroke-dashoffset="${(1 - p) * L}"/>`);
    }
    parts.push(`</g>`);
    // label
    if (el.label) {
      const ly = diagram ? y + b.h * 0.68 + 28 : y + b.h + 34;
      parts.push(`<text x="${b.x}" y="${ly}" text-anchor="middle" font-family="${pack.font?.label || "Inter, sans-serif"}" font-weight="${pack.font?.weight || 800}" font-size="${pack.font?.labelSize || 34}" fill="${ink}" opacity="${clamp((p - 0.4) * 2, 0, 1)}">${esc(el.label)}</text>`);
    }
  }
  parts.push(`</svg>`);
  return parts.join("");
}

// Lazy rasterizer adapter: @resvg/resvg-js → sharp → rsvg-convert CLI. Throws if none present.
async function rasterizeSvg(svg, width, height, outPng) {
  try {
    const { Resvg } = await import("@resvg/resvg-js");
    const png = new Resvg(svg, { fitTo: { mode: "width", value: width } }).render().asPng();
    writeFileSync(outPng, png); return;
  } catch { /* try next */ }
  try {
    const sharp = (await import("sharp")).default;
    await sharp(Buffer.from(svg)).png().toFile(outPng); return;
  } catch { /* try next */ }
  await new Promise((res, rej) => {
    const tmp = outPng + ".svg"; writeFileSync(tmp, svg);
    const p = spawn("rsvg-convert", ["-w", String(width), "-h", String(height), "-o", outPng, tmp]);
    p.on("error", rej); p.on("close", (c) => (c === 0 ? res() : rej(new Error(`rsvg-convert exit ${c} (install a rasterizer: @resvg/resvg-js, sharp, or librsvg)`))));
  });
}

// Render a resolved scene to MP4 via SVG frames + ffmpeg. EXPERIMENTAL — needs a rasterizer.
export async function renderSceneSvgFfmpeg(plan, outMp4, { fps = 30, audioPath = null } = {}) {
  const frames = plan.durationInFrames || Math.round((plan.duration || 4) * fps);
  const dir = mkdtempSync(join(tmpdir(), "wbsvg-"));
  try {
    for (let f = 0; f < frames; f++) {
      await rasterizeSvg(buildSceneSvg(plan, f, fps), plan.canvas.width, plan.canvas.height,
        join(dir, `f${String(f).padStart(5, "0")}.png`));
    }
    const args = ["-y", "-framerate", String(fps), "-i", join(dir, "f%05d.png")];
    if (audioPath) args.push("-i", audioPath);
    args.push("-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", ...(audioPath ? ["-c:a", "aac", "-shortest"] : []), outMp4);
    await new Promise((res, rej) => {
      const p = spawn("ffmpeg", args);
      p.on("error", rej); p.on("close", (c) => (c === 0 ? res() : rej(new Error(`ffmpeg exit ${c}`))));
    });
    return { path: outMp4, renderer: "svg_ffmpeg", frames };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}
