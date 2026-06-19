// ─────────────────────────────────────────────────────────────────────────────
// video/orchestrator.mjs — the parallel-assembly brain (Step 6b).
//
// "This is the product." Given a segmented scene list, it dispatches every scene
// as simultaneous BullMQ jobs, collapsing total time to the slowest single scene
// instead of the sum — the entire reason this can match Atlabs on speed. Above
// ten scenes it dispatches in rate-safe batches of ten (Veo/Kling concurrency),
// waiting for each batch before the next, then triggers the stitch.
//
// The decision logic (planAdvance / batchRanges) is PURE and unit-tested. The
// side-effecting parts take an injected `deps` ({ queues, store, credits }) so
// they're testable with mocks and runnable against real BullMQ.
//
// CREDITS: per-scene generation already meters+charges inside the Python
// endpoints (Step 4). So the orchestrator only PRE-CHECKS the balance up front
// (fail fast with 402) — it does NOT keep a second ledger or double-charge.
// ─────────────────────────────────────────────────────────────────────────────
import { QUEUE, DEFAULT_JOB_OPTS, METERED_JOB_OPTS } from "./connection.mjs";
import { creditsForScenes, batchPlan as planBatches, normalizeTier } from "./params.mjs";
import * as store from "./store.mjs";

// ── pure: batch index → [start, end) scene range ──
export function batchRanges(batchPlan) {
  const ranges = [];
  let start = 0;
  for (const size of batchPlan) {
    ranges.push([start, start + size]);
    start += size;
  }
  return ranges;
}

/**
 * Pure decision function. Given the batch plan, the current per-scene states,
 * and which batches have already been dispatched, decide the next action:
 *   { action: 'dispatch', batchIndex } — current batch done, send the next
 *   { action: 'stitch' }               — every scene complete, fuse the MP4
 *   { action: 'fail' }                 — a scene hard-failed (no audio/visual)
 *   { action: 'wait' }                 — the in-flight batch isn't done yet
 */
export function planAdvance({ batchPlan, scenes, dispatched }) {
  const ranges = batchRanges(batchPlan);
  const complete = scenes.map(store.sceneComplete);
  const failed = scenes.map(store.sceneFailed);

  // a null/missing scene (expired hash, never written) is treated as a hard
  // failure so the job converges to 'fail' instead of waiting on it forever.
  const failedOrMissing = scenes.map((s, i) => s == null || failed[i]);
  if (failedOrMissing.some(Boolean)) return { action: "fail" };
  if (complete.every(Boolean)) return { action: "stitch" };

  const dispatchedSet = dispatched instanceof Set ? dispatched : new Set(dispatched || [0]);
  const D = Math.max(0, ...dispatchedSet);
  const [ds, de] = ranges[D] || [0, scenes.length];
  const currentBatchDone = complete.slice(ds, de).every(Boolean);

  if (currentBatchDone && D + 1 < ranges.length) {
    return { action: "dispatch", batchIndex: D + 1 };
  }
  return { action: "wait" };
}

// ── progress: fraction of scenes complete ──
export function progressOf(scenes) {
  if (!scenes.length) return 0;
  const done = scenes.filter(store.sceneComplete).length;
  return Math.round((done / scenes.length) * 100);
}

/**
 * Enqueue every scene in one batch as parallel audio + visual jobs.
 * `deps.queues` provides .audio / .visual with an async add(name, data, opts).
 */
export async function dispatchBatch(jobId, batchIndex, ctx, deps) {
  const { batchPlan } = ctx;
  const [start, end] = batchRanges(batchPlan)[batchIndex];
  const adds = [];
  for (let i = start; i < end; i++) {
    const data = { jobId, sceneIndex: i };   // processors read only these
    adds.push(deps.queues.audio.add("scene-audio", data, METERED_JOB_OPTS));
    adds.push(deps.queues.visual.add("scene-visual", data, METERED_JOB_OPTS));
  }
  await Promise.all(adds);
  return { batchIndex, scenes: end - start };
}

