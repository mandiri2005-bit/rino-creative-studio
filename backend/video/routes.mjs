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
import { query } from "../db.js";
import * as conc from "./concurrency.mjs";   // per-plan parallel-job cap (Phase 3)

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

// Generate the per-video reference anchor from the art-direction brief. Stored in R2
// (workers download it once) or returned inline when R2 isn't configured. Metered to
// the caller via their Authorization header.
async function generateAnchorImage(brief, imageModel, aspectRatio, authHeader, tenantId, jobId) {
  const prompt = (`${brief}. A clean, well-lit reference image of the main character and ` +
    `setting — the canonical look every scene must match. Single clear subject, neutral composition.`).slice(0, 560);
  const r = await fetch(`${PYTHON_API}/generate-image`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(authHeader && { Authorization: authHeader }) },
    body: JSON.stringify({ model: imageModel || "nano-banana-hd", prompt, aspect_ratio: aspectRatio || "16:9" }),
  });
  if (!r.ok) throw new Error(`generate-image ${r.status}: ${(await r.text()).slice(0, 120)}`);
  const data = await r.json();
  const b64 = data.image_b64 || data.b64 || data.data?.[0]?.b64_json;
  if (!b64) throw new Error("no image in anchor response");
  if (storage.isConfigured?.()) {
    const key = storage.buildKey(tenantId, jobId, "anchor", "anchor.png");
    await storage.uploadBytes(key, Buffer.from(b64, "base64"), "image/png");
    return { key };
  }
  return { b64 };
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

  // Operator-driven defaults (Rino sets these in Railway; the UI hides the model selectors and just
  // consumes these). Generic for ALL Video Instant modes, not only whiteboard.
  app.get("/api/video/config", auth, (req, res) => {
    res.set("Cache-Control", "no-store");
    res.json({
      genModel: process.env.VI_GEN_MODEL || "deepseek-chat",
      ttsModel: process.env.VI_TTS_MODEL || "gemini-2.5-flash-preview-tts",   // default Gemini → UI shows the 30 gendered voices (Rino overrides via env)
    });
  });

  // Server-backed "Video saya": the tenant's recent videos (cross-device; not just this browser's
  // localStorage). Done videos carry a fresh signed mp4Url so they play directly.
  app.get("/api/video/jobs", auth, async (req, res) => {
    res.set("Cache-Control", "no-store");
    const tenantId = resolveTenantId ? await resolveTenantId(req) : null;
    if (!tenantId) return res.json({ jobs: [] });
    const rows = await store.listTenantJobs(tenantId, 24).catch(() => []);
    const jobs = [];
    for (const j of rows) {
      let mp4Url = null;
      if (j.status === "done" && j.mp4Key && storage.isConfigured?.()) {
        try { mp4Url = await storage.signedUrl(j.mp4Key, 3600); } catch { /* ignore */ }
      }
      jobs.push({ jobId: j.jobId, status: j.status, sceneCount: j.sceneCount, createdAt: j.createdAt,
        durationActual: j.durationActual, visualMode: j.visualMode, whiteboardGenre: j.whiteboardGenre, mp4Url });
    }
    res.json({ jobs });
  });

  app.post("/api/video/assemble", auth, async (req, res) => {
    let _slotTenant = null, _slotJob = null;   // for releasing the concurrency slot on early failure
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
      const jobId = genJobId();
      // Phase 3 per-plan concurrency: ONE slot per assembly, released when the job
      // goes terminal (store.setStatus). 429 if the tenant is already at its cap.
      const _planRow = await query(`SELECT plan FROM tenants WHERE id=$1`, [tenantId], tenantId).catch(() => null);
      const _plan = _planRow?.rows?.[0]?.plan || "free";
      if (!(await conc.acquire(tenantId, _plan, jobId))) {
        const lim = conc.capFor(_plan);
        return res.status(429).json({ error: "concurrency_limit", limit: lim, plan: _plan,
          message: `Kamu lagi menjalankan ${lim} render video paralel (paket ${_plan}). Tunggu salah satu selesai, atau upgrade buat lebih banyak job bersamaan.` });
      }
      _slotTenant = tenantId; _slotJob = jobId;
      // Global admission cap (Change 2): bound TOTAL in-flight assemblies across ALL tenants so a
      // launch spike queues at the door with a friendly message instead of 100 jobs piling onto the
      // single worker and starving each other behind the 2-wide stitch queue. Default-off
      // (VIDEO_MAX_INFLIGHT_JOBS unset on the API service). Release the per-tenant slot we just took.
      if (!(await conc.acquireGlobal(jobId))) {
        try { await conc.release(tenantId, jobId); } catch {}
        _slotTenant = null; _slotJob = null;
        res.set("Retry-After", "90");
        return res.status(429).json({ error: "server_busy", retryAfter: 90,
          message: "Lagi ramai banget 🙏 Antrean render lagi penuh — video kamu belum bisa diproses sekarang. Coba lagi 1-2 menit ya." });
      }
      // Reference-image consistency: generate ONE anchor from the brief up front, so
      // every scene (rendered in PARALLEL) can use it as ref_image — the anchor is
      // ready before the fan-out, so parallelism is preserved.
      let anchorKey = null, anchorB64 = null;
      if (b.refMode && (b.brief || "").trim()) {
        try {
          const a = await generateAnchorImage(b.brief, b.imageModel, b.aspectRatio, req.headers.authorization, tenantId, jobId);
          anchorKey = a.key; anchorB64 = a.b64;
        } catch (e) { console.warn(`[anchor ${jobId}] failed (non-fatal): ${e.message}`); }
      }
      const result = await startAssembly({
        jobId, tenantId, userId, scenes,
        tier: b.tier || "hd", clipModel: b.clipModel || "veo3",
        visualMode: b.visualMode || "hybrid", whiteboardGenre: b.whiteboardGenre,
        captions: !!b.captions,
        voice: b.voice, imageModel: b.imageModel,
        ttsModel: b.ttsModel, language: b.language, genModel: b.genModel, aspectRatio: b.aspectRatio,
        captionFont: b.captionFont, anchorKey, anchorB64, heroStyle: b.heroStyle,
        brief: b.brief, visualStyle: b.visualStyle, style: b.style, culturalPalette: b.culturalPalette,   // for the NON-WB visual worker (workers.mjs) — style = gaya narasi, culturalPalette = Nusantara cues
      }, deps());
      res.json({ ok: true, status: "running", ...result });
    } catch (e) {
      // release BOTH slots (per-tenant + global) if acquired but the job never started (e.g. 402 credits)
      if (_slotTenant && _slotJob) { try { await conc.release(_slotTenant, _slotJob); await conc.releaseGlobal(_slotJob); } catch {} }
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
    res.set("Cache-Control", "no-store");   // status poll must never be cached (would freeze the UI on a stale "stitching")
    const meta = await store.getMeta(req.params.jobId);
    if (!meta) return res.status(404).json({ error: "job not found" });
    const scenesRaw = await store.getScenes(req.params.jobId, meta.sceneCount || 0);
    // STRIP planJson from the POLL response: it's the worker's render data (hero raster b64 +
    // maskShapes, up to ~2.5MB/scene for a recraft mask), and the UI only needs status/text. Returning
    // it for every scene OOM'd the API on long jobs (39 × ~2.5MB ≈ 98MB per poll @ 1.5s). Worker keeps it.
    const scenes = scenesRaw.map((s) => { if (!s) return s; const { planJson, ...rest } = s; return rest; });
    // hand the browser a playable URL for the finished MP4 (signed, short-lived)
    let mp4Url = null;
    if (meta.mp4Key && storage.isConfigured?.()) {
      try { mp4Url = await storage.signedUrl(meta.mp4Key, 3600); } catch { /* ignore */ }
    }
    // Honest queue UX (Change 3): if status="stitching" but the processor hasn't started
    // (renderStartedAt unset), the job is WAITING behind the 2-wide stitch queue → surface how many
    // renders are queued so the UI shows "Antre ≈N" instead of a frozen 4% bar. Best-effort + cheap.
    let renderQueued = null;
    if (meta.status === "stitching" && !meta.renderStartedAt) {
      try { renderQueued = await deps().queues?.stitch?.getWaitingCount?.(); } catch { /* ignore */ }
    }
    res.json({ ...meta, scenes, mp4Url, renderQueued });
  });
}
