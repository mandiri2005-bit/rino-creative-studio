// ─────────────────────────────────────────────────────────────────────────────
// start.mjs — role-branching entrypoint. ONE image, the role chosen by env.
//
//   VIDEO_ROLE=worker  → the Step 6 BullMQ video workers (audio/visual/check/
//                        stitch) in their own process — ffmpeg/generation off the
//                        API event loop (the Step 3 discipline).
//   otherwise          → the Express API server (server.js), exactly as before.
//
// Lets a second Railway service (or `docker run -e VIDEO_ROLE=worker`) reuse the
// backend image to run the workers, without a separate Dockerfile or start cmd.
// ─────────────────────────────────────────────────────────────────────────────
if (process.env.VIDEO_ROLE === "worker") {
  await import("./video/worker-entry.mjs");
} else {
  await import("./server.js");
}