/**
 * Kick off a whole video. Pre-checks credits, persists job + scene state, and
 * dispatches the first batch (which is the WHOLE video when scene_count ≤ 10).
 * Returns { jobId, sceneCount, dispatch, batchPlan }.
 */
export async function startAssembly(ctx, deps) {
  const {
    jobId, tenantId, userId, scenes, tier = "hd",
    clipModel = "veo3", visualMode = "hybrid", whiteboardGenre = "", captions = false,
    voice, imageModel, ttsModel, language, genModel, aspectRatio = "16:9", captionFont,
    anchorKey, anchorB64,
  } = ctx;
  if (!jobId) throw new Error("startAssembly: jobId required");
  if (!scenes?.length) throw new Error("startAssembly: scenes required");

  const tierN = normalizeTier(tier);
  const batchPlan = planBatches(scenes.length);
  const creditsNeeded = creditsForScenes(scenes.length, tierN);

  // pre-check balance only — per-scene metering happens inside Python.
  if (deps.credits?.precheck) {
    const ok = await deps.credits.precheck(tenantId, creditsNeeded);
    if (!ok) {
      const e = new Error("insufficient_credits");
      e.status = 402;
      e.creditsNeeded = creditsNeeded;
      throw e;
    }
  }

  await deps.store.createJob({
    jobId, tenantId, userId, tier: tierN, clipModel, visualMode, whiteboardGenre, captions,
    voice: voice || "", imageModel: imageModel || "",
    ttsModel: ttsModel || "", language: language || "", genModel: genModel || "", aspectRatio,
    captionFont: captionFont || "",
    anchorKey: anchorKey || "", anchorB64: anchorB64 || "",
    sceneCount: scenes.length, batchSize: 10, batchPlan,
    status: "running", progress: 0, creditsEstimate: creditsNeeded,
    scenes,
  });

  await dispatchBatch(jobId, 0, { batchPlan, tier: tierN }, deps);
  await deps.store.tryClaimBatch(jobId, 0);

  return {
    jobId,
    sceneCount: scenes.length,
    dispatch: scenes.length <= 10 ? "full_parallel" : "batch",
    batchPlan,
    creditsEstimate: creditsNeeded,
  };
}

/**
 * Re-evaluate a job after any scene asset lands (called by the check worker) and
 * take the next action. Idempotent: batch dispatch + stitch are claimed atomically
 * so concurrent check jobs never double-fire.
 */
export async function advance(jobId, deps) {
  const meta = await deps.store.getMeta(jobId);
  if (!meta || ["done", "failed", "canceled"].includes(meta.status)) return { action: "noop" };

  const scenes = await deps.store.getScenes(jobId, meta.sceneCount);
  const dispatched = await deps.store.dispatchedBatches(jobId);
  const decision = planAdvance({ batchPlan: meta.batchPlan, scenes, dispatched });

  await deps.store.patchMeta(jobId, { progress: progressOf(scenes) });

  switch (decision.action) {
    case "dispatch": {
      if (await deps.store.tryClaimBatch(jobId, decision.batchIndex)) {
        await dispatchBatch(jobId, decision.batchIndex, { batchPlan: meta.batchPlan, tier: meta.tier }, deps);
      }
      return decision;
    }
    case "stitch": {
      if (await deps.store.tryClaimStitch(jobId)) {
        await deps.store.setStatus(jobId, "stitching");
        await deps.queues.stitch.add("stitch", { jobId }, DEFAULT_JOB_OPTS);
      }
      return decision;
    }
    case "fail": {
      await deps.store.setStatus(jobId, "failed", { error: "a scene failed to generate" });
      // refund what the partial assembly already consumed (idempotent per job)
      await deps.credits?.refundJob?.(meta.tenantId, jobId);
      return decision;
    }
    default:
      return decision; // wait
  }
}

export const QUEUE_NAMES = QUEUE;
