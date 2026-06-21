// ─────────────────────────────────────────────────────────────────────────────
// video/worker-entry.mjs — the dedicated video-worker PROCESS entrypoint.
//
// Run as its own process / docker-compose service, separate from the Express API:
//   node backend/video/worker-entry.mjs
//
// It calls markVideoWorker() FIRST, which is the only thing that lifts the
// ffmpeg guard — so heavy encode + per-scene generation run here, off the API
// event loop (the Step 3 discipline). The API process never imports this file.
//
// Generation backend: httpGenerationClient (live Python) by default; set
// VIDEO_SYNTHETIC=1 to run the deterministic ffmpeg-only synthetic generator
// (no API keys) for local end-to-end demos.
// ─────────────────────────────────────────────────────────────────────────────
import { markVideoWorker } from "./runtime.mjs";
markVideoWorker();

import { startWorkers, makeDeps } from "./workers.mjs";
import { httpGenerationClient, syntheticGenerationClient } from "./generationClient.mjs";

const synthetic = process.env.VIDEO_SYNTHETIC === "1";
if (!synthetic && !process.env.INTERNAL_SERVICE_SECRET) {
  // Real per-scene generation would hit Python WITHOUT internal-service auth →
  // no per-tenant metering and no RLS tenant context. Refuse to start dark.
  console.error(
    "[video-worker] FATAL: INTERNAL_SERVICE_SECRET is unset but VIDEO_SYNTHETIC!=1. " +
    "Per-scene generation would run UNMETERED and without tenant RLS. " +
    "Set INTERNAL_SERVICE_SECRET (same value as the Python API), or VIDEO_SYNTHETIC=1 for a keyless demo."
  );
  process.exit(1);
}
const generationClient = synthetic ? syntheticGenerationClient() : httpGenerationClient();

const engine = startWorkers(makeDeps({ generationClient }));

// Auto-expire finished videos under the R2 `videos/` prefix after VIDEO_R2_TTL_DAYS (default 7).
// OPT-IN via VIDEO_R2_LIFECYCLE=1: the R2 API token usually lacks PutBucketLifecycle perms, so this
// just logged "Access Denied" on EVERY boot. R2 lifecycle is simplest set ONCE in the Cloudflare
// dashboard (Bucket → Settings → Object lifecycle rules → expire prefix `videos/` after 7 days).
// Flip the flag only if the token has lifecycle perms and you want it managed from code.
if (!synthetic && /^(1|true|yes)$/i.test(process.env.VIDEO_R2_LIFECYCLE || "")) {
  import("../storage.mjs")
    .then((s) => s.isConfigured?.() && s.ensureVideoLifecycle()
      .then(() => console.log("[video-worker] R2 videos/ lifecycle ensured"))
      .catch((e) => console.warn("[video-worker] lifecycle setup skipped (set the rule in the Cloudflare dashboard):", e.message)))
    .catch(() => {});
}

const mode = process.env.VIDEO_SYNTHETIC === "1" ? "synthetic" : "http";
console.log(`[video-worker] up — audio/visual/check/stitch workers running (generation: ${mode})`);

// GRACEFUL SHUTDOWN — on redeploy/restart Railway sends SIGTERM before SIGKILL. Stop accepting new
// jobs and let ACTIVE ones finish (BullMQ worker.close() drains in-flight processors) so a deploy
// doesn't kill a mid-render job (→ stall/re-run/re-charge). Capped by WB_DRAIN_TIMEOUT_MS so a very
// long render can't block the exit forever (it just re-queues). Pair with the pre-deploy gate
// (video/predeploy-check.mjs) which defers the deploy itself while jobs are active.
let _draining = false;
async function shutdown(sig) {
  if (_draining) return; _draining = true;
  const maxMs = Number(process.env.WB_DRAIN_TIMEOUT_MS) || 120000;
  console.log(`[video-worker] ${sig} → draining active jobs before exit (max ${maxMs}ms)…`);
  const t = setTimeout(() => { console.warn(`[video-worker] drain exceeded ${maxMs}ms → exiting; unfinished jobs re-queue`); process.exit(0); }, maxMs);
  try { await engine.close(); console.log("[video-worker] drained cleanly → exit"); }
  catch (e) { console.warn("[video-worker] drain error:", e.message); }
  finally { clearTimeout(t); process.exit(0); }
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
