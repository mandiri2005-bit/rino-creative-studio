// ─────────────────────────────────────────────────────────────────────────────
// video/store.mjs — durable-enough live state for a video-assembly job in Redis.
//
// Mirrors the existing redis.js "live job" pattern but for the parallel engine,
// where many workers write CONCURRENTLY. To stay race-free without Lua:
//   • the job plan/status is one JSON key (vjob:{id}) — written only by the
//     orchestrator + the check worker (low contention);
//   • each scene's assets live in their OWN Redis HASH (vjob:{id}:scene:{i}) so
//     the audio worker and the visual worker write DISJOINT fields (HSET is
//     atomic per-field — no read-modify-write clobber);
//   • batch dispatch + stitch are guarded by atomic SADD / SET-NX so exactly one
//     worker ever advances a batch or kicks off the stitch.
// ─────────────────────────────────────────────────────────────────────────────
import { sharedConnection } from "./connection.mjs";

const TTL = 60 * 60 * 24; // 24h
const jobKey = (id) => `vjob:${id}`;
const sceneKey = (id, i) => `vjob:${id}:scene:${i}`;
const batchGuardKey = (id) => `vjob:${id}:dispatched`;
const stitchGuardKey = (id) => `vjob:${id}:stitch`;

function r() { return sharedConnection(); }

// scene is "ready to stitch" when its narration exists and a visual exists
// (a clip that fell back to an image counts as ready).
export function sceneComplete(scene) {
  return (
    scene &&
    scene.audioStatus === "done" &&
    (scene.visualStatus === "done" || scene.visualStatus === "fallback")
  );
}
export function sceneFailed(scene) {
  return scene && (scene.audioStatus === "failed" || scene.visualStatus === "failed");
}

// Map a segmenter scene → its visual kind. Accepts the Python segmenter's
// snake_case (clip_eligible) AND camelCase (clipEligible); explicit `kind` wins.
export function sceneKind(scene) {
  return scene.kind ?? ((scene.clipEligible ?? scene.clip_eligible) ? "clip" : "image");
}

/** Create the job JSON + one hash per scene. */
export async function createJob(job) {
  const { jobId, scenes = [] } = job;
  const meta = { ...job };
  delete meta.scenes;
  meta.createdAt = meta.createdAt || Date.now();
  meta.status = meta.status || "queued";
  const pipe = r().pipeline();
  pipe.set(jobKey(jobId), JSON.stringify(meta), "EX", TTL);
  scenes.forEach((s, i) => {
    pipe.hset(sceneKey(jobId, i), {
      number: String(s.number ?? i + 1),
      text: s.text ?? "",
      visualPrompt: s.visualPrompt ?? s.visual_prompt ?? "",
      kind: sceneKind(s),
      estSeconds: String(s.estSeconds ?? s.est_seconds ?? 0),
      audioStatus: "pending",
      visualStatus: "pending",
    });
    pipe.expire(sceneKey(jobId, i), TTL);
  });
  await pipe.exec();
  return meta;
}

export async function getMeta(jobId) {
  const raw = await r().get(jobKey(jobId));
  return raw ? JSON.parse(raw) : null;
}

/** Merge a partial into the job JSON (orchestrator/check only). */
export async function patchMeta(jobId, partial) {
  const cur = (await getMeta(jobId)) || {};
  const next = { ...cur, ...partial };
  await r().set(jobKey(jobId), JSON.stringify(next), "EX", TTL);
  return next;
}

export async function setStatus(jobId, status, extra = {}) {
  const next = await patchMeta(jobId, { status, ...extra });
  // Phase 3: release the tenant's concurrency slot the moment a job goes terminal.
  // setStatus is the single status chokepoint, so this covers EVERY path (orchestrator
  // done/fail + the cancel endpoint). release() is idempotent, so double-fire is safe.
  if (next?.tenantId && (status === "done" || status === "failed" || status === "canceled")) {
    try { const { release } = await import("./concurrency.mjs"); await release(next.tenantId, jobId); }
    catch { /* non-fatal — a stranded slot self-heals via its TTL */ }
  }
  return next;
}

/** Write disjoint scene fields (HSET, atomic per field). */
export async function setSceneFields(jobId, i, fields) {
  const flat = {};
  for (const [k, v] of Object.entries(fields)) flat[k] = v === null || v === undefined ? "" : String(v);
  await r().hset(sceneKey(jobId, i), flat);
  await r().expire(sceneKey(jobId, i), TTL);
}

export async function getScene(jobId, i) {
  const h = await r().hgetall(sceneKey(jobId, i));
  return h && Object.keys(h).length ? h : null;
}

export async function getScenes(jobId, count) {
  const pipe = r().pipeline();
  for (let i = 0; i < count; i++) pipe.hgetall(sceneKey(jobId, i));
  const res = await pipe.exec();
  return res.map(([, h]) => (h && Object.keys(h).length ? h : null));
}

/** Atomic: returns true only for the FIRST caller that dispatches this batch. */
export async function tryClaimBatch(jobId, batchIndex) {
  const added = await r().sadd(batchGuardKey(jobId), String(batchIndex));
  await r().expire(batchGuardKey(jobId), TTL);
  return added === 1;
}
export async function dispatchedBatches(jobId) {
  const m = await r().smembers(batchGuardKey(jobId));
  return new Set(m.map(Number));
}

/** Atomic: returns true only for the FIRST caller that kicks off the stitch. */
export async function tryClaimStitch(jobId) {
  const ok = await r().set(stitchGuardKey(jobId), "1", "EX", TTL, "NX");
  return ok === "OK";
}

export async function destroy(jobId, count = 0) {
  const pipe = r().pipeline();
  pipe.del(jobKey(jobId), batchGuardKey(jobId), stitchGuardKey(jobId));
  for (let i = 0; i < count; i++) pipe.del(sceneKey(jobId, i));
  await pipe.exec();
}
