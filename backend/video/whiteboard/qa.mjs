// whiteboard/qa.mjs — QA gates (Guide-2 §N/§T). Two checks the pipeline lacked:
//   • validateResolvedScene — the RESOLVED plan is actually renderable (boxes finite + on-canvas,
//     every element has something to draw, viewBox sane, draw windows valid). Complements
//     validate.mjs (which checks the RAW LLM plan). Pure → unit-testable.
//   • validateRenderedClip — the produced MP4 is real (exists, non-zero, duration ≈ expected,
//     has a video stream). Shells out to ffprobe. Both are NON-FATAL signals the worker logs.
import { spawn } from "node:child_process";
import { statSync } from "node:fs";

const finite = (n) => typeof n === "number" && Number.isFinite(n);

// Check a resolved plan (post-resolvePlan) is renderable. Returns { ok, errors, warnings }.
export function validateResolvedScene(plan) {
  const errors = [], warnings = [];
  if (!plan || typeof plan !== "object") return { ok: false, errors: ["resolved plan is not an object"], warnings };
  const W = plan.canvas?.width, H = plan.canvas?.height;
  if (!finite(W) || !finite(H) || W <= 0 || H <= 0) errors.push(`bad canvas ${W}x${H}`);
  if (!finite(plan.durationInFrames) || plan.durationInFrames <= 0) errors.push(`durationInFrames must be > 0 (got ${plan.durationInFrames})`);

  const els = Array.isArray(plan.elements) ? plan.elements : [];
  for (const el of els) {
    const b = el.box;
    if (!b || ![b.x, b.y, b.w, b.h].every(finite) || b.w <= 0 || b.h <= 0) { errors.push(`element ${el.id}: bad box`); continue; }
    if (finite(W) && finite(H) && (b.x - b.w / 2 < -2 || b.y - b.h / 2 < -2 || b.x + b.w / 2 > W + 2 || b.y + b.h / 2 > H + 2)) {
      warnings.push(`element ${el.id}: box off-canvas`);
    }
    const hasArt = (el.strokes && el.strokes.length) || (el.shapes && el.shapes.length) || el.raster;
    if (!hasArt) warnings.push(`element ${el.id}: nothing to draw (no strokes/shapes/raster)`);
    const vb = String(el.viewBox || "").split(/\s+/).map(Number);
    if (vb.length !== 4 || !vb.every(finite)) warnings.push(`element ${el.id}: bad viewBox "${el.viewBox}"`);
    if (!el.draw || !finite(el.draw.startFrame) || !finite(el.draw.durFrames) || el.draw.durFrames <= 0) {
      errors.push(`element ${el.id}: bad draw window`);
    }
  }
  for (const c of plan.connectors || []) {
    if (![c.from?.x, c.from?.y, c.to?.x, c.to?.y].every(finite)) errors.push("connector: non-finite endpoint");
  }
  return { ok: errors.length === 0, errors, warnings };
}

function ffprobeJson(filePath) {
  return new Promise((res) => {
    const bin = process.env.FFPROBE_BIN || "ffprobe";
    const p = spawn(bin, ["-v", "error", "-show_entries", "format=duration:stream=codec_type",
      "-of", "json", filePath]);
    let out = "";
    p.stdout.on("data", (d) => { out += d; });
    p.on("error", () => res(null));
    p.on("close", () => { try { res(JSON.parse(out)); } catch { res(null); } });
  });
}

// Check the rendered MP4. Returns { ok, errors, warnings, duration, streams }.
export async function validateRenderedClip(filePath, { expectedDuration = 0, tolerance = 0.75 } = {}) {
  const errors = [], warnings = [];
  let size = 0;
  try { size = statSync(filePath).size; } catch { return { ok: false, errors: [`clip missing: ${filePath}`], warnings }; }
  if (size < 1024) errors.push(`clip suspiciously small (${size} bytes)`);
  const j = await ffprobeJson(filePath);
  const duration = Number(j?.format?.duration) || 0;
  const kinds = (j?.streams || []).map((s) => s.codec_type);
  const hasVideo = kinds.includes("video");
  if (!hasVideo) errors.push("clip has no video stream");
  if (!kinds.includes("audio")) warnings.push("clip has no audio stream");
  if (!duration) errors.push("clip duration is 0/unreadable");
  else if (expectedDuration > 0 && Math.abs(duration - expectedDuration) > tolerance) {
    warnings.push(`clip duration ${duration.toFixed(2)}s vs expected ${expectedDuration.toFixed(2)}s`);
  }
  return { ok: errors.length === 0, errors, warnings, duration, streams: kinds };
}
