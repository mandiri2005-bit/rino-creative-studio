/**
 * redis.js — live job progress store (replaces the in-memory activeJobs Map).
 *
 * Holds the rich running-job object (progress, total, logs[], files[], status)
 * for TTS + Imagen jobs while they run, so status survives a backend restart.
 * Durable job records still live in Postgres (db.js); this is ephemeral live state.
 *
 * Uses ioredis. Connection comes from REDIS_URL (same Upstash/Redis as the rest
 * of the stack). For Upstash over TLS use a rediss:// URL — ioredis enables TLS
 * automatically from the scheme.
 */
import Redis from "ioredis";

const REDIS_URL = process.env.REDIS_URL || "redis://127.0.0.1:6379";
const redis = new Redis(REDIS_URL, { maxRetriesPerRequest: 3 });
redis.on("error", (e) => console.error("[redis]", e.message));

const KEY = (jobId) => `job:${jobId}`;
const TTL_SECONDS = 60 * 60 * 24; // expire live state 24h after last write

/** Create/overwrite the live job object. */
async function setLiveJob(jobId, obj) {
  await redis.set(KEY(jobId), JSON.stringify(obj), "EX", TTL_SECONDS);
}

/** Read the live job object, or null if absent/expired. */
async function getLiveJob(jobId) {
  const raw = await redis.get(KEY(jobId));
  return raw ? JSON.parse(raw) : null;
}

/**
 * Atomically mutate the live job object via a callback.
 * Read-modify-write; fine for our single-runner-per-job model (no concurrent writers).
 */
async function updateLiveJob(jobId, mutate) {
  const cur = (await getLiveJob(jobId)) || {};
  const next = mutate(cur) || cur;
  await setLiveJob(jobId, next);
  return next;
}

/** Append a log line to the live job. */
async function pushLiveLog(jobId, line) {
  return updateLiveJob(jobId, (j) => {
    j.logs = j.logs || [];
    j.logs.push(line);
    return j;
  });
}

/** Delete live state (optional; TTL also handles it). */
async function delLiveJob(jobId) {
  await redis.del(KEY(jobId));
}

// ── Lightweight rate-limit + per-key lock ────────────────────────────────────
// FAIL-OPEN by design: a Redis hiccup must never block a legitimate paying user.
// rateLimitOk: true if this call is within `limit` per rolling `windowSec`.
async function rateLimitOk(key, limit, windowSec) {
  try {
    const n = await redis.incr(`rl:${key}`);
    if (n === 1) await redis.expire(`rl:${key}`, windowSec);
    return n <= limit;
  } catch { return true; }
}
// acquireLock: SET NX EX — true if acquired. Release with releaseLock (or let the TTL expire).
async function acquireLock(key, ttlSec) {
  try { return (await redis.set(`lock:${key}`, "1", "EX", ttlSec, "NX")) === "OK"; }
  catch { return true; }
}
async function releaseLock(key) {
  try { await redis.del(`lock:${key}`); } catch { /* TTL will expire it */ }
}

export { redis, setLiveJob, getLiveJob, updateLiveJob, pushLiveLog, delLiveJob, rateLimitOk, acquireLock, releaseLock };
