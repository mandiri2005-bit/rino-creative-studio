// ─────────────────────────────────────────────────────────────────────────────
// video/runtime.mjs — the "FFmpeg never on the API event loop" guard.
//
// The Step 3 lesson, encoded. Heavy work (FFmpeg encode, per-scene generation)
// must run ONLY inside the BullMQ worker PROCESS — never inside an Express HTTP
// handler. In Node, spawn() doesn't freeze the event loop the way Step 3's
// synchronous Python did, but an ffmpeg encode still pins CPU; sharing the API
// process degrades every other request and removes BullMQ's concurrency cap.
//
// So: the worker entrypoint (worker-entry.mjs) calls markVideoWorker(); the API
// process (server.js) never does. assertWorkerProcess() then makes a stray call
// from a request handler fail LOUDLY in dev instead of silently saturating the
// API. CLIs / tests opt in with VIDEO_WORKER=1.
// ─────────────────────────────────────────────────────────────────────────────

let _isWorker = false;

/** Called once at startup by the dedicated worker process. */
export function markVideoWorker() {
  _isWorker = true;
}

/** True only inside the worker process (or an explicit CLI opt-in). */
export function isVideoWorker() {
  return _isWorker || process.env.VIDEO_WORKER === "1";
}

/**
 * Throw unless we're in the worker process. Call at the top of any CPU-heavy /
 * long-running operation (ffmpeg encode, etc.) so it can never run on the API
 * event loop by accident.
 */
export function assertWorkerProcess(op = "this operation") {
  if (!isVideoWorker()) {
    throw new Error(
      `[video] ${op} must run inside the BullMQ worker process, not an HTTP handler. ` +
      `Heavy ffmpeg/generation pins CPU and must stay off the API process (the Step 3 trap). ` +
      `Run 'node backend/video/worker-entry.mjs' (it calls markVideoWorker()), or set VIDEO_WORKER=1 for a one-off.`
    );
  }
}
