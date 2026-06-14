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

// One-time, idempotent: auto-expire finished videos under the R2 `videos/` prefix
// after VIDEO_R2_TTL_DAYS (default 7). Best-effort — never blocks worker startup.
if (!synthetic) {
  import("../storage.mjs")
    .then((s) => s.isConfigured?.() && s.ensureVideoLifecycle()
      .then(() => console.log("[video-worker] R2 videos/ 7-day lifecycle ensured"))
      .catch((e) => console.warn("[video-worker] lifecycle setup skipped:", e.message)))
    .catch(() => {});
}

const mode = process.env.VIDEO_SYNTHETIC === "1" ? "synthetic" : "http";
console.log(`[video-worker] up — audio/visual/check/stitch workers running (generation: ${mode})`);

async function shutdown(sig) {
  console.log(`[video-worker] ${sig} — draining…`);
  try { await engine.close(); } finally { process.exit(0); }
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
