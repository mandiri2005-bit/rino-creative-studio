/**
 * End-to-end integration harness for the video-assembly engine.
 *
 * Proves the WHOLE engine with REAL BullMQ + REAL ffmpeg, no API keys:
 *   startAssembly → parallel audio+visual workers → batch fan-in/advance →
 *   stitch worker → one MP4 of the expected (master-clock) length.
 *
 * Requires a reachable Redis (REDIS_URL) and ffmpeg. Run via the wrapper:
 *   tests/node/run-video-e2e.sh        (starts an ephemeral redis on :6399)
 *
 * Usage: VIDEO_WORKER=1 REDIS_URL=... node tests/node/video_e2e.mjs [sceneCount]
 */
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { makeQueues, startWorkers, makeDeps, cleanupJobTmp } from "../../backend/video/workers.mjs";
import { syntheticGenerationClient } from "../../backend/video/generationClient.mjs";
import { startAssembly } from "../../backend/video/orchestrator.mjs";
import { batchPlan } from "../../backend/video/params.mjs";
import * as store from "../../backend/video/store.mjs";
import { ffprobeDuration } from "../../backend/video/ffmpeg.mjs";

const sceneCount = Number(process.argv[2] || 5);
const PER = 1.5;              // seconds of narration per scene
const XF = Number(process.env.VIDEO_XFADE || 0.5);

function buildScenes(n) {
  return Array.from({ length: n }, (_, i) => ({
    number: i + 1,
    text: `Scene ${i + 1} narration about the sea and the islands.`,
    visual_prompt: `scene ${i + 1}, establishing shot, documentary realism`,
    kind: i % 3 === 0 ? "clip" : "image",   // mix of clips and stills
    est_seconds: PER,
  }));
}

const jobId = `e2e-${sceneCount}-${process.pid}`;
const deps = makeDeps({ generationClient: syntheticGenerationClient({ width: 640, height: 360, fps: 30 }), queues: makeQueues() });
const engine = startWorkers(deps);

try {
  const scenes = buildScenes(sceneCount);
  const res = await startAssembly({ jobId, tenantId: "t_e2e", userId: "u_e2e", scenes, tier: "hd", visualMode: "hybrid" }, deps);
  console.log(`[e2e] started: ${res.sceneCount} scenes, dispatch=${res.dispatch}, plan=${JSON.stringify(res.batchPlan)}`);

  // poll to completion
  const deadline = Date.now() + 120000;
  let meta;
  for (;;) {
    meta = await store.getMeta(jobId);
    if (meta && (meta.status === "done" || meta.status === "failed")) break;
    if (Date.now() > deadline) throw new Error(`timeout; last status=${meta?.status} progress=${meta?.progress}`);
    await new Promise((s) => setTimeout(s, 500));
  }

  assert.equal(meta.status, "done", `job failed: ${meta.error || "?"}`);
  assert.deepEqual(meta.batchPlan, batchPlan(sceneCount));
  const mp4 = meta.mp4Path;
  assert.ok(mp4 && existsSync(mp4), "MP4 was not produced");

  const dur = await ffprobeDuration(mp4);
  const expected = sceneCount * PER - (sceneCount - 1) * XF;   // master clock − crossfades
  console.log(`[e2e] DONE: status=${meta.status} mp4=${mp4} duration=${dur}s (expected ≈ ${expected}s)`);
  assert.ok(Math.abs(dur - expected) < 0.6, `duration ${dur} far from expected ${expected}`);

  console.log(`[e2e] PASS — ${sceneCount} scenes assembled end-to-end (BullMQ + ffmpeg).`);
} finally {
  await engine.close();
  await cleanupJobTmp(jobId).catch(() => {});
  await store.destroy(jobId, sceneCount).catch(() => {});
  // close shared producer connections so the process can exit
  const { sharedConnection } = await import("../../backend/video/connection.mjs");
  sharedConnection().disconnect();
  for (const q of Object.values(deps.queues)) await q.close().catch(() => {});
  process.exit(0);
}
