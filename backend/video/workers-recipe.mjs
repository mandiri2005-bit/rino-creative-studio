// ─────────────────────────────────────────────────────────────────────────────
// video/workers-recipe.mjs — the avatar-recipe (product-ad + spokesperson) BullMQ
// trigger worker.
//
// BULLMQ MIGRATION Phase 3 (default OFF; only started when RECIPE_BULLMQ_ENABLED).
// Same "trigger + wait" shape as workers-videoclip.mjs: it owns NO money-path. The
// whole recipe DAG + the ONE umbrella hold/commit/refund live in Python
// (recipe_product_ad.run_product_ad_job / recipe_spokesperson.run_spokesperson_job,
// reused unchanged). The worker just calls `POST /recipes/<slug>/run {job_id, slug}`
// — IDEMPOTENT (a job already terminal returns immediately without re-running the
// DAG or re-charging) — and waits for the already-persisted job row to settle.
//
// Durability + queue concurrency:
//   • a worker crash mid-run re-delivers → /recipes/<slug>/run re-runs safely
//     (terminal → no-op; else resumes; the umbrella hold/commit are op_id-idempotent).
//   • RECIPE_CLIP_CONCURRENCY caps concurrent recipes (mirrors Python RECIPE_MAX_INFLIGHT).
//
// The FE keeps polling the SAME Python GET /recipes/<slug>/jobs/<id> → no FE change.
// When RECIPE_BULLMQ_ENABLED is unset, this worker is never registered and the old
// asyncio path in Python runs unchanged.
// ─────────────────────────────────────────────────────────────────────────────
import { Worker } from "bullmq";
import { QUEUE, CONCURRENCY, makeConnection } from "./connection.mjs";

const PYTHON_API = process.env.PYTHON_API_URL || "http://127.0.0.1:8000";
const INTERNAL_SECRET = process.env.INTERNAL_SERVICE_SECRET || "";
// A recipe is the heaviest job (multiple clips × variants + TTS + per-aspect ffmpeg) → very generous
// fetch ceiling. Python's own RECIPE_JOB_STALE_SECS (default 1h) bounds the DAG inside.
const RUN_FETCH_TIMEOUT_MS = Number(process.env.RECIPE_RUN_FETCH_TIMEOUT_MS || 90 * 60 * 1000);

const RECIPE_SLUGS = new Set(["product-ad", "spokesperson"]);

function authHeaders(data) {
  return {
    "Content-Type": "application/json",
    "X-Internal-Secret": INTERNAL_SECRET,
    "X-Internal-Tenant-Id": data.tenantId || "",
    "X-Internal-User-Id": data.userId || data.tenantId || "",
  };
}

// POST /recipes/<slug>/run {job_id, slug} and await the terminal status. Non-2xx or a still-'running'
// body throws so BullMQ retries (bounded). Python idempotency makes a retry a safe no-op once terminal.
export async function recipeProcessor(job) {
  const { jobId, slug, tenantId } = job.data || {};
  if (!jobId) throw new Error("recipe job missing jobId");
  if (!RECIPE_SLUGS.has(slug)) throw new Error(`recipe job unknown slug: ${slug}`);
  const r = await fetch(`${PYTHON_API}/recipes/${slug}/run`, {
    method: "POST",
    headers: authHeaders(job.data || {}),
    body: JSON.stringify({ job_id: jobId, slug }),
    signal: RUN_FETCH_TIMEOUT_MS > 0 ? AbortSignal.timeout(RUN_FETCH_TIMEOUT_MS) : undefined,
  });
  if (!r.ok) {
    if (r.status === 404) return { ok: true, jobId, status: "gone", tenantId };  // reaped/recycled
    const txt = await r.text().catch(() => "");
    throw new Error(`/recipes/${slug}/run ${r.status}: ${txt.slice(0, 200)}`);
  }
  const data = await r.json().catch(() => ({}));
  const status = data.status || "unknown";
  if (status === "running") throw new Error(`/recipes/${slug}/run left job ${jobId} still running`);
  return { ok: true, jobId, status, tenantId };
}

// attempts:2 → one crash-retry (safe: /run is idempotent). A recipe is expensive, so keep it to one.
export const RECIPE_JOB_OPTS = Object.freeze({
  attempts: Number(process.env.RECIPE_CLIP_ATTEMPTS || 2),
  backoff: { type: "exponential", delay: 10000 },
  removeOnComplete: { age: 3600, count: 1000 },
  removeOnFail: { age: 24 * 3600 },
});

/** Register the recipe trigger worker. Returns the Worker (call .close() to drain). */
export function startRecipeWorker() {
  const w = new Worker(QUEUE.RECIPE, (job) => recipeProcessor(job), {
    connection: makeConnection(),
    concurrency: CONCURRENCY[QUEUE.RECIPE] || 2,
    lockDuration: Number(process.env.WB_LOCK_DURATION_MS) || 600000,
    stalledInterval: 60000,
    drainDelay: Number(process.env.VIDEO_DRAIN_DELAY) || 60,
  });
  w.on("failed", (job, err) =>
    console.warn(`[recipe ${job?.data?.jobId}] failed: ${err?.message || err}`));
  return w;
}
