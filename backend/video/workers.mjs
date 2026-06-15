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
import { tmpdir } from "node:os";
import { join } from "node:path";
import { QUEUE, CONCURRENCY, makeConnection } from "./connection.mjs";
import * as store from "./store.mjs";
import { ffprobeDuration, stitch, buildSrt, hasSubtitlesFilter } from "./ffmpeg.mjs";
import { advance } from "./orchestrator.mjs";
import { rm } from "node:fs/promises";

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
  const ctype = name.endsWith(".mp4") ? "video/mp4" : name.endsWith(".png") ? "image/png" : "audio/wav";
  await s.uploadBytes(key, await readFile(localPath), ctype);
  return { path: localPath, key };
}

async function resolveLocal(jobId, tmpDir, key, path, fallbackName) {
  if (path) return path; // local already
  const s = await storage();
  if (s && key) {
    const { writeFile } = await import("node:fs/promises");
    const out = join(tmpDir, fallbackName);
    await writeFile(out, await s.downloadBytes(key));
    return out;
  }
  throw new Error(`no asset for ${fallbackName}`);
}

// ── processors (exported for unit/integration testing) ──
export async function audioProcessor(job, deps) {
  const { jobId, sceneIndex } = job.data;
  try {
    const meta = await deps.store.getMeta(jobId);
    const scene = await deps.store.getScene(jobId, sceneIndex);
    if (!meta || !scene) return { skipped: "missing" };
    if (JOB_TERMINAL.has(meta.status)) return { skipped: "job-terminal" };
    if (scene.audioStatus === "done") return { skipped: "already-done" }; // idempotent: no re-charge
    const tmpDir = jobTmpDir(jobId);
    await mkdir(tmpDir, { recursive: true });
    const a = await deps.generationClient.synthesizeAudio(
      { jobId, sceneIndex, text: scene.text, estSeconds: Number(scene.estSeconds),
        voice: meta.voice || undefined, ttsModel: meta.ttsModel || undefined,
        tenantId: meta.tenantId, userId: meta.userId }, tmpDir);
    const duration = (await ffprobeDuration(a.path)) || a.durationSeconds || Number(scene.estSeconds) || 0;
    const up = await maybeUpload(jobId, meta.tenantId, "audio", a.path);
    await deps.store.setSceneFields(jobId, sceneIndex, {
      audioStatus: "done", audioPath: up.path, audioKey: up.key, durationActual: duration.toFixed(3),
    });
    return { sceneIndex };
  } catch (e) {
    await deps.store.setSceneFields(jobId, sceneIndex, { audioStatus: "failed", audioError: e.message }).catch(() => {});
    return { sceneIndex, failed: e.message };
  } finally {
    // always advance, even on the skip/return paths; never let a queue blip re-run a charged job
    await deps.queues.check.add("check", { jobId }).catch(() => {});
  }
}

// Diverse-provider image fallbacks: if the chosen model's provider is flaky or
// content-blocks the prompt, a DIFFERENT provider usually succeeds — so a failed
// scene still gets a REAL image instead of a placeholder. Ordered across providers
// (Gemini → Flux → Seedream).
const IMAGE_FALLBACK_MODELS = ["nano-banana", "flux-kontext-pro", "seedream-4-0"];

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
    const tmpDir = jobTmpDir(jobId);
    await mkdir(tmpDir, { recursive: true });
    const base = {
      jobId, sceneIndex, kind: scene.kind, visualPrompt: scene.visualPrompt,
      estSeconds: Number(scene.estSeconds), clipModel: meta.clipModel,
      imageModel: meta.imageModel || undefined, aspectRatio: meta.aspectRatio || "16:9",
      tenantId: meta.tenantId, userId: meta.userId,
    };
    let v;
    try {
      v = await deps.generationClient.generateVisual(base, tmpDir);
    } catch (primaryErr) {
      let cause = primaryErr;
      // Tier 1 — a failed CLIP retries as a real image on its default model.
      if (scene.kind === "clip") {
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
    const scenes = [];
    for (let i = 0; i < scenesRaw.length; i++) {
      const s = scenesRaw[i];
      scenes.push({
        kind: s.visualKind || "image",
        duration: Number(s.durationActual) || Number(s.estSeconds) || 2,
        visualPath: await resolveLocal(jobId, tmpDir, s.visualKey, s.visualPath,
          `vis_${i}.${s.visualKind === "clip" ? "mp4" : "png"}`),
        audioPath: await resolveLocal(jobId, tmpDir, s.audioKey, s.audioPath, `aud_${i}.wav`),
      });
    }
    const outPath = join(tmpDir, "out.mp4");
    const stitchOpts = { cwd: tmpDir };
    if (meta.aspectRatio === "9:16") { stitchOpts.width = 1080; stitchOpts.height = 1920; } // portrait
    if (meta.captions && (await hasSubtitlesFilter())) {
      // Step 6e: burn captions from the KNOWN script + measured timing (no ASR).
      const { writeFile } = await import("node:fs/promises");
      const xfade = Number(process.env.VIDEO_XFADE || 0.5);
      const srt = buildSrt(scenesRaw.map((s) => s?.text || ""), scenes.map((s) => s.duration), xfade);
      await writeFile(join(tmpDir, "captions.srt"), srt, "utf8");
      stitchOpts.srt = "captions.srt";
    } else if (meta.captions) {
      console.warn(`[stitch ${jobId}] captions requested but ffmpeg has no 'subtitles' filter (no libass) — rendering without burn-in`);
    }
    const result = await stitch(scenes, outPath, stitchOpts);
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
