// ─────────────────────────────────────────────────────────────────────────────
// video/connection.mjs — the BullMQ Redis connection + queue names.
//
// BullMQ requires an ioredis connection with `maxRetriesPerRequest: null` (it
// manages blocking commands itself). The rest of the app uses a connection with
// `maxRetriesPerRequest: 3` (redis.js), so the video engine gets its OWN
// connection here rather than sharing that one. Same REDIS_URL / same server —
// the running `redis` container the whole stack already uses.
//
// Queue names are the three workers from the roadmap plus the stitch step:
//   audio  — TTS per scene → ffprobe-measure real duration → upload
//   visual — image (Ken Burns) or clip (Veo/Kling) per scene → upload
//   check  — batch-completion checker: dispatch the next batch, then stitch
//   stitch — the final FFmpeg fuse into one MP4
// ─────────────────────────────────────────────────────────────────────────────
import Redis from "ioredis";

export const REDIS_URL = process.env.REDIS_URL || "redis://127.0.0.1:6379";

// BullMQ mandates maxRetriesPerRequest=null and enableReadyCheck=false on the
// connection it blocks on. One shared connection is fine for queues + workers in
// the same process; the standalone worker entry creates its own.
export function makeConnection() {
  const conn = new Redis(REDIS_URL, {
    maxRetriesPerRequest: null,   // BullMQ requirement (it manages blocking commands)
    enableReadyCheck: false,
    // Upstash is remote TLS — without these a transient drop surfaced as repeated
    // `connect ETIMEDOUT` + BullMQ `could not renew lock` and never recovered.
    connectTimeout: 20000,        // give the cross-network TLS handshake room (default 10s)
    keepAlive: 30000,             // keep the socket warm so Upstash/NAT doesn't reap an idle conn
    retryStrategy: (times) => Math.min(1000 + times * 500, 8000), // reconnect w/ backoff, never give up
    reconnectOnError: () => true, // also reconnect on command-level errors (e.g. READONLY/failover)
  });
  conn.on("error", (e) => console.warn("[video/redis]", e.code || e.message)); // log, don't crash
  return conn;
}

// A lazily-created shared connection for queue producers (the API side).
let _shared = null;
export function sharedConnection() {
  if (!_shared) _shared = makeConnection(); // makeConnection already attaches an 'error' handler
  return _shared;
}

// NB: BullMQ forbids ":" in queue names (it is the internal Redis key separator).
export const QUEUE = Object.freeze({
  AUDIO:  "video-audio",
  VISUAL: "video-visual",
  CHECK:  "video-check",
  STITCH: "video-stitch",
});

// Worker concurrency per queue. Audio/visual fan out wide (the whole point);
// stitch is one-at-a-time per job and CPU-heavy, so keep it low.
export const CONCURRENCY = Object.freeze({
  [QUEUE.AUDIO]:  Number(process.env.VIDEO_AUDIO_CONCURRENCY  || 10),
  [QUEUE.VISUAL]: Number(process.env.VIDEO_VISUAL_CONCURRENCY || 10),
  [QUEUE.CHECK]:  Number(process.env.VIDEO_CHECK_CONCURRENCY  || 4),
  [QUEUE.STITCH]: Number(process.env.VIDEO_STITCH_CONCURRENCY || 2),
});

// Default job options: keep the queues from growing unbounded, retry transient
// failures with backoff.
export const DEFAULT_JOB_OPTS = Object.freeze({
  attempts: Number(process.env.VIDEO_JOB_ATTEMPTS || 2),
  backoff: { type: "exponential", delay: 4000 },
  removeOnComplete: { age: 3600, count: 1000 },
  removeOnFail: { age: 24 * 3600 },
});

// Audio/visual jobs call METERED upstream endpoints (each call debits credits in
// Python). A BullMQ retry would re-debit, so these get attempts:1 — the processors
// already do their own clip→image fallback, and a worker-level 'failed' handler
// marks the scene terminally failed so the job still converges. (Idempotency is
// also guarded in-processor: a re-delivered job whose asset already landed skips
// the metered call.)
export const METERED_JOB_OPTS = Object.freeze({
  ...DEFAULT_JOB_OPTS,
  attempts: 1,
});
