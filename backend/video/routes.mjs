// ─────────────────────────────────────────────────────────────────────────────
// video/routes.mjs — the API surface for the video engine, mounted on server.js.
//
// CRITICAL: these run in the API (Express) PROCESS. They only ENQUEUE work and
// read state — they NEVER call ffmpeg or generate. The heavy lifting happens in
// the separate worker process (worker-entry.mjs). startAssembly only adds BullMQ
// jobs, so the runFfmpeg guard is never tripped here (the Step 3 discipline).
//
//   POST /api/video/segment  → proxy to Python video_segmenter (topic/text → scenes)
//   POST /api/video/params   → proxy to Python (duration → scene_count/credits)
//   POST /api/video/assemble → enqueue a job, return { jobId, batchPlan, … }
//   GET  /api/video/assemble/:jobId → job + scene state for polling
// ─────────────────────────────────────────────────────────────────────────────
import { makeQueues } from "./workers.mjs";
import * as store from "./store.mjs";
import { startAssembly } from "./orchestrator.mjs";
import * as storage from "../storage.mjs";

const PYTHON_API = process.env.PYTHON_API_URL || "http://127.0.0.1:8000";

let _deps = null;
function deps() {
  if (!_deps) {
    _deps = { store, queues: makeQueues(), credits: { async precheck() { return true; } } };
  }
  return _deps;
}

async function pyForward(req, res, path) {
  try {
    const r = await fetch(`${PYTHON_API}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(req.headers.authorization && { Authorization: req.headers.authorization }),
      },
      body: JSON.stringify(req.body || {}),
    });
    res.status(r.status);
    res.json(await r.json());
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
}

function genJobId() {
  return `vid_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

/**
 * Mount the routes. Pass the host app's auth middleware + tenant/user resolvers
 * (from server.js) so the engine inherits the same identity model.
 */
export function mountVideoRoutes(app, { requireAuth, resolveTenantId, resolveUserId } = {}) {
  const auth = requireAuth || ((req, res, next) => next());

  app.post("/api/video/segment", auth, (req, res) => pyForward(req, res, "/video/segment"));
  app.post("/api/video/params", auth, (req, res) => pyForward(req, res, "/video/params"));
  app.post("/api/video/decide", auth, (req, res) => pyForward(req, res, "/video/decide"));

  app.post("/api/video/assemble", auth, async (req, res) => {
    try {
      const b = req.body || {};
      const scenes = b.scenes;
      if (!Array.isArray(scenes) || scenes.length === 0) {
        return res.status(400).json({ error: "scenes[] required (call /api/video/segment first)" });
      }
      const tenantId = resolveTenantId ? await resolveTenantId(req) : (b.tenantId || "anon");
      // resolveUserId needs the tenant to scope the lookup + RLS context — every
      // other caller passes it; omitting it returns null for real Clerk users.
      const userId = resolveUserId ? await resolveUserId(req, tenantId) : (b.userId || tenantId);
      const result = await startAssembly({
        jobId: genJobId(), tenantId, userId, scenes,
        tier: b.tier || "hd", clipModel: b.clipModel || "veo3",
        visualMode: b.visualMode || "hybrid", captions: !!b.captions,
        voice: b.voice, imageModel: b.imageModel,
        ttsModel: b.ttsModel, language: b.language, aspectRatio: b.aspectRatio,
      }, deps());
      res.json({ ok: true, status: "running", ...result });
    } catch (e) {
      res.status(e.status || 500).json({ error: e.message, creditsNeeded: e.creditsNeeded });
    }
  });

  // Cancel a running job: mark it terminal (in-flight workers skip on next check)
  // and refund what it consumed so far. Uses the caller's own auth for the refund.
  app.post("/api/video/assemble/:jobId/cancel", auth, async (req, res) => {
    const jobId = req.params.jobId;
    const meta = await store.getMeta(jobId);
    if (!meta) return res.status(404).json({ error: "job not found" });
    if (!["done", "failed", "canceled"].includes(meta.status)) {
      await store.setStatus(jobId, "canceled", { error: "canceled by user" });
    }
    let refunded = null;
    try {
      const r = await fetch(`${PYTHON_API}/video/credits/refund`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(req.headers.authorization && { Authorization: req.headers.authorization }) },
        body: JSON.stringify({ job_id: jobId }),
      });
      if (r.ok) refunded = (await r.json()).refunded;
    } catch { /* best effort */ }
    res.json({ ok: true, status: "canceled", refunded });
  });

  app.get("/api/video/assemble/:jobId", auth, async (req, res) => {
    const meta = await store.getMeta(req.params.jobId);
    if (!meta) return res.status(404).json({ error: "job not found" });
    const scenes = await store.getScenes(req.params.jobId, meta.sceneCount || 0);
    // hand the browser a playable URL for the finished MP4 (signed, short-lived)
    let mp4Url = null;
    if (meta.mp4Key && storage.isConfigured?.()) {
      try { mp4Url = await storage.signedUrl(meta.mp4Key, 3600); } catch { /* ignore */ }
    }
    res.json({ ...meta, scenes, mp4Url });
  });
}
