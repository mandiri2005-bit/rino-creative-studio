// whiteboard/qa.mjs — QA gates (Guide-2 §N/§T). Two checks the pipeline lacked:
//   • validateResolvedScene — the RESOLVED plan is actually renderable (boxes finite + on-canvas,
//     every element has something to draw, viewBox sane, draw windows valid). Complements
//     validate.mjs (which checks the RAW LLM plan). Pure → unit-testable.
//   • validateRenderedClip — the produced MP4 is real (exists, non-zero, duration ≈ expected,
//     has a video stream). Shells out to ffprobe.
//   • probeClipContent + assessClip — the DELIVERY gate: blank (black) video and silent narration
//     are detected with ffmpeg blackdetect/silencedetect (an empty white board only warns), and assessClip turns every signal into
//     one verdict (pass / warn / reject). The worker rejects a broken clip before it is uploaded
//     or charged, so the user gets a refund instead of a blank video. Parsers are pure → testable.
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

// ── Delivery gate ────────────────────────────────────────────────────────────────────────────
// Parse `ffmpeg -vf blackdetect` stderr → total seconds of black video.
export function parseBlackdetect(stderr) {
  let total = 0;
  const rx = /black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)/g;
  let m;
  while ((m = rx.exec(String(stderr || "")))) {
    const a = Number(m[1]), b = Number(m[2]);
    if (Number.isFinite(a) && Number.isFinite(b) && b > a) total += b - a;
  }
  return total;
}

// Parse `ffmpeg -af silencedetect` stderr → total seconds of silence. A silence that starts
// but never ends runs to the end of the clip (pass `duration` to close it).
export function parseSilencedetect(stderr, duration = 0) {
  const text = String(stderr || "");
  let total = 0, open = null;
  const rx = /silence_(start|end):\s*(-?[\d.]+)/g;
  let m;
  while ((m = rx.exec(text))) {
    const t = Math.max(0, Number(m[2]));
    if (!Number.isFinite(t)) continue;
    if (m[1] === "start") open = t;
    else if (open !== null) { total += Math.max(0, t - open); open = null; }
  }
  if (open !== null && duration > open) total += duration - open;
  return total;
}

function ffmpegStderr(args) {
  return new Promise((res) => {
    const bin = process.env.FFMPEG_BIN || "ffmpeg";
    const p = spawn(bin, args);
    let err = "";
    p.stderr.on("data", (d) => { err += d; if (err.length > 2e6) err = err.slice(-1e6); });
    p.on("error", () => res(null));
    p.on("close", () => res(err));
  });
}

// Measure black video and silent audio across the whole clip. Returns seconds, or null for a
// measurement that could not run (never throws: an unmeasurable signal is not a rejection).
export async function probeClipContent(filePath, { duration = 0, hasAudio = true } = {}) {
  // pic_th 0.98 = a frame is "black" when 98% of its pixels are dark → a blank board, not a
  // dark illustration. Whiteboards are light, so also test for an all-white (empty) canvas.
  const black = await ffmpegStderr(["-hide_banner", "-nostats", "-i", filePath,
    "-vf", "blackdetect=d=1:pic_th=0.98", "-an", "-f", "null", "-"]);
  const blank = await ffmpegStderr(["-hide_banner", "-nostats", "-i", filePath,
    "-vf", "negate,blackdetect=d=1:pic_th=0.995:pix_th=0.02", "-an", "-f", "null", "-"]);
  let silence = null;
  if (hasAudio) {
    const s = await ffmpegStderr(["-hide_banner", "-nostats", "-i", filePath,
      "-af", "silencedetect=n=-50dB:d=1", "-vn", "-f", "null", "-"]);
    silence = s === null ? null : parseSilencedetect(s, duration);
  }
  return {
    blackSec: black === null ? null : parseBlackdetect(black),
    blankSec: blank === null ? null : parseBlackdetect(blank),
    silenceSec: silence,
  };
}

// One verdict from the structural probe (validateRenderedClip) + content probe.
//   reject — the clip is unusable: missing, no video, zero length, truncated to under half,
//            ≥ 90% black, or narration expected but ≥ 95% silent / no audio.
//   warn   — deliverable but worth a look (duration drift, long blank or silent stretches).
//   pass   — nothing found.
export function assessClip({ clip, content = {}, expectedDuration = 0, expectAudio = true } = {}) {
  const reasons = [], warnings = [];
  if (!clip) return { verdict: "reject", reasons: ["no clip probe"], warnings };
  reasons.push(...(clip.errors || []));
  warnings.push(...(clip.warnings || []));
  const d = Number(clip.duration) || 0;
  if (d > 0 && expectedDuration > 0 && d < expectedDuration * 0.5) {
    reasons.push(`clip truncated: ${d.toFixed(1)}s of expected ${expectedDuration.toFixed(1)}s`);
  }
  if (d > 0) {
    const frac = (sec) => (typeof sec === "number" ? sec / d : 0);
    // Black frames mean the renderer failed (a crashed browser renders black) → reject.
    // An all-white frame is NOT proof of failure: sparse lineart on a white board can cover under
    // 0.5% of the pixels for a whole scene, so an empty-looking board only ever warns.
    const black = frac(content.blackSec), white = frac(content.blankSec);
    if (black >= 0.9) reasons.push(`video is blank (black) for ${Math.round(black * 100)}% of its length`);
    else if (black >= 0.3) warnings.push(`video is blank (black) for ${Math.round(black * 100)}% of its length`);
    if (white >= 0.9) warnings.push(`board looks empty for ${Math.round(white * 100)}% of its length`);
    if (expectAudio) {
      const streams = clip.streams || [];
      if (!streams.includes("audio")) reasons.push("narration missing: clip has no audio stream");
      else {
        const quiet = frac(content.silenceSec);
        if (quiet >= 0.95) reasons.push(`narration missing: audio is silent for ${Math.round(quiet * 100)}% of the clip`);
        else if (quiet >= 0.4) warnings.push(`audio is silent for ${Math.round(quiet * 100)}% of the clip`);
      }
    }
  }
  const verdict = reasons.length ? "reject" : warnings.length ? "warn" : "pass";
  return { verdict, reasons, warnings };
}

// Full gate for a rendered file. WB_QA_MODE: "enforce" (default) rejects; "observe" only reports.
export async function runDeliveryGate(filePath, { expectedDuration = 0, expectAudio = true } = {}) {
  const clip = await validateRenderedClip(filePath, { expectedDuration });
  const content = clip.ok
    ? await probeClipContent(filePath, { duration: clip.duration, hasAudio: (clip.streams || []).includes("audio") })
    : {};
  const out = assessClip({ clip, content, expectedDuration, expectAudio });
  const mode = String(process.env.WB_QA_MODE || "enforce").toLowerCase();
  return { ...out, mode, enforce: mode !== "observe", duration: clip.duration, content };
}
