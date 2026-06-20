// video/whiteboard/render.mjs — the worker-callable Remotion render for the
// "whiteboard" Video Instant mode (Opt B). Called by stitchProcessor IN PLACE OF
// the ffmpeg stitch when meta.visualMode === "whiteboard". It consumes the same
// per-scene data the stitcher already resolved (text + local audioPath + measured
// duration + the per-scene visual asset) and renders ONE MP4. NO API / NO metering
// here — the pipeline's audio + visual workers already did (and metered) that.
//
// Genres: lineart (handwriting + optional lucide, no visual asset), color (SVG →
// draw-reveal), diagram (SVG → diagram), detail (raster + mask SVG → raster-reveal).
import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition } from "@remotion/renderer";
import { readFileSync, copyFileSync, mkdirSync } from "node:fs";
import { dirname, extname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { parseSvg, parseSvgShapes, parseSvgDiagram } from "./svg.mjs";
import { resolvePlan } from "./plan/resolvePlan.mjs";
import { validateResolvedScene } from "./qa.mjs";

// §H rough hand-drawn pass (roughjs) — loaded once, guarded: a missing dep just disables rough.
let _roughen = null;
try { ({ roughenResolved: _roughen } = await import("./plan/rough.mjs")); }
catch (e) { console.warn(`[whiteboard-plan] rough pass unavailable: ${e.message}`); }

const __dir = dirname(fileURLToPath(import.meta.url));
const ENTRY = join(__dir, "src", "index.ts");
const PLAN_ASSETS = join(__dir, "assets", "whiteboard");
// genre → plan render mode: diagram = flowchart (boxes+arrows); detail = raster-reveal
// (Recraft raster, falls back to icons until that renderer lands); else stroke icons.
const GENRE_MODE = { diagram: "diagram", detail: "raster", lineart: "icons", color: "color" };

// cap 1080p; tier sets fps + crf (render fee is flat, tier is quality only)
const ASPECT = { "16:9": [1920, 1080], "9:16": [1080, 1920], "1:1": [1080, 1080], "4:5": [1080, 1350] };
const TIER = { fast: { fps: 30, crf: 28 }, hd: { fps: 30, crf: 23 }, hd_plus: { fps: 60, crf: 20 }, "hd+": { fps: 60, crf: 20 } };
const DRAW = { lineart: 2.5, color: 4, diagram: 6, detail: 5 };

const dataUri = (p) => {
  const ext = (extname(p) || ".png").slice(1).toLowerCase();
  const mime = ext === "jpg" || ext === "jpeg" ? "image/jpeg" : ext === "webp" ? "image/webp" : "image/png";
  return `data:${mime};base64,${readFileSync(p).toString("base64")}`;
};

// 3–4 words per handwritten line (lineart). Narration is the audio; this is the board text.
function wrapLines(text, per = 4) {
  const w = String(text || "").trim().split(/\s+/).filter(Boolean);
  if (!w.length) return [];
  const out = [];
  for (let i = 0; i < w.length; i += per) out.push(w.slice(i, i + per).join(" "));
  return out.slice(0, 4); // keep it to a few lines so the writing fits the narration
}

// build the per-scene `illustration` from the upstream visual asset (no API)
function buildIllustration(genre, sc) {
  if (genre === "diagram" && sc.visualPath) {
    const { viewBox, items } = parseSvgDiagram(readFileSync(sc.visualPath, "utf8"));
    return items.length ? { viewBox, mode: "diagram", items } : null;
  }
  if (genre === "color" && sc.visualPath) {
    const svg = readFileSync(sc.visualPath, "utf8");
    const { strokes } = parseSvg(svg, { split: false, dropBg: true, dropLight: true, lightThreshold: 220 });
    const { viewBox, shapes } = parseSvgShapes(svg, { dropBg: true });
    return strokes.length || shapes.length ? { viewBox, mode: "draw-reveal", strokes, shapes } : null;
  }
  if (genre === "detail" && sc.visualPath && sc.maskPath) {
    const mask = readFileSync(sc.maskPath, "utf8");
    const { viewBox, strokes } = parseSvg(mask, { split: false, dropBg: true, dropLight: true, lightThreshold: 230 });
    const { shapes } = parseSvgShapes(mask, { dropBg: true });
    return { viewBox, mode: "raster-reveal", raster: dataUri(sc.visualPath), strokes, shapes };
  }
  return null; // lineart, or a genre whose asset isn't ready yet → handwriting only
}

/**
 * Render a whiteboard MP4 from the stitcher's resolved scenes.
 * @param scenes [{ text, duration, visualPath?, maskPath?, audioPath?, lucide? }]
 * @param meta   { whiteboardGenre, aspectRatio, tier }
 * @param outPath absolute mp4 path
 * @param opts   { tmpDir, browserExecutable }
 * @returns { duration } seconds
 */
export async function renderWhiteboard(scenes, meta, outPath, opts = {}) {
  const genre = meta.whiteboardGenre || "lineart";
  const [width, height] = ASPECT[meta.aspectRatio] || ASPECT["16:9"];
  const tier = TIER[meta.tier] || TIER.hd;
  const tmpDir = opts.tmpDir || dirname(outPath);
  const pub = join(tmpDir, "wb-public");
  mkdirSync(join(pub, "audio"), { recursive: true });

  const built = scenes.map((sc, i) => {
    let audioSrc;
    if (sc.audioPath) {
      const rel = `audio/scene-${i}${extname(sc.audioPath) || ".wav"}`;
      copyFileSync(sc.audioPath, join(pub, rel)); // Remotion <Audio> resolves via staticFile(publicDir)
      audioSrc = rel;
    }
    const duration = Math.max(Number(sc.duration) || 0, DRAW[genre]) + 0.2;
    const base = {
      narration: sc.text || "",
      holdSeconds: 0.4,
      durationSeconds: Math.round(duration * 100) / 100,
      ...(audioSrc ? { audioSrc } : {}),
    };
    if (genre === "lineart") {
      return { ...base, layout: i === 0 ? "title" : "center", lines: wrapLines(sc.text), ...(sc.lucide ? { lucide: sc.lucide } : {}) };
    }
    const illustration = buildIllustration(genre, sc);
    return { ...base, layout: illustration ? "full" : "center", lines: illustration ? [] : wrapLines(sc.text), illustration: illustration || undefined };
  });

  const spec = { theme: "marker", grid: false, fps: tier.fps, width, height, scenes: built };
  const serveUrl = await bundle({ entryPoint: ENTRY, publicDir: pub });
  const composition = await selectComposition({ serveUrl, id: "Whiteboard", inputProps: spec });
  mkdirSync(dirname(outPath), { recursive: true });
  const browserExecutable = opts.browserExecutable || process.env.REMOTION_BROWSER_EXECUTABLE || undefined;
  // Anti-hang: bound the whole render so a pathological scene (e.g. a heavy
  // raster-reveal mask) can never wedge the worker forever. If we blow the budget
  // the stitch processor catches, fails the job, and refunds — instead of "Merangkai"
  // spinning indefinitely. timeoutInMilliseconds is the per-frame delayRender cap.
  const RENDER_TIMEOUT_MS = Number(process.env.WB_RENDER_TIMEOUT_MS) || 360000;
  const render = renderMedia({
    serveUrl, composition, codec: "h264", crf: tier.crf,
    outputLocation: outPath, inputProps: spec,
    concurrency: Number(process.env.WB_RENDER_CONCURRENCY) || 2,
    timeoutInMilliseconds: 60000,
    ...(browserExecutable ? { browserExecutable } : {}),
  });
  let timer;
  const guard = new Promise((_, rej) => {
    timer = setTimeout(() => rej(new Error(`whiteboard render exceeded ${RENDER_TIMEOUT_MS}ms`)), RENDER_TIMEOUT_MS);
  });
  try {
    await Promise.race([render, guard]);
  } finally {
    clearTimeout(timer);
  }
  const duration = built.reduce((a, s) => a + s.durationSeconds, 0);
  return { duration };
}

// Scale a plan's beat/camera timings from the LLM's assumed duration to the scene's REAL
// (VO-measured) duration, and clamp — so pacing follows the actual narration length.
function rescalePlanTiming(plan, actualDuration) {
  const src = Number(plan?.duration) || actualDuration || 1;
  const f = actualDuration > 0 && src > 0 ? actualDuration / src : 1;
  const scale = (arr) =>
    (arr || []).map((b) => ({
      ...b,
      ...(typeof b.start === "number" ? { start: Math.max(0, +(b.start * f).toFixed(3)) } : {}),
      ...(typeof b.end === "number" ? { end: Math.min(actualDuration, +(b.end * f).toFixed(3)) } : {}),
    }));
  return { ...plan, duration: actualDuration, beats: scale(plan?.beats), camera: scale(plan?.camera) };
}

/**
 * Plan-engine render (Golpo-like): each scene carries a whiteboard_visual_plan (sc.planJson,
 * generated+validated in the visual phase). Resolve each plan (assets→strokes, slots→boxes,
 * beats→frames), rescale to the measured VO duration, and render the multi-scene
 * "WhiteboardPlanVideo" composition → ONE MP4. A scene with no/invalid plan degrades to a
 * blank board for its duration (audio still plays). Mirrors renderWhiteboard's structure.
 * @returns { duration } seconds
 */
export async function renderWhiteboardPlan(scenes, meta, outPath, opts = {}) {
  const [width, height] = ASPECT[meta.aspectRatio] || ASPECT["16:9"];
  const tier = TIER[meta.tier] || TIER.hd;
  const fps = tier.fps;
  const tmpDir = opts.tmpDir || dirname(outPath);
  const pub = join(tmpDir, "wb-public");
  mkdirSync(join(pub, "audio"), { recursive: true });

  const built = scenes.map((sc, i) => {
    let audioSrc = null;
    if (sc.audioPath) {
      const rel = `audio/scene-${i}${extname(sc.audioPath) || ".wav"}`;
      copyFileSync(sc.audioPath, join(pub, rel));
      audioSrc = rel;
    }
    const sceneDur = Math.max(0.5, Number(sc.duration) || 0.5);
    let plan = null;
    try {
      if (sc.planJson) {
        const raw = typeof sc.planJson === "string" ? JSON.parse(sc.planJson) : sc.planJson;
        const mode = GENRE_MODE[meta.whiteboardGenre] || "icons"; // genre → render mode
        plan = resolvePlan(rescalePlanTiming({ ...raw, mode }, sceneDur), { assetsDir: PLAN_ASSETS, fps, strict: false });
        plan.canvas = { width, height };
        // §H rough hand-drawn pass — opt-in via WB_STYLE=rough or plan.style_pass.mode. _roughen is
        // loaded once at module top (guarded), so a missing roughjs dep just leaves the clean style.
        const roughMode = (process.env.WB_STYLE || "").toLowerCase() === "rough" || raw.style_pass?.mode === "rough";
        if (roughMode && plan.mode === "diagram" && _roughen) {
          try {
            plan.style_pass = { mode: "rough", ...(raw.style_pass || {}) };
            _roughen(plan);
          } catch (re) { console.warn(`[whiteboard-plan] rough pass skipped: ${re.message}`); }
        }
      }
    } catch (e) {
      console.warn(`[whiteboard-plan ${meta.jobId || ""}/${i}] resolve failed: ${e.message} → blank board`);
      plan = null;
    }
    if (!plan) {
      // plan unavailable (gen/resolve failed) → DON'T leave a blank board: show the scene's
      // narration as a centered write-on text so the scene always has content.
      const fdur = Math.max(1, Math.round(sceneDur * fps));
      const txt = String(sc.text || sc.visualPrompt || "").trim().split(/\s+/).slice(0, 14).join(" ");
      plan = {
        fps, duration: sceneDur, durationInFrames: fdur, canvas: { width, height }, mode: "icons",
        elements: txt ? [{ id: "fallback", type: "text", slot: "center", box: { x: Math.round(width / 2), y: Math.round(height / 2), w: Math.round(width * 0.7), h: 160 },
          label: txt, viewBox: "0 0 100 100", strokes: [], assetSource: "fallback", fallback: true,
          draw: { startFrame: 0, durFrames: Math.max(6, Math.round(fdur * 0.45)) } }] : [],
        overlays: [], camera: [],
      };
    }
    const qa = validateResolvedScene(plan); // §N non-fatal QA gate on the resolved scene
    if (!qa.ok || qa.warnings.length) console.warn(`[whiteboard-plan ${meta.jobId || ""}/${i}] resolved-scene QA: ${[...qa.errors, ...qa.warnings].slice(0, 4).join("; ")}`);
    return { plan, audioSrc };
  });

  const spec = { fps, width, height, scenes: built };
  const serveUrl = await bundle({ entryPoint: ENTRY, publicDir: pub });
  const composition = await selectComposition({ serveUrl, id: "WhiteboardPlanVideo", inputProps: spec });
  mkdirSync(dirname(outPath), { recursive: true });
  const browserExecutable = opts.browserExecutable || process.env.REMOTION_BROWSER_EXECUTABLE || undefined;
  const RENDER_TIMEOUT_MS = Number(process.env.WB_RENDER_TIMEOUT_MS) || 360000;
  const render = renderMedia({
    serveUrl, composition, codec: "h264", crf: tier.crf,
    outputLocation: outPath, inputProps: spec,
    concurrency: Number(process.env.WB_RENDER_CONCURRENCY) || 2,
    timeoutInMilliseconds: 60000,
    ...(browserExecutable ? { browserExecutable } : {}),
  });
  let timer;
  const guard = new Promise((_, rej) => {
    timer = setTimeout(() => rej(new Error(`whiteboard plan render exceeded ${RENDER_TIMEOUT_MS}ms`)), RENDER_TIMEOUT_MS);
  });
  try {
    await Promise.race([render, guard]);
  } finally {
    clearTimeout(timer);
  }
  const duration = built.reduce((a, s) => a + s.plan.durationInFrames / fps, 0);
  return { duration };
}
