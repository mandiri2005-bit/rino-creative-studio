// ─────────────────────────────────────────────────────────────────────────────
// video/workers-videoclip.mjs — the single-clip /video-tools BullMQ trigger worker.
//
// BULLMQ MIGRATION Phase 2 (default OFF; only started when VIDEO_BULLMQ_ENABLED).
// This worker is a THIN "trigger + wait": it owns NO money-path. The whole
// dispatch + metering commit/refund lives in Python (video_providers.dispatch +
// metering, reused unchanged). The worker just calls back into the Python
// endpoint `POST /video-tools/run {job_id}` — which is IDEMPOTENT (a job already
// terminal returns immediately without re-dispatching or re-charging) — and waits
// for it to drive the already-persisted job row to a terminal state.
//
// Durability + queue concurrency are what BullMQ buys us:
//   • a worker crash mid-run re-delivers the job (attempts>1) → /video-tools/run
//     re-runs safely (terminal → no-op; still-running → resumes the dispatch).
//   • VIDEOCLIP_WORKER_CONCURRENCY caps concurrent clips (mirrors Python VIDEO_MAX_INFLIGHT).
//
// The FE keeps polling the SAME Python GET /video-tools/jobs/<id> (job row stays
// Python-owned) → no FE change. When VIDEO_BULLMQ_ENABLED is unset, this worker is
// never registered and the old asyncio path in Python runs unchanged.
// ─────────────────────────────────────────────────────────────────────────────
import { Worker } from "bullmq";
import { QUEUE, CONCURRENCY, makeConnection } from "./connection.mjs";

const PYTHON_API = process.env.PYTHON_API_URL || "http://127.0.0.1:8000";
const INTERNAL_SECRET = process.env.INTERNAL_SERVICE_SECRET || "";
// A single-clip /run drives a full provider dispatch (submit → poll → persist) which can take minutes.
// Give the fetch a generous ceiling; Python's own VIDEO_JOB_STALE_SECS bounds the dispatch inside.
const RUN_FETCH_TIMEOUT_MS = Number(process.env.VIDEO_RUN_FETCH_TIMEOUT_MS || 30 * 60 * 1000);

// Worker→Python internal-service auth (same trusted-header scheme as generationClient / /video/meter):
// Python accepts these ONLY when X-Internal-Secret matches, then builds the tenant's RLS + metering ctx.
function authHeaders(data) {
  return {
    "Content-Type": "application/json",
    "X-Internal-Secret": INTERNAL_SECRET,
    "X-Internal-Tenant-Id": data.tenantId || "",
    "X-Internal-User-Id": data.userId || data.tenantId || "",
  };
}

// The processor: POST /video-tools/run {job_id} and await the terminal status. Any non-2xx or a
// still-'running' body (Python couldn't settle) throws so BullMQ retries (bounded by attempts). The
// Python endpoint's idempotency makes a retry a safe no-op once the job is terminal.
export async function videoClipProcessor(job) {
  const { jobId, tenantId } = job.data || {};
  if (!jobId) throw new Error("videoclip job missing jobId");
  const r = await fetch(`${PYTHON_API}/video-tools/run`, {
    method: "POST",
    headers: authHeaders(job.data || {}),
    body: JSON.stringify({ job_id: jobId }),
    signal: RUN_FETCH_TIMEOUT_MS > 0 ? AbortSignal.timeout(RUN_FETCH_TIMEOUT_MS) : undefined,
  });
  if (!r.ok) {
    // 404 = the job row is gone (poll-reaped / process recycled). Nothing to retry — treat as done so
    // the queue doesn't churn; the hold was already refunded by the reaper (idempotent).
    if (r.status === 404) return { ok: true, jobId, status: "gone", tenantId };
    const txt = await r.text().catch(() => "");
    throw new Error(`/video-tools/run ${r.status}: ${txt.slice(0, 200)}`);
  }
  const data = await r.json().catch(() => ({}));
  const status = data.status || "unknown";
  // If Python still reports 'running' after /run returned, the dispatch didn't converge — let BullMQ
  // retry (attempts) rather than swallow it. (Normal outcomes are 'success' or 'failed'.)
  if (status === "running") throw new Error(`/video-tools/run left job ${jobId} still running`);
  return { ok: true, jobId, status, tenantId };
}

// Job options for the videoclip queue. attempts:2 → one crash-retry (safe: /run is idempotent).
// Backoff spaces the retry so a transient upstream saturation clears.
export const VIDEOCLIP_JOB_OPTS = Object.freeze({
  attempts: Number(process.env.VIDEO_CLIP_ATTEMPTS || 2),
  backoff: { type: "exponential", delay: 8000 },
  removeOnComplete: { age: 3600, count: 1000 },
  removeOnFail: { age: 24 * 3600 },
});

/** Register the single-clip trigger worker. Returns the Worker (call .close() to drain). */
export function startVideoClipWorker() {
  const w = new Worker(QUEUE.VIDEOCLIP, (job) => videoClipProcessor(job), {
    connection: makeConnection(),
    concurrency: CONCURRENCY[QUEUE.VIDEOCLIP] || 4,
    lockDuration: Number(process.env.WB_LOCK_DURATION_MS) || 600000,   // long dispatch → long lock
    stalledInterval: 60000,
    drainDelay: Number(process.env.VIDEO_DRAIN_DELAY) || 60,
  });
  w.on("failed", (job, err) =>
    console.warn(`[videoclip ${job?.data?.jobId}] failed: ${err?.message || err}`));
  return w;
}
