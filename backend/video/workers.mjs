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
import { existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { QUEUE, CONCURRENCY, makeConnection } from "./connection.mjs";
import * as store from "./store.mjs";
import { ffprobeDuration, stitch, buildAssFromScenes, hasSubtitlesFilter } from "./ffmpeg.mjs";
import { advance } from "./orchestrator.mjs";
import { startRecovery } from "./recovery.mjs";
import { rm } from "node:fs/promises";

// __dirname for ESM (needed to load the tier catalog next to this file).
const __dirname = dirname(fileURLToPath(import.meta.url));

// Recraft tier catalog — loaded ONCE at module load. Drives tier-based routing
// (visualProcessor + stitchProcessor) so a scene's tier fully determines engine,
// renderer, ladder, meter model, and the picked Recraft style/template.
// If the catalog is missing/malformed we log and fall back to genre-only legacy
// routing (backward compatible with existing prod jobs that carry no tier).
let RECRAFT_CATALOG = null;
let TIER_MAP = {};
try {
  RECRAFT_CATALOG = JSON.parse(readFileSync(join(__dirname, "whiteboard/recraft_catalog.json"), "utf8"));
  TIER_MAP = Object.fromEntries(RECRAFT_CATALOG.tiers.map((t) => [t.tier, t]));
} catch (e) {
  console.warn(`[workers] recraft_catalog.json load failed → legacy genre-only routing: ${e.message}`);
}

/**
 * Resolve tier-based routing for a whiteboard scene.
 *
 * Reads scene/job meta (tier + user-picked substyles) and returns the full routing
 * envelope used by visualProcessor + stitchProcessor. Backward-compatible: if
 * `meta.whiteboardTier` is unset OR the catalog is unavailable OR the tier isn't
 * in the catalog, we return {engine:"legacy", ...} so callers take the historic
 * genre-driven path (unchanged prod behaviour for pre-tier jobs).
 *
 * Fields returned:
 *   tier           — tier id (e.g. "ultra_color"); null on legacy
 *   tierDef        — the raw catalog entry for tier; null on legacy
 *   engine         — "DrawReveal" | "SceneView" | "WhiteboardPlan" | "legacy"
 *   renderer       — "remotion" | "svg_ffmpeg" (for tiered scenes)
 *   genre          — "color" | "detail" | "plan"  (from the tier)
 *   model          — Recraft model id (e.g. "recraftv3_vector")
 *   meterModel     — meter model id used for /video/meter (e.g. "recraft-v3-vector")
 *   meterUsd       — unit cost the tier's Recraft calls charge at (per catalog)
 *   apiStyle       — single-call tiers (premium/regular): the picked V2/V3 style
 *                    ultra: the picked V2 Vector style (used as recraftStyle in plan-mode)
 *                    lite: null (plan-mode uses iconStyle for the fallback)
 *   template       — multi-call tiers (ultra/lite): picked template snake_case; else null
 *   iconStyle      — lite: the picked V2-Icon fallback style; ultra: same as apiStyle; else null
 *   elementCount   — multi-call tiers: template's element_count; else null
 *   ladder         — tier's ladder (["cache","recraft"] for premium/regular/ultra,
 *                    full ladder for lite)
 */
export function resolveWBVariant(meta) {
  const tierId = meta && meta.whiteboardTier;
  const tierDef = tierId && TIER_MAP[tierId];
  if (!tierDef) {
    // Legacy path — pre-tier jobs, or an unknown tier (e.g. catalog missed on this
    // worker replica). Callers must fall through to the historic genre-driven code.
    return {
      tier: null, tierDef: null, engine: "legacy", renderer: null,
      genre: meta?.whiteboardGenre || "lineart", model: null, meterModel: null, meterUsd: 0,
      apiStyle: null, template: null, iconStyle: null, elementCount: null, ladder: null,
    };
  }
  // meta.whiteboardApiStyle = the user's picked substyle (single-call tiers: Premium/Regular Color+Detail).
  // meta.whiteboardTemplate = the user's picked template (multi-call tiers: ultra/lite).
  // meta.whiteboardStyle    = the user's picked per-element style — Ultra's V2 Vector style ("Cartoon" etc)
  //                           OR Lite's V2-Icon fallback style ("Pictogram" etc). FE sends the SAME body
  //                           field (whiteboard_style) for both — the tier disambiguates. Prior code
  //                           read meta.whiteboardIconStyle which the FE never populated → Lite's icon
  //                           picker was silently ignored (always fell to firstSubOf → "Icon").
  // Any missing pick falls back to the FIRST substyle in the relevant category → deterministic default.
  const pickedApi = meta.whiteboardApiStyle || null;
  const pickedTemplate = meta.whiteboardTemplate || null;
  const pickedStyle = meta.whiteboardStyle || null;
  const catByKey = (k) => (tierDef.categories || []).find((c) => c.key === k) || null;
  const firstSubOf = (cat) => (cat && cat.substyles && cat.substyles[0]) || null;
  const findSub = (cat, api) => (cat && cat.substyles || []).find((s) => s.api_style === api) || null;

  let apiStyle = null, template = null, iconStyle = null, elementCount = null;
  if (tierId === "ultra_color") {
    // Ultra: multi-call plan → picks BOTH a template AND a V2 Vector style (whiteboardStyle);
    // iconStyle = apiStyle since every element uses the SAME V2 Vector style.
    const templateCat = catByKey("template");
    const styleCat = catByKey("style");
    const tSub = findSub(templateCat, pickedTemplate) || firstSubOf(templateCat);
    const sSub = findSub(styleCat, pickedStyle) || firstSubOf(styleCat);
    template = tSub ? tSub.api_style : null;
    elementCount = tSub && tSub.element_count != null ? tSub.element_count : null;
    apiStyle = sSub ? sSub.api_style : null;
    iconStyle = apiStyle;
  } else if (tierId === "lite_color") {
    // Lite: multi-call plan → picks a template + a V2-Icon fallback style (whiteboardStyle).
    // apiStyle stays null; recraftOnly:false path uses iconStyle only when the free ladder misses.
    const templateCat = catByKey("template");
    const iconCat = catByKey("icon_style");
    const tSub = findSub(templateCat, pickedTemplate) || firstSubOf(templateCat);
    const iSub = findSub(iconCat, pickedStyle) || firstSubOf(iconCat);
    template = tSub ? tSub.api_style : null;
    elementCount = tSub && tSub.element_count != null ? tSub.element_count : null;
    iconStyle = iSub ? iSub.api_style : null;
    apiStyle = null;
  } else {
    // Single-call tiers (premium_color / premium_detail / regular_color / regular_detail):
    // one Recraft call per scene with a picked style out of the tier's category(-ies).
    // The tier has one or more style categories; pick from whichever contains the api_style.
    let sub = null;
    for (const cat of (tierDef.categories || [])) {
      const hit = findSub(cat, pickedApi);
      if (hit) { sub = hit; break; }
    }
    if (!sub) sub = firstSubOf(tierDef.categories?.[0]);
    apiStyle = sub ? sub.api_style : null;
  }

  return {
    tier: tierId,
    tierDef,
    engine: tierDef.engine,        // "DrawReveal" | "SceneView" | "WhiteboardPlan"
    renderer: tierDef.renderer,    // "remotion" | "svg_ffmpeg"
    genre: tierDef.genre,          // "color" | "detail" | "plan"
    model: tierDef.recraft?.model || null,
    meterModel: tierDef.recraft?.meter_model || null,
    meterUsd: Number(tierDef.recraft?.meter_usd) || 0,
    apiStyle,
    template,
    iconStyle,
    elementCount,
    ladder: tierDef.ladder || null,
  };
}
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
    const probed = await ffprobeDuration(a.path);
    const duration = probed || a.durationSeconds || Number(scene.estSeconds) || 0;
    if (!probed && !a.durationSeconds) {
      // Neither a real measurement nor a provider-declared length → we fall back to the LLM's
      // pre-synthesis estSeconds, which UNDER-predicts dense narration and used to clip the scene
      // tail via the renderer's -shortest. The renderer now re-measures the audio and extends the
      // window, but surface this so the root (ffprobe-null / Gemini-TTS returns no duration) is
      // findable instead of silent. (Rino: "audio kepotong di awal scene N")
      console.warn(`[audio ${jobId}/${sceneIndex}] no measured/declared audio duration → estSeconds=${Number(scene.estSeconds) || 0}s (renderer re-measures & extends to avoid a tail-cut)`);
    }
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
// Snapshot the visual-worker flag ONCE at worker startup (#10) — reading process.env per-scene could
// otherwise let two scenes of one video pick different strategies across a rolling restart mid-render.
// A process's env is fixed for its lifetime, so one read suffices.
const VI_VISUAL_WORKER_ON = process.env.VI_VISUAL_WORKER_ENABLED === "1";

// Memoize the PROMISE (not the result) so concurrent scenes of ONE job share a SINGLE download
// instead of racing on the per-job temp file / re-downloading. The inner fn catches every error and
// returns b64|null, so the cached promise ALWAYS resolves (never a cached rejection).
const _anchorCache = new Map(); // jobId -> Promise<base64 | null>
// Backstop bound: cleanupJobTmp (#8) evicts on the stitch success/failure paths, but a job that fails
// or is canceled BEFORE stitch (and a stitch-success with no R2) never reaches it — cap the map so
// those can't leak unboundedly. Evicting a still-active entry only forces a cheap re-resolve.
const _ANCHOR_CACHE_MAX = 200;
function resolveAnchor(jobId, tmpDir, meta) {
  if (_anchorCache.has(jobId)) return _anchorCache.get(jobId);
  if (_anchorCache.size >= _ANCHOR_CACHE_MAX) _anchorCache.delete(_anchorCache.keys().next().value);
  const p = (async () => {
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
    return b64;
  })();
  _anchorCache.set(jobId, p);
  return p;
}

// Generic vocabulary that often forms part of a multi-word cast/place name ("Gunung Salak") but is
// NOT distinctive — mirrors python _CAST_TOKEN_STOPWORDS so the JS anchor gate agrees with the python
// text filter (a token here must never proxy for the whole name → no generic-word bleed).
const _CAST_TOKEN_STOPWORDS = new Set([
  "gunung","pantai","danau","sungai","laut","hutan","istana","rumah","jalan","kota","desa","pulau",
  "kampung","pasar","candi","masjid","benteng","menara","lembah","bukit","wilayah","daerah","negeri",
  "kerajaan","wanita","manusia","orang","anak","perempuan","lelaki","besar","kecil","tinggi","tua",
  "muda","agung","raya","utama","tempat","warga","raja","ratu","putri","pangeran","sultan","tanah",
  "kebun","ladang","tepi","lereng","gedung","pelabuhan","stasiun","teluk","selat","telaga","sawah",
  "jembatan","taman","makam","keraton","pendopo","balai","puncak","kawah","muara","dusun","nagari",
  "benua","samudra","samudera","pohon","gerbang","pintu","alun","pasir","rawa","sumur",
  "mountain","river","village","palace","temple","castle","forest",
  "island","great","little","young","old","city","town","house","place","king","queen","prince",
  "woman","man","people","child","land","field",
]);

// #2: does this scene's narration NAME a recurring cast member? Mirrors the python word-boundary
// name-filter (laozhang_api /video/visual-prompt): whole-word, plus a distinctive token (len>=4, not
// a stopword) for partial refs. Returns true/false, or null when there's no cast registry to judge by.
function _sceneNamesCast(sceneText, visualCastJson) {
  if (!sceneText || !visualCastJson) return null;
  let cast;
  try { cast = JSON.parse(visualCastJson); } catch { return null; }
  const names = [...(cast.characters || []), ...(cast.locations || [])]
    .map((c) => ((c && c.name) || "").trim()).filter((n) => n.length >= 3);
  if (!names.length) return null;
  const lc = sceneText.toLowerCase();
  const wb = (s) => {
    const e = s.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    try { return new RegExp(`(?<![\\p{L}\\p{N}'])${e}(?![\\p{L}\\p{N}'])`, "u").test(lc); }
    catch { return lc.includes(s.toLowerCase()); }
  };
  return names.some((n) => wb(n) || n.toLowerCase().split(/\s+/)
    .some((t) => t.length >= 4 && !_CAST_TOKEN_STOPWORDS.has(t) && wb(t)));
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
      //
      // Tier-based routing (catalog): if meta.whiteboardTier is set + in the catalog,
      // the tier's engine drives the branch — Ultra/Lite (WhiteboardPlan) FORCE the
      // plan-mode path regardless of process.env.WB_ENGINE (prod-env-unchanged).
      // Premium/Regular Color+Detail (DrawReveal/SceneView) take the single-call
      // path with tier-picked model + apiStyle. Pre-tier jobs keep the legacy
      // WB_ENGINE-gated behaviour byte-for-byte.
      const variant = resolveWBVariant(meta);
      const genre = (variant.engine !== "legacy" ? variant.genre : (meta.whiteboardGenre || "lineart"));
      // Ultra/Lite tiers force plan-mode; else honour the historic env gate.
      const usePlanEngine = variant.engine === "WhiteboardPlan"
        || (variant.engine === "legacy" && (process.env.WB_ENGINE || "legacy") === "plan");
      // Plan-engine (Golpo-like): generate a per-scene whiteboard_visual_plan via the live
      // Visual Director (Python LLM route), validate it, and store it for the render phase.
      // Invalid/failed plan degrades the scene to handwriting (never kills the video).
      if (usePlanEngine) {
        try {
          const { validateWhiteboardPlan } = await import("./whiteboard/plan/validate.mjs");
          const narration = scene.text || scene.visualPrompt || "";
          const duration = Number(scene.estSeconds) || 8;
          const planKey = { genre, duration, narration };
          // Re-run the SAME script → reuse the cached plan, skipping the (paid) LLM entirely.
          let plan = await deps.store.getCachedPlan?.(planKey);
          const planFromCache = !!plan;
          if (!plan) {
            // Tier-aware plan opts (Ultra/Lite): Ultra = recraftOnly (V2 Vector on every element,
            // no free-icon fallback); Lite = recraftOnly:false with the free ladder + Recraft
            // V2-Icon fallback style. Legacy jobs (variant.engine === "legacy") pass no tier hints
            // → generateWhiteboardPlan behaves exactly as before this change.
            const _tierPlanOpts = variant.tier === "ultra_color"
              ? { tier: variant.tier, template: variant.template, recraftOnly: true,
                  recraftModel: variant.model, recraftStyle: variant.apiStyle,
                  meterModel: variant.meterModel, elementCount: variant.elementCount,
                  ladder: variant.ladder }
              : variant.tier === "lite_color"
              ? { tier: variant.tier, template: variant.template, recraftOnly: false,
                  recraftModel: variant.model, iconFallbackStyle: variant.iconStyle,
                  meterModel: variant.meterModel, elementCount: variant.elementCount,
                  ladder: variant.ladder }
              : {};
            plan = await deps.generationClient?.generateWhiteboardPlan?.(
              { jobId, tenantId: meta.tenantId, userId: meta.userId },
              { narration, duration, genre, model: meta.genModel, language: meta.language, sceneId: `s${sceneIndex}`,
                ..._tierPlanOpts });
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
                const { traceMaskB64, vectorizeMaskB64 } = await import("./whiteboard/visuals.mjs");
                const ctx = { jobId, tenantId: meta.tenantId, userId: meta.userId };
                const aspect = meta.aspectRatio === "9:16" ? "9:16" : "16:9";
                const heroQuery = ([plan.visual_metaphor, narration].filter(Boolean).join(". ").trim().slice(0, 300))
                  || plan.elements?.[0]?.asset_query || "scene";
                try {
                  // include provider + heroStyle + mask-mode in the key so switching the hero look,
                  // model, OR mask (potrace↔recraft) re-generates instead of reusing a stale cached hero.
                  const _maskMode = (process.env.WB_HERO_MASK || "potrace").toLowerCase();   // potrace (free) | recraft ($0.01, cleaner)
                  const hkey = `${aspect}:${process.env.WB_RASTER_PROVIDER || "flux"}:${meta.heroStyle || ""}:${_maskMode}:${heroQuery}`;
                  const hit = await deps.store.getCachedAsset?.("hero", hkey); // cross-job reuse
                  let raster, maskViewBox = "0 0 1024 1024", maskShapes = [], source = "flux-hero", lic = "flux-kontext-pro:provider-terms";
                  if (hit && hit.raster) {
                    raster = hit.raster; maskViewBox = hit.maskViewBox || maskViewBox; maskShapes = hit.maskShapes || [];
                    source = "flux-hero-cache"; lic = hit.license || lic;
                  } else {
                    // WB_RASTER_PROVIDER (video-worker env) switches the detail-hero image model:
                    // "flux" (default → flux-kontext-pro, honors 16:9/9:16) | "nano-banana-hd" | etc.
                    // (any IMAGE_MODELS key the python /video/whiteboard-raster route accepts).
                    // B: bound a STALLED hero gen (one scene took 188s = the 180s upstream timeout)
                    // + retry once → a slow image no longer gates the whole asset phase. Both fail →
                    // fall through to the icon fallback (raster stays undefined). Tunable WB_HERO_*.
                    const _heroTimeout = Number(process.env.WB_HERO_TIMEOUT_MS) || 75000;
                    const _heroTries = Math.max(1, Number(process.env.WB_HERO_RETRIES || 1) + 1);
                    // meter the ACTUAL provider (WB_RASTER_PROVIDER), not a hardcoded flux — mirrors
                    // python /video/whiteboard-raster (flux→flux-kontext-pro, else the provider id) so
                    // the charge uses the real model's price (nano-banana-2-hd ≠ flux-kontext-pro).
                    const _heroProvider = process.env.WB_RASTER_PROVIDER || "flux";
                    const _heroModel = _heroProvider === "flux" ? "flux-kontext-pro" : _heroProvider;
                    let b64 = null;
                    for (let _a = 1; _a <= _heroTries; _a++) {
                      try {
                        b64 = await deps.generationClient?.generateWhiteboardRaster?.(ctx,
                          { query: heroQuery, provider: _heroProvider, aspect, seed: 1000 + sceneIndex * 13, mode: "hero", timeoutMs: _heroTimeout, heroStyle: meta.heroStyle });
                        if (b64) break;
                      } catch (he) {
                        console.warn(`[whiteboard-plan ${jobId}/${sceneIndex}] hero gen attempt ${_a}/${_heroTries} failed: ${he.message}`);
                        if (_a === _heroTries) throw he; // outer catch logs + falls back to icons
                      }
                    }
                    if (b64) {
                      raster = "data:image/png;base64," + b64;
                      meters.push({ operation: "image", model: _heroModel, units: { count: 1 } });
                      // reveal mask: WB_HERO_MASK=recraft → Recraft vectorize ($0.01, cleaner segmented
                      // shapes) on the hero PNG; else potrace (FREE). Recraft out-of-credits (breaker) or
                      // any error → fall back to potrace so a render never fails on the mask.
                      let _masked = false;
                      if (_maskMode === "recraft") {
                        try {
                          const vm = await vectorizeMaskB64(b64);
                          maskViewBox = vm.maskViewBox; maskShapes = vm.maskShapes;
                          if (vm.meter) meters.push(vm.meter);   // recraft-vectorize $0.01
                          _masked = true;
                        } catch (ve) { console.warn(`[whiteboard-plan ${jobId}/${sceneIndex}] recraft mask failed (${ve.message}) → potrace`); }
                      }
                      if (!_masked) {
                        try { ({ maskViewBox, maskShapes } = await traceMaskB64(b64)); } // FREE local line-trace (no meter)
                        catch (te) { console.warn(`[whiteboard-plan ${jobId}/${sceneIndex}] hero trace failed (${te.message}) → full-image reveal`); }
                      }
                      await deps.store.setCachedAsset?.("hero", hkey,
                        { raster, maskViewBox, maskShapes, source, model: _heroModel, license: lic, createdAt: new Date().toISOString() });
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
                // Tier-aware ladder + Recraft params (2026-07-05 catalog build):
                //  - Ultra: recraftOnly=true → SKIP the free-lib check entirely, every element goes
                //    to Recraft V2 Vector with the user-picked style (Cartoon / Vector art / etc.).
                //  - Lite: recraftOnly=false → free-lib ladder first; Recraft V2-Vector-Icon fallback
                //    with the user-picked icon style (Pictogram / Colored shape / etc.).
                //  - Legacy pre-tier jobs (variant.engine === "legacy"): historic behavior — free lib
                //    first, Recraft V3-Vector default fallback.
                const _isUltra = variant.tier === "ultra_color";
                const _recraftModel = variant.model || "recraftv3_vector";
                const _recraftStyle = _isUltra ? (variant.apiStyle || null) : (variant.iconStyle || null);
                const _meterModel = variant.meterModel
                  || (_recraftModel === "recraftv2_vector" ? "recraft-v2-vector"
                    : _recraftModel === "recraftv3_vector" ? "recraft-v3-vector"
                    : "recraft-v3-vector");
                const _license = `${_meterModel}:provider-terms`;
                // Cache key includes tier+model+style so Ultra "Cartoon" doesn't collide with
                // Regular Color "Cartoon" (both V2 Vector but different pipelines) — and so
                // legacy jobs continue to use the tier-less "icon-{genre}" key (BC).
                const kind = variant.tier
                  ? `icon-plan:${variant.tier}:${_recraftModel}:${_recraftStyle || "default"}`
                  : `icon-${genre}`;
                for (const el of plan.elements || []) {
                  // Connectors/arrows are flow FILLER that resolvePlan DROPS at render → never pay
                  // Recraft for them ("hug"/"Seperti pelukan" was generated then dropped). (Rino)
                  if (/^connector/i.test(el.slot || "") || el.type === "arrow" || el.type === "connector") continue;
                  const q = el.asset_query || el.id;
                  const labelQ = el.label ? String(el.label).trim() : "";
                  try {
                    // LADDER: (1) cached-Recraft for THIS tier's cache key — cross-job reuse of a
                    // paid asset; (2) free lib (Lite only — Ultra skips per recraftOnly); (3) paid
                    // Recraft with the tier's model+style. Cached asset ALWAYS wins over free-lib
                    // (a paid V2 Vector "skewer" beats a generic Lucide "utensil").
                    const hit = await deps.store.getCachedAsset?.(kind, q);
                    if (hit && hit.strokes) {
                      el.viewBox = hit.viewBox; el.strokes = hit.strokes; if (hit.shapes) el.shapes = hit.shapes;
                      el.assetSource = "recraft-cache"; el.license = hit.license || _license; continue;
                    }
                    // Ultra bypasses the free-lib check — every element MUST go to Recraft V2 Vector
                    // (the whole tier's premise: user paid for a per-subject clean vector, not a
                    // generic Lucide icon). Lite honors the free-lib fallback (its cost story).
                    if (!_isUltra) {
                      if (coveredByLibrary(q) || (labelQ && coveredByLibrary(labelQ))) continue; // free lib fallback
                    }
                    // Lineart (free tier, priced "icons0") NEVER pays for Recraft → fall through
                    // to the free-lib / bohlam fallback. Recraft icon-fill is a PAID-mode
                    // differentiator (Color=starter+, Realistis=plus+, both mode-gated at submit).
                    // Ultra + Lite are already paid tiers (plan-mode) → skip this legacy check.
                    if (!variant.tier && genre === "lineart") continue;
                    // GATE before the paid Recraft gen → at balance 0 skip it (free fallback) instead
                    // of debiting into the negative (same guard as flux/TTS). Gate against the ACTUAL
                    // meter (v2-vector = $0.044 for Ultra/Lite, v3-vector = $0.08 for legacy).
                    if (!(await deps.generationClient?.gateUsage?.({ jobId, tenantId: meta.tenantId, userId: meta.userId }, "image", _meterModel, { count: 1 }))) {
                      console.warn(`[whiteboard-plan ${jobId}/${sceneIndex}] recraft icon "${q}" skipped: insufficient credits → free fallback`); continue;
                    }
                    const { svg, meter } = await generateRecraftIcon(q, {
                      genre, seed: 1000 + sceneIndex * 13,
                      model: _recraftModel, style: _recraftStyle,
                      meterModel: _meterModel,
                    });
                    const parsed = parseSvg(svg, { dropBg: true, dropLight: true });
                    if (parsed.strokes && parsed.strokes.length) {
                      el.viewBox = parsed.viewBox; el.strokes = parsed.strokes; el.assetSource = "recraft"; el.license = _license;
                      // colored fills (so Recraft icons aren't thin outlines — drawn under the strokes)
                      const { shapes } = parseSvgShapes(svg, { dropBg: true });
                      if (shapes && shapes.length) el.shapes = shapes;
                      await deps.store.setCachedAsset?.(kind, q, { viewBox: parsed.viewBox, strokes: parsed.strokes, shapes,
                        source: "recraft", model: _meterModel, license: _license, createdAt: new Date().toISOString() });
                      if (meter) meters.push(meter);
                    }
                  } catch (ge) {
                    if (!isRecraftCreditSkip(ge.message)) console.warn(`[whiteboard-plan ${jobId}/${sceneIndex}] recraft icon "${q}" failed: ${ge.message}`);
                  }
                }
              }
              for (let mi = 0; mi < meters.length; mi++) {
                const m = meters[mi];
                // Stable op_id per (job, scene, meter-slot) → /video/meter dedups (charge() ON CONFLICT),
                // so a BullMQ re-delivery of this scene's plan-baking can't double-charge a Recraft icon.
                await deps.generationClient?.meterUsage?.(
                  { jobId, tenantId: meta.tenantId, userId: meta.userId }, m.operation, m.model, m.units,
                  `vi-wbm:${jobId}:${sceneIndex}:${mi}:${m.operation}:${m.model}`);
              }
            } catch (re) {
              console.warn(`[whiteboard-plan ${jobId}/${sceneIndex}] asset baking skipped: ${re.message}`);
            }
            // Persist the tier envelope alongside the plan so a stitch re-run picks the SAME
            // renderer/model/template/style (idempotent recovery — no drift after restart).
            // Legacy path leaves these null → historic Redis rows unchanged.
            await deps.store.setSceneFields(jobId, sceneIndex, {
              visualStatus: "done", visualKind: "whiteboard-plan", planJson: JSON.stringify(plan),
              ...(variant.tier ? {
                whiteboardTier: variant.tier,
                whiteboardTemplate: variant.template || "",
                whiteboardStyle: variant.apiStyle || "",
                whiteboardApiStyle: variant.apiStyle || "",
                iconStyle: variant.iconStyle || "",
              } : {}),
            });
            return { sceneIndex, genre, engine: "plan", tier: variant.tier || null };
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
        // Single-call tiers (premium_color / premium_detail / regular_color / regular_detail):
        // pass the tier's Recraft model + picked apiStyle + meter model down into the asset
        // generator. Pass the FULL prompt (visuals.mjs.sceneKeySlug hashes it) — the previous
        // slice(0,200) collided across scenes when the art-direction preamble was >200 chars
        // (2026-07-05 bug: 5 Regular Color scenes shared the same "Setting: Prehistoric bamboo
        // forests…" preamble, so all 5 scenes hit the same cache entry and rendered scene 4's
        // SVG). Legacy jobs (variant.engine === "legacy") pass no tier hints → the asset
        // generator falls back to its historic genre-driven defaults.
        let _prompt = scene.visualPrompt || scene.text || "";
        // WB_REFRAME_ENABLED (default OFF → this whole block is skipped, _prompt stays the
        // raw scene.visualPrompt so behaviour is byte-identical to pre-reframe). When ON
        // AND the tier's engine is single-call (DrawReveal/SceneView), rewrite the raw
        // visualPrompt into a scene-grounded prompt via generationClient.generateWBVisualReframe.
        // Cache by {brief, narration, tier, apiStyle} so the same script re-renders reuse
        // the (paid) reframe. Any error / null result → keep the raw scene.visualPrompt.
        if ((variant.engine === "DrawReveal" || variant.engine === "SceneView")
            && /^(1|true|yes)$/i.test(process.env.WB_REFRAME_ENABLED || "")) {
          try {
            const _reframeParts = {
              brief: meta.brief,
              narration: scene.text,
              tier: variant.tier,
              apiStyle: variant.apiStyle,
            };
            let reframedPrompt = await deps.store.getCachedReframe?.(_reframeParts);
            const _reframeFromCache = !!reframedPrompt;
            if (!reframedPrompt) {
              reframedPrompt = await deps.generationClient?.generateWBVisualReframe?.(
                { jobId, tenantId: meta.tenantId, userId: meta.userId },
                { narration: scene.text, brief: meta.brief, tier: variant.tier,
                  apiStyle: variant.apiStyle, character: meta.visualCast || "",
                  model: process.env.WB_REFRAME_MODEL || meta.genModel,
                  language: meta.language });
            }
            if (reframedPrompt) {
              if (!_reframeFromCache) await deps.store.setCachedReframe?.(_reframeParts, reframedPrompt);
              _prompt = reframedPrompt;
            }
          } catch (re) {
            console.warn(`[whiteboard ${jobId}/${sceneIndex}] reframe failed → raw prompt: ${re.message}`);
          }
        }
        const _tierAssetOpts = (variant.tier && variant.engine !== "WhiteboardPlan")
          ? { tier: variant.tier, apiStyle: variant.apiStyle, model: variant.model,
              meterModel: variant.meterModel /* sceneKey OMITTED → visuals.mjs hashes _prompt */ }
          : {};
        const a = await generateWhiteboardAsset(genre, {
          prompt: _prompt, tmpDir, sceneIndex,
          aspect: meta.aspectRatio || "16:9",
          // diagram genre: graph from Python (same LLM routing/failover + Model Narasi)
          diagramGraph: genre === "diagram"
            ? (desc) => deps.generationClient?.generateDiagramGraph?.(
                { jobId, tenantId: meta.tenantId, userId: meta.userId },
                { description: desc, model: meta.genModel, language: meta.language })
            : undefined,
          ..._tierAssetOpts,
        });
        const _wbMeters = a.meters || [];
        for (let mi = 0; mi < _wbMeters.length; mi++) {
          const m = _wbMeters[mi];
          // Stable op_id per (job, scene, meter-slot) → idempotent on a whiteboard-asset worker re-run.
          await deps.generationClient?.meterUsage?.(
            { jobId, tenantId: meta.tenantId, userId: meta.userId }, m.operation, m.model, m.units,
            `vi-wba:${jobId}:${sceneIndex}:${mi}:${m.operation}:${m.model}`);
        }
        const up = a.visualPath ? await maybeUpload(jobId, meta.tenantId, "images", a.visualPath) : { path: undefined, key: null };
        const mk = a.maskPath ? await maybeUpload(jobId, meta.tenantId, "images", a.maskPath) : { path: undefined, key: null };
        await deps.store.setSceneFields(jobId, sceneIndex, {
          visualStatus: "done", visualKind: a.kind || "whiteboard",
          ...(up.path ? { visualPath: up.path } : {}), ...(up.key ? { visualKey: up.key } : {}),
          ...(mk.path ? { maskPath: mk.path } : {}), ...(mk.key ? { maskKey: mk.key } : {}),
          // Tier envelope for stitch-time renderer selection + recovery idempotency (legacy = no-op).
          ...(variant.tier ? {
            whiteboardTier: variant.tier,
            whiteboardTemplate: variant.template || "",
            whiteboardStyle: variant.apiStyle || "",
            whiteboardApiStyle: variant.apiStyle || "",
            iconStyle: variant.iconStyle || "",
          } : {}),
        });
        return { sceneIndex, genre, tier: variant.tier || null };
      } catch (e) {
        console.warn(`[whiteboard ${jobId}/${sceneIndex}] ${genre} asset failed: ${e.message} → handwriting`);
        await deps.store.setSceneFields(jobId, sceneIndex, { visualStatus: "fallback", visualKind: "whiteboard", visualError: e.message });
        return { sceneIndex, fellBack: true };
      }
    }
    const tmpDir = jobTmpDir(jobId);
    await mkdir(tmpDir, { recursive: true });
    // VI_VISUAL_WORKER_ENABLED (default off): mirror of the WB plan worker for NON-WB visualModes
    // (full_images / hybrid / full_clips). The pre-built scene.visualPrompt is the regex-based
    // segmenter output that lets the SHARED brief lock characters across every scene (the
    // "Chastelein in every scene" bug). When this flag is on, a per-scene LLM (Sonnet by default,
    // env VI_VISUAL_WORKER_MODEL) writes a tightly-scoped prompt grounded on THIS scene's
    // narration only + the brief as WORLD context (era/setting/palette — NOT a character roster).
    // Metered as a chat debit tagged with this video job → refundable. Any failure keeps the
    // existing visualPrompt (graceful fallback; no scene is ever lost to a worker LLM error).
    if (VI_VISUAL_WORKER_ON) {   // #10: snapshot read, not per-scene process.env
      try {
        const fresh = await deps.generationClient?.generateVisualPrompt?.(
          { jobId, tenantId: meta.tenantId, userId: meta.userId },
          { narration: scene.text || "",
            // brief intentionally OMITTED — mirror WB (whiteboard-plan also has no brief field).
            // The shared brief was poisoned by upstream _video_visual_brief naming a recurring
            // character and dominating every scene. Cinematography consistency now rides on `style`.
            language: meta.language || "",
            visualStyle: meta.visualStyle || "",
            style: meta.style || "",   // gaya narasi → per-style cinematography tone (STYLE_TONE)
            culturalPalette: meta.culturalPalette || "",   // Nusantara cues (clean, no character names)
            visualCast: meta.visualCast || "",   // Visual SharedContext registry (name-filtered per scene)
            sceneKind: scene.kind || "image",
            sceneIndex, sceneTotal: Number(meta.sceneCount) || 1 });
        if (fresh && typeof fresh.visual_prompt === "string" && fresh.visual_prompt.length >= 80) {
          scene.visualPrompt = fresh.visual_prompt;
          // Persist the LLM prompt to Redis so the status poll + reaper see the new prompt, not the
          // pre-built regex one (otherwise audits look like the worker did nothing — Rino's morning bug).
          await deps.store.setSceneFields(jobId, sceneIndex, { visualPrompt: fresh.visual_prompt })
            .catch((e) => console.warn(`[visual-prompt ${jobId}/${sceneIndex}] Redis persist failed (#9, non-fatal; this render uses the in-memory LLM prompt, but Redis still holds the stale regex one): ${e.message}`));
          console.log(`[visual-prompt ${jobId}/${sceneIndex}] LLM prompt OK (${fresh.visual_prompt.length} chars) :: ${fresh.visual_prompt.slice(0, 140)}`);
        } else {
          console.warn(`[visual-prompt ${jobId}/${sceneIndex}] no usable LLM prompt (visual worker disabled on python [409] or invalid output) → keep regex prompt (#7)`);
        }
      } catch (e) {
        console.warn(`[visual-prompt ${jobId}/${sceneIndex}] LLM failed (${e.message}) → keep regex prompt`);
      }
    }
    const refImage = await resolveAnchor(jobId, tmpDir, meta);  // per-video reference (or null)
    // #2: gate the character anchor to scenes that actually NAME a recurring cast member. The anchor
    // is built from the brief (which depicts the main character), so applying it to a character-absent
    // scene (e.g. a modern-era scene) re-introduces the Chastelein bleed through the PIXEL channel —
    // bypassing the text-side name-filter. null = no cast registry to judge by → keep prior behavior.
    const _namesCast = _sceneNamesCast(scene.text || "", meta.visualCast || "");
    // A pronoun-only scene still depicts the recurring protagonist — Indonesian biography names them
    // once then uses dia/ia/beliau/mereka (#2 pronoun fix). Keep the anchor on those; only drop it on
    // a true topic shift (no cast name AND no referring pronoun). null = no registry → prior behavior.
    const _hasPronoun = /(?<![\p{L}\p{N}'])(?:dia|ia|beliau|mereka|he|she|they|him|her)(?![\p{L}\p{N}'])/iu.test(scene.text || "");
    const _useAnchor = (_namesCast === null) ? true : (_namesCast || _hasPronoun);
    const base = {
      jobId, sceneIndex, kind: scene.kind, visualPrompt: scene.visualPrompt,
      estSeconds: Number(scene.estSeconds), clipModel: meta.clipModel,
      imageModel: meta.imageModel || undefined, aspectRatio: meta.aspectRatio || "16:9",
      seed: hashSeed(jobId),   // same seed for all scenes of this video → steadier look
      refImage: (_useAnchor ? refImage : null) || undefined,   // anchor → ref_image, gated per-scene (#2)
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
  // Honest queue UX (Change 3): orchestrator flips status to "stitching" the moment all assets are
  // ready, but the stitch queue is only 2-wide — so a job can sit "stitching" while merely WAITING
  // behind other renders. Stamp renderStartedAt when the processor ACTUALLY starts → the UI shows an
  // honest "Antre" view until now, then the render bar (no more frozen-looking 4% bar for waiting jobs).
  await deps.store.patchMeta(jobId, { renderStartedAt: Date.now() }).catch(() => {});
  // Per-phase wallclock instrumentation (2026-07-04 Rino: WB < 1min still slow after env flips;
  // need to isolate whether time goes to asset-resolve / render / upload / register / concat).
  // Prefix "[stitch <jobId>]" so grep in Railway logs is trivial. Never throws.
  const _t0 = Date.now();
  const _phases = {};
  const _mark = (name, from = _t0) => { _phases[name] = Date.now() - from; };
  const _phaseTimer = () => { const s = Date.now(); return () => Date.now() - s; };
  const tmpDir = jobTmpDir(jobId);
  try {
    await mkdir(tmpDir, { recursive: true });
    const _tAssetsStart = Date.now();
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
      const audioPath = await resolveLocal(jobId, tmpDir, s.audioKey, s.audioPath, `aud_${i}.wav`);
      // The scene window MUST cover the ACTUAL narration so the stitch never truncates it. The svg
      // WB renderer re-measures + extends its frames, but Remotion (legacy + plan) trusts the JSON
      // duration and cuts audio when it runs out — so when durationActual under-estimated actual TTS
      // length, WB scenes ended with a chopped VO tail before the next scene started. Rino 2026-07-05:
      // "vo cut di pergantian scene". Fix: run ffprobe for WB too — the extension is only additive
      // (base = max(base, aLen)), so short-audio scenes keep byte-identical windows and long-audio
      // scenes now grow to cover the full VO.
      let base = Number(s.durationActual) || Number(s.estSeconds) || 2;
      if (audioPath) {
        const aLen = await ffprobeDuration(audioPath);
        if (Number.isFinite(aLen) && aLen > base) base = aLen;
      }
      scenes.push({
        kind: s.visualKind || "image",
        duration: base + sceneGap,
        text: s?.text || "",   // per-scene caption (long-video render path)
        visualPath: wbNoAsset ? undefined : await resolveLocal(jobId, tmpDir, s.visualKey, s.visualPath,
          `vis_${i}.${s.visualKind === "clip" ? "mp4" : "png"}`),
        // detail genre also carries a vectorized reveal mask (whiteboard only)
        ...(meta.visualMode === "whiteboard" && (s.maskKey || s.maskPath)
          ? { maskPath: await resolveLocal(jobId, tmpDir, s.maskKey, s.maskPath, `mask_${i}.svg`) }
          : {}),
        // whiteboard plan-engine: per-scene visual plan (built in the visual phase)
        ...(s.planJson ? { planJson: s.planJson } : {}),
        audioPath,
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
    _phases.assets_resolve_ms = Date.now() - _tAssetsStart;
    const _tRenderStart = Date.now();
    let result;
    if (meta.visualMode === "whiteboard") {
      // Tier-based renderer selection: if the job's tier maps to renderer === "svg_ffmpeg"
      // (Ultra/Lite) → force renderWhiteboardPlanSvg regardless of WB_ENGINE/WB_RENDER_BACKEND
      // env. Legacy jobs (no tier) fall through to the historic env-gated branches.
      //
      // Per-job tier consistency: every scene in a job is generated with the SAME tier by
      // construction (tier is a JOB-level pick, stored on meta and echoed onto each scene
      // as it renders). We ASSERT that here and log a warning on drift — a mixed job would
      // route to the tier we detected on scene 0 by construction.
      const _stitchVariant = resolveWBVariant(meta);
      const _perSceneTiers = new Set();
      for (const _s of scenesRaw) if (_s && _s.whiteboardTier) _perSceneTiers.add(_s.whiteboardTier);
      if (_stitchVariant.tier) _perSceneTiers.add(_stitchVariant.tier);
      if (_perSceneTiers.size > 1) {
        console.warn(`[stitch ${jobId}] per-job tier drift (${[..._perSceneTiers].join(",")}); routing on job tier ${_stitchVariant.tier}`);
      }
      const _forcePlanSvg = _stitchVariant.renderer === "svg_ffmpeg";
      // Opt B: render the WHOLE video with the Remotion whiteboard engine instead of
      // the ffmpeg stitch. render.mjs is imported LAZILY here (worker-only) so the API
      // process never loads @remotion/Chromium at startup.
      if (_forcePlanSvg) {
        // Ultra/Lite tier → svg_ffmpeg is the ONLY correct renderer (multi-subject plans built
        // for the svg backend). No env override; on error we do NOT silently fall back to
        // Remotion (a Remotion render of a multi-subject plan would look wrong).
        const { renderWhiteboardPlanSvg } = await import("./whiteboard/renderers/svgFfmpeg.mjs");
        result = await renderWhiteboardPlanSvg(scenes, { ...meta, jobId }, outPath, { tmpDir,
          onProgress: (d, t) => deps.store.patchMeta(jobId, { renderProgress: Math.round(d / t * 100), renderScenesDone: d, renderScenesTotal: t }).catch(() => {}) });
      } else if ((process.env.WB_ENGINE || "legacy") === "plan") {
        // Golpo-like plan engine: per-scene visual_plan → resolve → multi-scene render.
        // BACKEND is pluggable (Guide-2 §K/§L): default Remotion (proven); WB_RENDER_BACKEND=svg_ffmpeg
        // routes to the Chromium-free SVG/FFmpeg renderer. Falls back to Remotion on any svg-backend error.
        const backend = (process.env.WB_RENDER_BACKEND || "remotion").toLowerCase();
        if (backend === "svg_ffmpeg") {
          try {
            const { renderWhiteboardPlanSvg } = await import("./whiteboard/renderers/svgFfmpeg.mjs");
            result = await renderWhiteboardPlanSvg(scenes, { ...meta, jobId }, outPath, { tmpDir,
              onProgress: (d, t) => deps.store.patchMeta(jobId, { renderProgress: Math.round(d / t * 100), renderScenesDone: d, renderScenesTotal: t }).catch(() => {}) });
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
            result = await renderWhiteboardPlanSvg(scenes, { ...meta, jobId }, outPath, { tmpDir,
              onProgress: (d, t) => deps.store.patchMeta(jobId, { renderProgress: Math.round(d / t * 100), renderScenesDone: d, renderScenesTotal: t }).catch(() => {}) });
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
      // flat render fee — 3 credits/sec of output video (post-hoc, tagged for refund). STABLE op_id
      // keyed to the job → idempotent: a stitch re-run (BullMQ attempts:2 after a post-render crash,
      // or a recovery re-stitch) re-meters with the SAME op_id, which charge() dedups → the user is
      // charged the render fee exactly ONCE per job, never double. (was a uuid4 per call → double-charge)
      await deps.generationClient?.meterUsage?.(
        { jobId, tenantId: meta.tenantId, userId: meta.userId },
        "video", "whiteboard", { seconds: Math.max(1, Math.round(result.duration || 0)) },
        `video-renderfee:${jobId}`);
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
    _phases.render_ms = Date.now() - _tRenderStart;
    const _tUploadStart = Date.now();
    // upload the final MP4 under the lifecycle-managed `videos/` prefix (Step 6f)
    let up = { path: outPath, key: null, size: 0 };
    const s = await storage();
    if (s) {
      const { readFile } = await import("node:fs/promises");
      const key = s.videoKey(meta.tenantId, jobId);
      const bytes = await readFile(outPath);
      await s.uploadBytes(key, bytes, "video/mp4");
      up = { path: outPath, key, size: bytes.length };
      _phases.upload_ms = Date.now() - _tUploadStart;
      const _tRegStart = Date.now();
      // Register the video as a queryable `assets` row so it appears in Media Vault.
      // Before this hook the video mode (VI/WB/standard clip) uploaded to R2 but never
      // got a Postgres row → invisible in Vault (only visible during the 24h Redis TTL
      // of `vjob:{id}`). Best-effort — a registry failure MUST NOT fail the job (the
      // video is safely in R2 already). Idempotent on (bucket, s3_key) via
      // db.insert_asset's ON CONFLICT. Uses source_job_type=veo for BOTH WB and
      // standard-clip; metadata.visualMode distinguishes in Vault.
      try {
        await deps.generationClient?.registerVideoAsset?.({
          tenantId: meta.tenantId, userId: meta.userId, jobId,
          s3Key: key, contentType: "video/mp4", sizeBytes: up.size,
          sourceJobType: "veo",
          metadata: {
            visualMode: meta.visualMode || "video",
            whiteboardGenre: meta.whiteboardGenre || null,
            durationSec: Math.round(result?.duration || 0),
            aspectRatio: meta.aspectRatio || "16:9",
          },
          projectId: meta.projectId || null,
        });
      } catch (regErr) {
        console.warn(`[asset-register ${jobId}] non-fatal: ${regErr.message}`);
      }
      _phases.register_ms = Date.now() - _tRegStart;
    }
    _phases.total_ms = Date.now() - _t0;
    _phases.visualMode = meta.visualMode || "video";
    _phases.sceneCount = meta.sceneCount || (scenesRaw && scenesRaw.length) || 0;
    _phases.videoDurationSec = Math.round(result?.duration || 0);
    // Single-line JSON for grep: [stitch-timing <jobId>] {...}. Never throws.
    console.log(`[stitch-timing ${jobId}] ${JSON.stringify(_phases)}`);
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
      // Idle cost: a Worker re-arms a blocking BZPOPMIN every drainDelay (BullMQ default 5s)
      // even with ZERO jobs → continuous billable commands on a per-command Redis (Upstash).
      // queue.add() writes a marker that wakes a blocked worker instantly, so a larger
      // drainDelay only stretches the IDLE re-arm — it never adds latency to real job pickup.
      // 60s cuts the dominant idle poll ~12× (4 workers × 1/5s → 1/60s).
      drainDelay: Number(process.env.VIDEO_DRAIN_DELAY) || 60,
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

  // STITCH had no failed-handler → a stitch killed by a deploy/restart or lock-stall left the
  // job stuck at "stitching" + charged, forever. Recovery converges EVERY job to done or
  // failed+refund: re-dispatch on stitch failure (bounded), + a boot scan & reaper for jobs
  // orphaned by a restart. (See recovery.mjs.) Idempotent refund → never double-pays.
  const recovery = startRecovery({ store: deps.store, queues: deps.queues, credits: deps.credits });
  stitch.on("failed", (job, err) => recovery.onStitchFailed(job?.data?.jobId, err?.message || "stitch failed"));

  const workers = [audio, visual, check, stitch];
  return {
    workers,
    async close() { await recovery.stop(); await Promise.all(workers.map((w) => w.close())); },
  };
}

export async function cleanupJobTmp(jobId) {
  _anchorCache.delete(jobId);   // #8: evict the per-job anchor entry so the Map can't grow unbounded
  await rm(jobTmpDir(jobId), { recursive: true, force: true }).catch(() => {});
}
