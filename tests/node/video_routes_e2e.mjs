/**
 * Integration harness for backend/video/routes.mjs — drives the REAL route
 * handlers (enqueue-only) through the REAL engine (synthetic generation + real
 * ffmpeg), proving the API→engine→MP4 wiring (#7) and that segmenter-shaped
 * scenes (snake_case clip_eligible, no `kind`) map correctly (#3).
 *
 * Uses a tiny fake express `app` so no HTTP server / express dep is needed.
 * Requires a reachable Redis + ffmpeg. Run via tests/node/run-video-e2e.sh.
 */
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mountVideoRoutes } from "../../backend/video/routes.mjs";
import { startWorkers, makeDeps, cleanupJobTmp } from "../../backend/video/workers.mjs";
import { syntheticGenerationClient } from "../../backend/video/generationClient.mjs";
import * as store from "../../backend/video/store.mjs";

function mockRes() {
  return { code: 200, body: null, status(c) { this.code = c; return this; }, json(d) { this.body = d; return this; } };
}
function fakeApp() {
  const h = {};
  return {
    handlers: h,
    post(p, _auth, fn) { h[`POST ${p}`] = fn; },
    get(p, _auth, fn) { h[`GET ${p}`] = fn; },
  };
}

const engine = startWorkers(makeDeps({ generationClient: syntheticGenerationClient({ width: 640, height: 360, fps: 30 }) }));
const app = fakeApp();
mountVideoRoutes(app); // no auth → passthrough; anon tenant via body

try {
  // segmenter-shaped scenes: snake_case clip_eligible, NO explicit `kind`
  const scenes = [
    { number: 1, text: "Opening scene over the sea.", visual_prompt: "wide shot, sea", clip_eligible: true, est_seconds: 1.5 },
    { number: 2, text: "A long reflective passage that runs past the clip ceiling.", visual_prompt: "still, shore", clip_eligible: false, est_seconds: 1.5 },
    { number: 3, text: "A short punchy beat.", visual_prompt: "motion, waves", clip_eligible: true, est_seconds: 1.5 },
    { number: 4, text: "Closing image, slow push out.", visual_prompt: "wide, horizon", clip_eligible: false, est_seconds: 1.5 },
  ];

  const postRes = mockRes();
  await app.handlers["POST /api/video/assemble"](
    { body: { scenes, tenantId: "t_routes", userId: "u_routes", tier: "hd" }, headers: {}, params: {} },
    postRes
  );
  assert.equal(postRes.code, 200, `assemble failed: ${JSON.stringify(postRes.body)}`);
  const { jobId, batchPlan, sceneCount } = postRes.body;
  assert.ok(jobId, "no jobId returned");
  assert.equal(sceneCount, 4);
  console.log(`[routes-e2e] enqueued ${jobId} (${sceneCount} scenes, plan ${JSON.stringify(batchPlan)})`);

  // verify #3: snake_case clip_eligible mapped to kind on the stored scenes
  const stored = await store.getScenes(jobId, 4);
  assert.equal(stored[0].kind, "clip", "scene 1 (clip_eligible:true) should be a clip");
  assert.equal(stored[1].kind, "image", "scene 2 (clip_eligible:false) should be an image");

  // poll the GET status route to completion
  const deadline = Date.now() + 120000;
  let body;
  for (;;) {
    const r = mockRes();
    await app.handlers["GET /api/video/assemble/:jobId"]({ params: { jobId }, headers: {} }, r);
    body = r.body;
    if (body && (body.status === "done" || body.status === "failed")) break;
    if (Date.now() > deadline) throw new Error(`timeout; status=${body?.status}`);
    await new Promise((s) => setTimeout(s, 500));
  }

  assert.equal(body.status, "done", `job failed: ${body.error || "?"}`);
  assert.ok(body.mp4Path && existsSync(body.mp4Path), "MP4 not produced via the route path");
  console.log(`[routes-e2e] PASS — POST /api/video/assemble → engine → ${body.mp4Path} (${body.durationActual}s)`);

  await cleanupJobTmp(jobId).catch(() => {});
  await store.destroy(jobId, 4).catch(() => {});
} finally {
  await engine.close();
  const { sharedConnection } = await import("../../backend/video/connection.mjs");
  sharedConnection().disconnect();
  process.exit(0);
}
