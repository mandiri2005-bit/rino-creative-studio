// ─────────────────────────────────────────────────────────────────────────────
// video/workers.mjs — the three BullMQ workers (audio, visual, batch-check) plus
// the stitch worker. These turn a scene list into simultaneous jobs and fuse the
// result. They run ONLY in the worker process (worker-entry.mjs) — never the API.
//
//   audio  : per-scene narration → measure real duration (ffprobe) → store
//   visual : per-scene image (Ken Burns) or clip (Veo/Kling), with clip→image
//            fallback so one bad generation degrades a scene, not the whole video
//   check  : after any asset lands, advance the job (next batch / stitch / fail)
//   stitch : when every scene is ready, FFmpeg-fuse to one MP4 (master clock = VO)
//
// HARDENING (midpoint review): every processor wraps its whole body so it can
// never reject and leave a scene stuck 'pending' (which would hang the job
// forever); it always writes a terminal scene status and always enqueues a check
// (finally). Audio/visual are idempotent — a re-delivered job whose asset already
// landed short-circuits BEFORE the metered Python call, so a retry never double-
// charges; jobs that are already terminal skip generation (no sibling spend after
// a hard fail). A worker-level 'failed' net covers an exhausted/stalled job.
// ─────────────────────────────────────────────────────────────────────────────
import { Worker, Queue } from "bullmq";
import { mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { QUEUE, CONCURRENCY, makeConnection } from "./connection.mjs";
import * as store from "./store.mjs";
import { ffprobeDuration, stitch, buildAssFromScenes, hasSubtitlesFilter } from "./ffmpeg.mjs";
import { advance } from "./orchestrator.mjs";
import { rm } from "node:fs/promises";
// NOTE: whiteboard render/visuals are imported LAZILY inside the worker-only branches
// below (dynamic import), NEVER at top level — workers.mjs is loaded by the API/frontend
// process too (server.js → routes.mjs → makeQueues), and render.mjs pulls @remotion +
// Chromium. An eager import here crashed the whole frontend on startup. Keep it lazy.

// Deterministic positive seed from the job id — the SAME seed for every scene of a
// video, so image models that honour `seed` keep the look (and any recurring
// character) steadier across scenes. FNV-1a → 1..2e9.
export function hashSeed(s) {
  let h = 2166136261;
  for (let i = 0; i < String(s).length; i++) { h ^= String(s).charCodeAt(i); h = Math.imul(h, 16777619); }
  return ((h >>> 0) % 2000000000) + 1;
}

export function jobTmpDir(jobId) {
  return join(tmpdir(), "rcs-video", String(jobId));
}

const JOB_TERMINAL = new Set(["failed", "canceled", "done", "stitching"]);

/** Producer-side queue handles (safe to import on the API side too). */
export function makeQueues(connection = makeConnection()) {
  return {
    audio: new Queue(QUEUE.AUDIO, { connection }),
    visual: new Queue(QUEUE.VISUAL, { connection }),
    check: new Queue(QUEUE.CHECK, { connection }),
    stitch: new Queue(QUEUE.STITCH, { connection }),
  };
}

// ── optional R2 ──
let _storage = null;
async function storage() {
  if (_storage === null) {
    try { _storage = await import("../storage.mjs"); } catch { _storage = false; }
  }
  return _storage && _storage.isConfigured?.() ? _storage : null;
}

async function maybeUpload(jobId, tenantId, assetType, localPath) {
  const s = await storage();
  if (!s) return { path: localPath, key: null };
  const { readFile } = await import("node:fs/promises");
  const name = localPath.split("/").pop();
  const key = s.buildKey(tenantId, jobId, assetType, name);
  const ctype = name.endsWith(".mp4") ? "video/mp4" : name.endsWith(".png") ? "image/png"
    : name.endsWith(".svg") ? "image/svg+xml" : "audio/wav";
  await s.uploadBytes(key, await readFile(localPath), ctype);
  return { path: localPath, key };
}

async function resolveLocal(jobId, tmpDir, key, path, fallbackName) {
  // Only trust the stored local path if the file is ACTUALLY there. With more than
  // one video-worker replica a scene's asset is written on whichever replica picked
  // up that job, so the replica running the stitch may not have it on disk — fall
  // back to re-downloading from R2 (the shared copy maybeUpload pushed).
  if (path && existsSync(path)) return path;
  const s = await storage();
  if (s && key) {
    const { writeFile } = await import("node:fs/promises");
    const out = join(tmpDir, fallbackName);
    await writeFile(out, await s.downloadBytes(key));
    return out;
  }
  throw new Error(
    `no asset for ${fallbackName}: local '${path || "?"}' missing and ` +
    (key ? "R2 download failed" : "no R2 key — set object storage on the video-worker OR run a single replica")
  );
}

// ── processors (exported for unit/integration testing) ──
export async function audioProcessor(job, deps) {
  const { jobId, sceneIndex } = job.data;
  try {
    const meta = await deps.store.getMeta(jobId);
    const scene = await deps.store.getScene(jobId, sceneIndex);
    if (!meta || !scene) return { skipped: "missing" };
    if (JOB_TERMINAL.has(meta.status)) return { skipped: "job-terminal" };
    if (scene.audioStatus === "done" || scene.audioStatus === "fallback") return { skipped: "already-done" }; // idempotent: no re-charge
    const tmpDir = jobTmpDir(jobId);
    await mkdir(tmpDir, { recursive: true });
    const base = { jobId, sceneIndex, text: scene.text, estSeconds: Number(scene.estSeconds),
      voice: meta.voice || undefined, ttsModel: meta.ttsModel || undefined,
      tenantId: meta.tenantId, userId: meta.userId };
    let a, audioFellBack = false, audioErr = null;
    try {
      a = await deps.generationClient.synthesizeAudio(base, tmpDir);
    } catch (e) {
      // TTS failed after its own retries → degrade this ONE scene to a SILENT track so the
      // whole video doesn't fail (mirrors the visual placeholder). Counts as complete via
      // sceneComplete's audio "fallback". Last resort; if no silentAudio (old mock) → rethrow.
      if (typeof deps.generationClient.silentAudio !== "function") throw e;
      audioErr = e.message; audioFellBack = true;
      // LOG it — the silent fallback used to be invisible in logs (only stored on the scene row),
      // so "no sound" had no findable cause. This surfaces the real TTS error (timeout/key/provider).
      console.warn(`[audio ${jobId}/${sceneIndex}] TTS failed → SILENT track: ${e.message}`);
      a = await deps.generationClient.silentAudio(base, tmpDir);
    }
    const duration = (await ffprobeDuration(a.path)) || a.durationSeconds || Number(scene.estSeconds) || 0;
    const up = await maybeUpload(jobId, meta.tenantId, "audio", a.path);
    await deps.store.setSceneFields(jobId, sceneIndex, {
      audioStatus: audioFellBack ? "fallback" : "done", audioPath: up.path, audioKey: up.key,
      durationActual: duration.toFixed(3), ...(audioFellBack ? { audioError: audioErr } : {}),
    });
    return { sceneIndex, ...(audioFellBack ? { fellBack: true } : {}) };
  } catch (e) {
    await deps.store.setSceneFields(jobId, sceneIndex, { audioStatus: "failed", audioError: e.message }).catch(() => {});
    return { sceneIndex, failed: e.message };
  } finally {
    // always advance, even on the skip/return paths; never let a queue blip re-run a charged job
    await deps.queues.check.add("check", { jobId }).catch(() => {});
  }
}

// The per-video reference anchor (base64), resolved once per job and cached. It
// comes inline in meta (no R2) or is downloaded from meta.anchorKey. Each scene
// passes it as ref_image so every image shares the anchor's character/look.
const _anchorCache = new Map(); // jobId -> base64 | null
async function resolveAnchor(jobId, tmpDir, meta) {
  if (_anchorCache.has(jobId)) return _anchorCache.get(jobId);
  let b64 = null;
  try {
    if (meta.anchorB64) {
      b64 = meta.anchorB64;
    } else if (meta.anchorKey) {
      const local = join(tmpDir, "anchor.png");
      if (!existsSync(local)) {
        const s = await storage();
        if (s) {
          const { writeFile } = await import("node:fs/promises");
          await writeFile(local, await s.downloadBytes(meta.anchorKey));
        }
      }
      if (existsSync(local)) {
        const { readFile } = await import("node:fs/promises");
        b64 = (await readFile(local)).toString("base64");
      }
    }
  } catch (e) { console.warn(`[anchor ${jobId}] resolve failed: ${e.message}`); }
  _anchorCache.set(jobId, b64);
  return b64;
}

// Diverse-provider image fallbacks: if the chosen model's provider is flaky or
// content-blocks the prompt, a DIFFERENT provider usually succeeds — so a failed
// scene still gets a REAL image instead of a placeholder.
// ORDER MATTERS for aspect: `flux-kontext-pro` honours the requested ratio (16:9),
// whereas `nano-banana` is 1:1-only and `seedream-4-0` is hardcoded 2K-square — both
// produce a square that the ffmpeg cover-crop then chops (the "kepotong di atas" bug).
// So try the aspect-correct provider FIRST; the squares are last-resort only.
// Override with VIDEO_IMAGE_FALLBACKS="a,b,c" (no deploy needed).
export const IMAGE_FALLBACK_MODELS = (process.env.VIDEO_IMAGE_FALLBACKS
  ? process.env.VIDEO_IMAGE_FALLBACKS.split(",").map((s) => s.trim()).filter(Boolean)
  : ["flux-kontext-pro", "nano-banana", "seedream-4-0"]);

async function imageWithAltModels(deps, base, tmpDir, cause) {
  const tried = base.imageModel || "nano-banana-hd";
  for (const model of IMAGE_FALLBACK_MODELS) {
    if (model === tried) continue;
    try {
      const v = await deps.generationClient.generateVisual({ ...base, kind: "image", imageModel: model }, tmpDir);
      v.kind = "image";
      v.fellBack = true;
      v.fallbackReason = `alt-image:${model} (after ${cause?.message || "fail"})`;
      return v;
    } catch { /* try the next provider */ }
  }
  return null;
}

// Last-resort visual: a local placeholder card so a scene with no real image
// still completes (visualStatus="fallback"). If the client can't make one (an
// old mock), preserve the original behaviour and let the failure propagate.
async function placeholderVisual(deps, base, tmpDir, cause) {
  if (typeof deps.generationClient.placeholderImage !== "function") throw cause;
  const v = await deps.generationClient.placeholderImage(base, tmpDir);
  v.kind = "image";
  v.fellBack = true;
  v.fallbackReason = `placeholder: ${cause.message}`;
  return v;
}

export async function visualProcessor(job, deps) {
  const { jobId, sceneIndex } = job.data;
  try {
    const meta = await deps.store.getMeta(jobId);
    const scene = await deps.store.getScene(jobId, sceneIndex);
    if (!meta || !scene) return { skipped: "missing" };
    if (JOB_TERMINAL.has(meta.status)) return { skipped: "job-terminal" };
    if (scene.visualStatus === "done" || scene.visualStatus === "fallback") return { skipped: "already-done" };
    if (meta.visualMode === "whiteboard") {
      // Whiteboard makes its OWN per-scene visual (Recraft vector SVG / raster+mask /
      // LLM diagram), revealed by the Remotion render. Each Recraft asset is metered
      // via /video/meter; lineart/diagram carry no Recraft meter. A failed asset
      // degrades the scene to handwriting (never kills the video).
      const genre = meta.whiteboardGenre || "lineart";
      // Plan-engine (Golpo-like): generate a per-scene whiteboard_visual_plan via the live
      // Visual Director (Python LLM route), validate it, and store it for the render phase.
      // Invalid/failed plan degrades the scene to handwriting (never kills the video).
      if ((process.env.WB_ENGINE || "legacy") === "plan") {
        try {
          const { validateWhiteboardPlan } = await import("./whiteboard/plan/validate.mjs");
          const narration = scene.text || scene.visualPrompt || "";
          const duration = Number(scene.estSeconds) || 8;
          const planKey = { genre, duration, narration };
          // Re-run the SAME script → reuse the cached plan, skipping the (paid) LLM entirely.
          let plan = await deps.store.getCachedPlan?.(planKey);
          const planFromCache = !!plan;
          if (!plan) {
            plan = await deps.generationClient?.generateWhiteboardPlan?.(
              { jobId, tenantId: meta.tenantId, userId: meta.userId },
              { narration, duration, genre, model: meta.genModel, language: meta.language, sceneId: `s${sceneIndex}` });
          }
          const v = plan ? validateWhiteboardPlan(plan) : { ok: false, errors: ["no plan returned"] };
          if (plan && v.ok) {
            if (v.warnings && v.warnings.length) console.warn(`[whiteboard-plan ${jobId}/${sceneIndex}] rendered with warnings: ${v.warnings.slice(0, 3).join("; ")}`);
            // Cache the RAW plan (pre-baking) so the value stays small; assets reuse via the asset cache.
            if (!planFromCache) await deps.store.setCachedPlan?.(planKey, plan);
            // Bake per-element assets into the plan (render phase stays dumb). Two modes:
            //  • genre "detail" → RASTER-REVEAL: a real Recraft photo + vectorized mask per
            //    element (2 paid calls/element — the genre's whole point: realistic).
            //  • else → generate-on-miss VECTOR icon ONLY for elements the FREE library
            //    (manifest + 1737 Lucide) misses (e.g. anatomy) → paid calls only on true gaps.
            // Best-effort per element: a failure leaves it to resolve as Lucide/generic.
            try {
              const { parseSvg, parseSvgShapes } = await import("./whiteboard/svg.mjs");
              const meters = [];
              if (genre === "detail") {
                // detail = ONE cohesive HERO illustration per SCENE (Golpo look), drawn on via a
                // LOCAL potrace line-trace reveal (free, no recraft). 1 flux image + 1 local trace per
                // scene → ~5× cheaper than the old per-element raster-reveal, and the whole scene is a
                // single realistic illustration that "draws" itself instead of scattered photos.
                const { traceMaskB64 } = await import("./whiteboard/visuals.mjs");
                const ctx = { jobId, tenantId: meta.tenantId, userId: meta.userId };
                const aspect = meta.aspectRatio === "9:16" ? "9:16" : "16:9";
                const heroQuery = ([plan.visual_metaphor, narration].filter(Boolean).join(". ").trim().slice(0, 300))
                  || plan.elements?.[0]?.asset_query || "scene";
                try {
                  const hkey = `${aspect}:${heroQuery}`;
                  const hit = await deps.store.getCachedAsset?.("hero", hkey); // cross-job reuse
                  let raster, maskViewBox = "0 0 1024 1024", maskShapes = [], source = "flux-hero", lic = "flux-kontext-pro:provider-terms";
                  if (hit && hit.raster) {
                    raster = hit.raster; maskViewBox = hit.maskViewBox || maskViewBox; maskShapes = hit.maskShapes || [];
                    source = "flux-hero-cache"; lic = hit.license || lic;
                  } else {
                    const b64 = await deps.generationClient?.generateWhiteboardRaster?.(ctx,
                      { query: heroQuery, provider: "flux", aspect, seed: 1000 + sceneIndex * 13, mode: "hero" });
                    if (b64) {
                      raster = "data:image/png;base64," + b64;
                      meters.push({ operation: "image", model: "flux-kontext-pro", units: { count: 1 } });
                      try { ({ maskViewBox, maskShapes } = await traceMaskB64(b64)); } // FREE local line-trace (no meter)
                      catch (te) { console.warn(`[whiteboard-plan ${jobId}/${sceneIndex}] hero trace failed (${te.message}) → full-image reveal`); }
                      await deps.store.setCachedAsset?.("hero", hkey,
                        { raster, maskViewBox, maskShapes, source, model: "flux-kontext-pro", license: lic, createdAt: new Date().toISOString() });
                    }
                  }
                  if (raster) {
                    // the whole scene = ONE full-canvas hero element that draws on
                    plan.elements = [{ id: "hero", type: "illustration", slot: "full_canvas",
                      raster, maskViewBox, maskStrokes: [], maskShapes, assetSource: source, license: lic }];
                    plan.beats = [{ start: 0, end: Math.max(1, Number(duration) || 6), action: "draw_icon", target: "hero" }];
                    plan.template = "single_concept"; plan.layout = "flow"; plan.camera = [];
                  }
                } catch (ge) {
                  console.warn(`[whiteboard-plan ${jobId}/${sceneIndex}] hero failed: ${ge.message}`);
                }
              } else {
                const { coveredByLibrary } = await import("./whiteboard/plan/resolver.mjs");
                const { generateRecraftIcon, isRecraftCreditSkip } = await import("./whiteboard/visuals.mjs");
                const kind = `icon-${genre}`; // genre-aware prompt → genre-aware cache
                for (const el of plan.elements || []) {
                  const q = el.asset_query || el.id;
                  if (coveredByLibrary(q)) continue;
                  try {
                    const hit = await deps.store.getCachedAsset?.(kind, q); // cross-job reuse
                    if (hit && hit.strokes) {
                      el.viewBox = hit.viewBox; el.strokes = hit.strokes; if (hit.shapes) el.shapes = hit.shapes;
                      el.assetSource = "recraft-cache"; el.license = hit.license || "recraft-v3-vector:provider-terms"; continue; // no Recraft, no meter
                    }
                    // GATE before the paid Recraft gen → at balance 0 skip it (free fallback) instead
                    // of debiting into the negative (same guard as flux/TTS).
                    if (!(await deps.generationClient?.gateUsage?.({ jobId, tenantId: meta.tenantId, userId: meta.userId }, "image", "recraft-v3-vector", { count: 1 }))) {
                      console.warn(`[whiteboard-plan ${jobId}/${sceneIndex}] recraft icon "${q}" skipped: insufficient credits → free fallback`); continue;
                    }
                    const { svg, meter } = await generateRecraftIcon(q, { genre, seed: 1000 + sceneIndex * 13 });
                    const parsed = parseSvg(svg, { dropBg: true, dropLight: true });
                    if (parsed.strokes && parsed.strokes.length) {
                      el.viewBox = parsed.viewBox; el.strokes = parsed.strokes; el.assetSource = "recraft"; el.license = "recraft-v3-vector:provider-terms";
                      // colored fills (so Recraft icons aren't thin outlines — drawn under the strokes)
                      const { shapes } = parseSvgShapes(svg, { dropBg: true });
                      if (shapes && shapes.length) el.shapes = shapes;
                      await deps.store.setCachedAsset?.(kind, q, { viewBox: parsed.viewBox, strokes: parsed.strokes, shapes,
                        source: "recraft", model: "recraft-v3-vector", license: "recraft-v3-vector:provider-terms", createdAt: new Date().toISOString() }); // §S provenance
                      if (meter) meters.push(meter);
                    }
                  } catch (ge) {
                    if (!isRecraftCreditSkip(ge.message)) console.warn(`[whiteboard-plan ${jobId}/${sceneIndex}] recraft icon "${q}" failed: ${ge.message}`);
                  }
                }
              }
              for (const m of meters) {
                await deps.generationClient?.meterUsage?.(
                  { jobId, tenantId: meta.tenantId, userId: meta.userId }, m.operation, m.model, m.units);
              }
            } catch (re) {
              console.warn(`[whiteboard-plan ${jobId}/${sceneIndex}] asset baking skipped: ${re.message}`);
            }
            await deps.store.setSceneFields(jobId, sceneIndex, {
              visualStatus: "done", visualKind: "whiteboard-plan", planJson: JSON.stringify(plan) });
            return { sceneIndex, genre, engine: "plan" };
          }
          console.warn(`[whiteboard-plan ${jobId}/${sceneIndex}] invalid plan → handwriting: ${(v.errors || []).slice(0, 2).join("; ")}`);
        } catch (e) {
          console.warn(`[whiteboard-plan ${jobId}/${sceneIndex}] failed → handwriting: ${e.message}`);
        }
        await deps.store.setSceneFields(jobId, sceneIndex, { visualStatus: "fallback", visualKind: "whiteboard", visualError: "plan unavailable" });
        return { sceneIndex, fellBack: true };
      }
      const tmpDir = jobTmpDir(jobId);
      await mkdir(tmpDir, { recursive: true });
      try {
        const { generateWhiteboardAsset } = await import("./whiteboard/visuals.mjs"); // lazy (worker-only)
        const a = await generateWhiteboardAsset(genre, {
          prompt: scene.visualPrompt || scene.text || "", tmpDir, sceneIndex,
          aspect: meta.aspectRatio || "16:9",
          // diagram genre: graph from Python (same LLM routing/failover + Model Narasi)
          diagramGraph: genre === "diagram"
            ? (desc) => deps.generationClient?.generateDiagramGraph?.(
                { jobId, tenantId: meta.tenantId, userId: meta.userId },
                { description: desc, model: meta.genModel, language: meta.language })
            : undefined,
        });
        for (const m of a.meters || []) {
          await deps.generationClient?.meterUsage?.(
            { jobId, tenantId: meta.tenantId, userId: meta.userId }, m.operation, m.model, m.units);
        }
        const up = a.visualPath ? await maybeUpload(jobId, meta.tenantId, "images", a.visualPath) : { path: undefined, key: null };
        const mk = a.maskPath ? await maybeUpload(jobId, meta.tenantId, "images", a.maskPath) : { path: undefined, key: null };
        await deps.store.setSceneFields(jobId, sceneIndex, {
          visualStatus: "done", visualKind: a.kind || "whiteboard",
          ...(up.path ? { visualPath: up.path } : {}), ...(up.key ? { visualKey: up.key } : {}),
          ...(mk.path ? { maskPath: mk.path } : {}), ...(mk.key ? { maskKey: mk.key } : {}),
        });
        return { sceneIndex, genre };
      } catch (e) {
        console.warn(`[whiteboard ${jobId}/${sceneIndex}] ${genre} asset failed: ${e.message} → handwriting`);
        await deps.store.setSceneFields(jobId, sceneIndex, { visualStatus: "fallback", visualKind: "whiteboard", visualError: e.message });
        return { sceneIndex, fellBack: true };
      }
    }
    const tmpDir = jobTmpDir(jobId);
    await mkdir(tmpDir, { recursive: true });
    const refImage = await resolveAnchor(jobId, tmpDir, meta);  // per-video reference (or null)
    const base = {
      jobId, sceneIndex, kind: scene.kind, visualPrompt: scene.visualPrompt,
      estSeconds: Number(scene.estSeconds), clipModel: meta.clipModel,
      imageModel: meta.imageModel || undefined, aspectRatio: meta.aspectRatio || "16:9",
      seed: hashSeed(jobId),   // same seed for all scenes of this video → steadier look
      refImage: refImage || undefined,   // anchor → ref_image on image gen (consistent character)
      tenantId: meta.tenantId, userId: meta.userId,
    };
    let v;
    try {
      v = await deps.generationClient.generateVisual(base, tmpDir);
    } catch (primaryErr) {
      let cause = primaryErr;
      // Tier 0 (IMAGE only) — retry the SAME primary model before downgrading. The main
      // failure in prod is Gemini (nano-banana-hd) returning 502 NO_IMAGE, which is
      // TRANSIENT (an immediate retry succeeds ~always). Retrying the primary keeps the
      // chosen model + correct aspect, instead of dropping to a 1:1/2K-square fallback
      // that the cover-crop then chops. Clips are NOT retried here (a Veo re-poll is ~4min;
      // they take the clip→image path below). Env: VIDEO_IMAGE_RETRIES (default 1).
      const imgRetries = Math.max(0, Number(process.env.VIDEO_IMAGE_RETRIES || 1));
      if (scene.kind !== "clip") {
        for (let attempt = 1; attempt <= imgRetries && !v; attempt++) {
          try { v = await deps.generationClient.generateVisual(base, tmpDir); }
          catch (e) { cause = e; }
        }
      }
      // Tier 1 — a failed CLIP retries as a real image on its default model.
      if (!v && scene.kind === "clip") {
        try {
          v = await deps.generationClient.generateVisual({ ...base, kind: "image" }, tmpDir);
          v.kind = "image";
          v.fellBack = true;
          v.fallbackReason = `clip→image: ${primaryErr.message}`;
        } catch (imgErr) { cause = imgErr; }
      }
      // Tier 2 — the default image failed too (or this was already an image). Try
      // OTHER providers so a flaky/blocked model still yields a REAL image, not a card.
      if (!v) v = await imageWithAltModels(deps, base, tmpDir, cause);
      // Tier 3 — genuinely nothing generated: a minimal placeholder so the video
      // still completes (last resort; the alt-model retries make this very rare).
      if (!v) v = await placeholderVisual(deps, base, tmpDir, cause);
    }
    const up = await maybeUpload(jobId, meta.tenantId, v.kind === "clip" ? "video" : "images", v.path);
    await deps.store.setSceneFields(jobId, sceneIndex, {
      visualStatus: v.fellBack ? "fallback" : "done", visualPath: up.path, visualKey: up.key, visualKind: v.kind,
      ...(v.fellBack && v.fallbackReason ? { visualError: v.fallbackReason } : {}),
    });
    return { sceneIndex, ...(v.fellBack ? { fellBack: true } : {}) };
  } catch (e) {
    await deps.store.setSceneFields(jobId, sceneIndex, { visualStatus: "failed", visualError: e.message }).catch(() => {});
    return { sceneIndex, failed: e.message };
  } finally {
    await deps.queues.check.add("check", { jobId }).catch(() => {});
  }
}

export async function checkProcessor(job, deps) {
  return advance(job.data.jobId, deps);
}

export async function stitchProcessor(job, deps) {
  const { jobId } = job.data;
  const meta = await deps.store.getMeta(jobId);
  if (!meta) return { skipped: true };
  const tmpDir = jobTmpDir(jobId);
  try {
    await mkdir(tmpDir, { recursive: true });
    const scenesRaw = await deps.store.getScenes(jobId, meta.sceneCount);
    // A breath between scenes: pad each scene with trailing silence so narrations
    // don't run back-to-back (the acrossfade used to OVERLAP them → "mepet"). The
    // visual holds (image Ken Burns / clip freeze-pad) through the pause.
    // 0.5s read as a "patah" (long silence) once -af apad started honouring it; 0.25s = a natural beat
    // between narrations without running them together. Tune via VIDEO_SCENE_GAP (0 = back-to-back).
    const sceneGap = Math.max(0, Number(process.env.VIDEO_SCENE_GAP || 0.25));
    const scenes = [];
    for (let i = 0; i < scenesRaw.length; i++) {
      const s = scenesRaw[i];
      // whiteboard scenes may carry NO pipeline visual asset (the Remotion render makes
      // its own) — don't demand one; other modes always have a visual to resolve.
      const wbNoAsset = meta.visualMode === "whiteboard" && !s.visualKey && !s.visualPath;
      scenes.push({
        kind: s.visualKind || "image",
        duration: (Number(s.durationActual) || Number(s.estSeconds) || 2) + sceneGap,
        text: s?.text || "",   // per-scene caption (long-video render path)
        visualPath: wbNoAsset ? undefined : await resolveLocal(jobId, tmpDir, s.visualKey, s.visualPath,
          `vis_${i}.${s.visualKind === "clip" ? "mp4" : "png"}`),
        // detail genre also carries a vectorized reveal mask (whiteboard only)
        ...(meta.visualMode === "whiteboard" && (s.maskKey || s.maskPath)
          ? { maskPath: await resolveLocal(jobId, tmpDir, s.maskKey, s.maskPath, `mask_${i}.svg`) }
          : {}),
        // whiteboard plan-engine: per-scene visual plan (built in the visual phase)
        ...(s.planJson ? { planJson: s.planJson } : {}),
        audioPath: await resolveLocal(jobId, tmpDir, s.audioKey, s.audioPath, `aud_${i}.wav`),
      });
    }
    const outPath = join(tmpDir, "out.mp4");
    const stitchOpts = { cwd: tmpDir };
    if (meta.aspectRatio === "9:16") { stitchOpts.width = 1080; stitchOpts.height = 1920; } // portrait
    if (meta.captions && (await hasSubtitlesFilter())) {
      // Step 6e: burn captions from the KNOWN script + measured timing (no ASR), as a
      // fully-styled .ass (font/outline/shadow/wrap) — see buildAss.
      const { writeFile } = await import("node:fs/promises");
      const xfade = Number(process.env.VIDEO_XFADE || 0.5);
      const assOpts = { width: stitchOpts.width, height: stitchOpts.height, captionFont: meta.captionFont };
      const ass = buildAssFromScenes(scenesRaw.map((s) => s?.text || ""), scenes.map((s) => s.duration), xfade, assOpts);
      await writeFile(join(tmpDir, "captions.ass"), ass, "utf8");
      stitchOpts.ass = "captions.ass";       // single-pass path (≤ threshold scenes)
      stitchOpts.captions = true;            // per-scene path builds its own per-scene .ass
      if (meta.captionFont) stitchOpts.captionFont = meta.captionFont;
    } else if (meta.captions) {
      console.warn(`[stitch ${jobId}] captions requested but ffmpeg has no 'subtitles' filter (no libass) — rendering without burn-in`);
    }
    let result;
    if (meta.visualMode === "whiteboard") {
      // Opt B: render the WHOLE video with the Remotion whiteboard engine instead of
      // the ffmpeg stitch. render.mjs is imported LAZILY here (worker-only) so the API
      // process never loads @remotion/Chromium at startup.
      if ((process.env.WB_ENGINE || "legacy") === "plan") {
        // Golpo-like plan engine: per-scene visual_plan → resolve → multi-scene render.
        // BACKEND is pluggable (Guide-2 §K/§L): default Remotion (proven); WB_RENDER_BACKEND=svg_ffmpeg
        // routes to the Chromium-free SVG/FFmpeg renderer. Falls back to Remotion on any svg-backend error.
        const backend = (process.env.WB_RENDER_BACKEND || "remotion").toLowerCase();
        if (backend === "svg_ffmpeg") {
          try {
            const { renderWhiteboardPlanSvg } = await import("./whiteboard/renderers/svgFfmpeg.mjs");
            result = await renderWhiteboardPlanSvg(scenes, { ...meta, jobId }, outPath, { tmpDir });
          } catch (be) {
            console.warn(`[stitch ${jobId}] svg_ffmpeg backend failed (${be.message}) → Remotion fallback`);
            const { renderWhiteboardPlan } = await import("./whiteboard/render.mjs");
            result = await renderWhiteboardPlan(scenes, { ...meta, jobId }, outPath, { tmpDir });
          }
        } else {
          const { renderWhiteboardPlan } = await import("./whiteboard/render.mjs");
          try {
            result = await renderWhiteboardPlan(scenes, { ...meta, jobId }, outPath, { tmpDir });
          } catch (be) {
            // Remotion (Chromium) can OOM-crash on a long/heavy render ("Target closed"). Auto-fall
            // back to the Chromium-free SVG/FFmpeg backend so the video STILL completes (slower, never
            // a browser crash). → Remotion stays the fast default; this is its safety net.
            console.warn(`[stitch ${jobId}] Remotion backend failed (${be.message}) → svg_ffmpeg fallback`);
            const { renderWhiteboardPlanSvg } = await import("./whiteboard/renderers/svgFfmpeg.mjs");
            result = await renderWhiteboardPlanSvg(scenes, { ...meta, jobId }, outPath, { tmpDir });
          }
        }
      } else {
        const { renderWhiteboard } = await import("./whiteboard/render.mjs");
        result = await renderWhiteboard(scenes, meta, outPath, { tmpDir });
      }
      // §N non-fatal QA: probe the produced MP4 (exists, video stream, duration ≈ expected)
      try {
        const { validateRenderedClip } = await import("./whiteboard/qa.mjs");
        const clipQA = await validateRenderedClip(outPath, { expectedDuration: result?.duration || 0 });
        if (!clipQA.ok || clipQA.warnings.length) console.warn(`[stitch ${jobId}] rendered-clip QA: ${[...clipQA.errors, ...clipQA.warnings].slice(0, 4).join("; ")} (streams: ${clipQA.streams})`);
      } catch (qe) { console.warn(`[stitch ${jobId}] clip QA skipped: ${qe.message}`); }
      // flat render fee — 3 credits/sec of output video (post-hoc, tagged for refund)
      await deps.generationClient?.meterUsage?.(
        { jobId, tenantId: meta.tenantId, userId: meta.userId },
        "video", "whiteboard", { seconds: Math.max(1, Math.round(result.duration || 0)) });
    } else {
      try {
        result = await stitch(scenes, outPath, stitchOpts);
      } catch (e) {
        // a font / force_style issue must never fail the whole video — retry once
        // without the custom font so captions just render in the default face.
        if (stitchOpts.captionFont) {
          console.warn(`[stitch ${jobId}] retrying without captionFont after: ${e.message}`);
          delete stitchOpts.captionFont;
          result = await stitch(scenes, outPath, stitchOpts);
        } else throw e;
      }
    }
    // upload the final MP4 under the lifecycle-managed `videos/` prefix (Step 6f)
    let up = { path: outPath, key: null };
    const s = await storage();
    if (s) {
      const { readFile } = await import("node:fs/promises");
      const key = s.videoKey(meta.tenantId, jobId);
      await s.uploadBytes(key, await readFile(outPath), "video/mp4");
      up = { path: outPath, key };
    }
    await deps.store.setStatus(jobId, "done", {
      mp4Path: up.path, mp4Key: up.key, durationActual: result.duration, progress: 100,
    });
    // if the deliverable is safely in R2, the local scratch is disposable
    if (up.key) await cleanupJobTmp(jobId).catch(() => {});
    return { jobId, duration: result.duration, mp4: up.key || up.path };
  } catch (e) {
    await deps.store.setStatus(jobId, "failed", { error: `stitch: ${e.message}` }).catch(() => {});
    await deps.credits?.refundJob?.(meta.tenantId, jobId);
    await cleanupJobTmp(jobId).catch(() => {});
    throw e;
  }
}

/** Build the deps the processors share. */
export function makeDeps({ generationClient, queues } = {}) {
  return {
    store,
    queues: queues || makeQueues(),
    generationClient,
    credits: {
      async precheck() { return true; }, // pre-check seam (Python meters per scene)
      // Refund a failed assembly's actual spend (idempotent on the Python side).
      // Best-effort: a refund hiccup must never throw inside the fail path.
      async refundJob(tenantId, jobId) {
        try { return await generationClient?.refundVideoJob?.(tenantId, jobId); }
        catch (e) { console.warn(`[video-refund ${jobId}] failed: ${e.message}`); }
      },
    },
  };
}

/** Start the four BullMQ workers. Returns { workers, close() }. */
export function startWorkers(deps) {
  const mk = (name, processor) =>
    new Worker(name, (job) => processor(job, deps), {
      connection: makeConnection(), concurrency: CONCURRENCY[name] || 5,
      // Long renders + (synchronous) potrace tracing can stall the event loop well past BullMQ's
      // 30s default lock → "could not renew lock" + false-stall RE-RUNS (double work/charge). Give a
      // generous lock (renewal fires at lockDuration/2, so 5min headroom tolerates multi-second stalls).
      lockDuration: Number(process.env.WB_LOCK_DURATION_MS) || 600000,
      stalledInterval: 60000,
    });
  const audio = mk(QUEUE.AUDIO, audioProcessor);
  const visual = mk(QUEUE.VISUAL, visualProcessor);
  const check = mk(QUEUE.CHECK, checkProcessor);
  const stitch = mk(QUEUE.STITCH, stitchProcessor);

  // Safety net: an exhausted or stalled audio/visual job must still leave a
  // TERMINAL scene status + enqueue a check, so a job can never hang on a
  // perpetually-'pending' scene. Never overwrites a successful 'done'/'fallback'.
  const netFor = (field) => async (job, err) => {
    if (!job?.data) return;
    const { jobId, sceneIndex } = job.data;
    try {
      const scene = await deps.store.getScene(jobId, sceneIndex);
      const cur = scene?.[field];
      if (cur !== "done" && cur !== "fallback") {
        const errField = field === "audioStatus" ? "audioError" : "visualError";
        await deps.store.setSceneFields(jobId, sceneIndex, { [field]: "failed", [errField]: err?.message || "worker failed" });
      }
      await deps.queues.check.add("check", { jobId });
    } catch { /* best effort */ }
  };
  audio.on("failed", netFor("audioStatus"));
  visual.on("failed", netFor("visualStatus"));

  const workers = [audio, visual, check, stitch];
  return {
    workers,
    async close() { await Promise.all(workers.map((w) => w.close())); },
  };
}

export async function cleanupJobTmp(jobId) {
  await rm(jobTmpDir(jobId), { recursive: true, force: true }).catch(() => {});
}
