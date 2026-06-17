/**
 * Cerita AI Studio — unified backend
 *
 * Direct routes  (Node @google/genai):
 *   Batch Images · TTS · Imagen
 *
 * Proxy routes  (→ Python FastAPI at PYTHON_API_URL):
 *   Chat · Image generation · Veo · Sora · Whisk · Flow storyboard
 *
 * Proxy routes  (→ MCP sidecar at MCP_API_URL):
 *   File search · context · list
 */
import express from "express";
import cors from "cors";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import multer from "multer";
import FormData from "form-data";
import { GoogleGenAI } from "@google/genai";
import Jimp from "jimp";
import { parseAudioMime, makeWavHeader, convertToWav, prependSilence, buildJsonl, mkId as _mkId } from "./utils.mjs";
import { getConfig, setConfig, getTtsProfiles, saveTtsProfiles, deleteTtsProfile, resolveTenantId, resolveUserId, _uuid5, getOrCreateSession, appendMessage, logUsage, calcGoogleCost,
         createJob, updateJobProgress, completeJob, failJob, getJob, listJobs, findJobByJobName, patchJobPayload, insertAsset, listAssets, logSyncJob, query } from "./db.js";
import { clerkMiddleware, requireAuth, getUserId } from "./auth.js";
import { Webhook } from "svix";
import { pool } from "./db.js";
import * as billing from "./billing.mjs";
import { setLiveJob, getLiveJob, updateLiveJob, pushLiveLog, delLiveJob } from "./redis.js";
import * as storage from "./storage.mjs";
import { isGoogleVeo, googleVeoSubmit, googleVeoStatus, googleVeoResult, getGoogleVeoJob, markGoogleVeoR2 } from "./googleVeo.mjs";
import { randomUUID } from "crypto";
import * as Sentry from "@sentry/node";
import { mountVideoRoutes } from "./video/routes.mjs";

// Step 3: Sentry is initialised in instrument.mjs (loaded via `node --import`)
// so it can auto-instrument http/express before this module runs.
const SENTRY_ON = !!process.env.SENTRY_DSN_NODE;

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT        = process.env.PORT        || 3000;
const PYTHON_API  = process.env.PYTHON_API_URL || "http://127.0.0.1:8000";
const MCP_API     = process.env.MCP_API_URL    || "http://127.0.0.1:8001";
const GEMINI_KEY  = process.env.GEMINI_API_KEY || "";

// ── Project Dalang (WS-7): language table is owned by the pakem (ONE source of
// truth, python/pakem). Node no longer hard-codes the canon; it lazily mirrors
// it from GET ${PYTHON_API}/narration/languages and refreshes in the background.
// The seed below is ONLY a cold-start fallback for the first few requests (or if
// the Python API is briefly unreachable) — it is overwritten by the pakem fetch.
// Mirrors _resolve_narasi_lang in laozhang_api.py, which itself delegates to pakem.
let _NARASI_LANG_NAMES = {
  id:"Bahasa Indonesia", en:"English", jv:"Basa Jawa (Javanese)",
  su:"Basa Sunda (Sundanese)", ms:"Bahasa Melayu (Malay)", ban:"Basa Bali (Balinese)",
  min:"Baso Minangkabau", ar:"العربية (Arabic)", zh:"中文 (Chinese)",
  ja:"日本語 (Japanese)", ko:"한국어 (Korean)", es:"Español (Spanish)",
  fr:"Français (French)", de:"Deutsch (German)", nl:"Nederlands (Dutch)",
  pt:"Português (Portuguese)", hi:"हिन्दी (Hindi)", th:"ภาษาไทย (Thai)",
  vi:"Tiếng Việt (Vietnamese)", tl:"Tagalog (Filipino)",
};
let _pakemLangFetchedAt = 0;
// Refresh the language mirror from the pakem (best-effort; never throws).
async function _refreshPakemLanguages(){
  if(Date.now()-_pakemLangFetchedAt < 5*60*1000) return; // 5-min TTL
  _pakemLangFetchedAt = Date.now();
  try{
    const r = await fetch(`${PYTHON_API}/narration/languages`);
    if(!r.ok) return;
    const d = await r.json();
    if(Array.isArray(d.languages) && d.languages.length){
      const next = {};
      for(const it of d.languages){ if(it && it.value) next[it.value] = it.label; }
      if(Object.keys(next).length) _NARASI_LANG_NAMES = next;
    }
  }catch(_e){ /* keep the existing mirror */ }
}
// Synchronous resolver over the (pakem-sourced) mirror. Kick a background
// refresh so the mirror trends toward the canon; resolution itself is sync so
// every existing call site keeps working unchanged.
const resolveLang = (lang) => {
  _refreshPakemLanguages();   // fire-and-forget
  if(!lang) return "Bahasa Indonesia";
  const k = String(lang).trim().toLowerCase();
  return _NARASI_LANG_NAMES[k] || String(lang).trim();
};

// ── Project Dalang (WS-7): the ONE prompt assembler. Ask the pakem
// (POST ${PYTHON_API}/narration/prompt) to build the cache-stable {system,user}
// narration messages from (style, language, mode, outline, brief, chapter,
// prev_tail, rag_passages). This replaces the inline style-rules + OUTPUT
// LANGUAGE strings that used to be duplicated here. Returns {system,user,meta}
// or null on failure so callers can fall back gracefully.
async function _pakemNarrationPrompt(payload){
  try{
    const r = await fetch(`${PYTHON_API}/narration/prompt`,{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload),
    });
    if(!r.ok){ console.warn(`[pakem] /narration/prompt HTTP ${r.status}`); return null; }
    const d = await r.json();
    if(!d || typeof d.user!=="string"){ return null; }
    return d; // {PAKEM_VERSION, system, user, meta}
  }catch(e){ console.warn("[pakem] /narration/prompt failed:",e.message); return null; }
}

// ── directories ──────────────────────────────────────────────────────────────
const DATA_DIR   = path.join(__dirname, "data");
const OUTPUT_DIR = path.join(__dirname, "output");
const TTS_DIR    = path.join(__dirname, "tts-output");
const IMG_DIR    = path.join(__dirname, "img-output");
const TMP_DIR    = path.join(__dirname, "tmp");
[DATA_DIR, OUTPUT_DIR, TTS_DIR, IMG_DIR, TMP_DIR].forEach(d => fs.mkdirSync(d, { recursive:true }));

const ai           = GEMINI_KEY ? new GoogleGenAI({ apiKey: GEMINI_KEY }) : null;
const upload       = multer({ storage: multer.memoryStorage(), limits: { fileSize: 20*1024*1024 }});

// ── helpers ───────────────────────────────────────────────────────────────────
// NOTE: job state (batch/tts/imagen) lives in Postgres (db.js) + Redis (redis.js);
//   the readJson/writeJson/activeJobs/mkJobId/*_JOBS_FILE helpers were removed in the
//   jobs-table migration. config/tts-profiles also moved to db.js (getConfig/etc).
const stateName = s       => (s && typeof s === "object" ? s.name : s) || "UNKNOWN";
const mkAI      = key     => new GoogleGenAI({ apiKey: key || GEMINI_KEY });

/** Generic proxy: forward request body + headers to Python, return JSON */
async function pyProxy(req, res, pyPath, extraHeaders = {}) {
  try {
    const headers = { "Content-Type": "application/json", ...extraHeaders };
    // forward per-request API key headers + the Clerk auth token (Authorization)
    for (const h of ["x-image-api-key","x-veo-api-key","x-sora-api-key","X-Image-API-Key","x-laozhang-api-key","X-LaoZhang-API-Key","x-deepseek-route","X-DeepSeek-Route","authorization"]) {
      if (req.headers[h.toLowerCase()] || req.headers[h]) {
        headers[h] = req.headers[h.toLowerCase()] || req.headers[h];
      }
    }
    const pyRes = await fetch(`${PYTHON_API}${pyPath}`, {
      method: req.method,
      headers,
      body: req.method !== "GET" ? JSON.stringify(req.body) : undefined,
    });
    if (!pyRes.ok) { const e = await pyRes.json().catch(() => ({error: pyRes.statusText})); return res.status(pyRes.status).json({error: e.error || e.detail || pyRes.statusText}); }
    res.json(await pyRes.json());
  } catch(e) { res.status(500).json({ error: e.message }); }
}

// ── Object storage: persist a generated file to R2 + record it in `assets` ──────
// Step 2. R2 is the source of truth; local disk stays a best-effort cache. Returns
// { key, id, signedUrl }. Falls back gracefully (logs, returns null) if R2 isn't
// configured yet, so generation never breaks on a storage hiccup.
//   assetType     ∈ image|audio|video|document|archive|other
//   sourceJobType ∈ batch_image|tts|imagen|veo|sora | null  (job_type_enum)
async function persistAsset({ tenantId, userId = null, jobId = null, assetType,
                              sourceJobType = null, filename, buffer,
                              contentType, metadata = {} }) {
  if (!storage.isConfigured()) {
    console.warn("[persistAsset] storage not configured — skipped", filename);
    return null;
  }
  const key = storage.buildKey(tenantId, jobId, assetType, filename);
  await storage.uploadBytes(key, buffer, contentType);
  const id = await insertAsset({
    tenantId, userId, jobId, bucket: storage.BUCKET_NAME, s3Key: key,
    originalFilename: filename, contentType, sizeBytes: buffer.length,
    assetType, sourceJobType, metadata,
  });
  return { key, id, signedUrl: await storage.signedUrl(key) };
}

// ── Signed-URL serving (Step 2.4) ──────────────────────────────────────────────
// Fresh 600s signed R2 URL for a key the tenant owns. RLS-checked: the SELECT
// only matches rows for the active tenant, so a user can never sign another
// tenant's asset. Returns null if not found / storage off.
async function signedUrlForKey(tenantId, key) {
  if (!key || !storage.isConfigured()) return null;
  const res = await query(
    "SELECT 1 FROM assets WHERE s3_key=$1 AND tenant_id=$2 AND is_deleted=false LIMIT 1",
    [key, tenantId], tenantId
  );
  if (!res.rows.length) return null;
  return storage.signedUrl(key, 600);
}
// Enrich a result_payload.files array with a fresh signedUrl per file that has a
// stored R2 key (added by persistAsset). Local `url` is kept as a fallback.
async function signFiles(files) {
  if (!Array.isArray(files) || !storage.isConfigured()) return files || [];
  return Promise.all(files.map(async (f) =>
    (f && f.key) ? { ...f, signedUrl: await storage.signedUrl(f.key, 600) } : f
  ));
}

// ── Usage logging for AI calls made directly in Node (anti revenue-leakage) ─────
// One usage_logs row per AI call. Proxied flows are logged Python-side (at the
// upstream call), so this only covers the google-native routes that hit the model
// from Node. Best-effort: never throws.
//   endpoint ∈ chat|image|tts|video|embedding|batch|other
// Per-image cost (USD), best-effort — mirrors _IMAGE_COSTS in laozhang_api.py.
const IMAGE_COSTS = {
  "imagen-4.0-ultra":0.06,"imagen-4.0-fast":0.02,"imagen-4.0":0.04,"imagen":0.04,
  "nano-banana-hd":0.039,"nano-banana":0.039,"gemini-2.0-flash":0.039,
  "gemini-2.5-flash":0.039,"flux-kontext-max":0.08,"flux-kontext":0.05,"flux":0.03,
  "seedream":0.03,"gpt-image-1":0.04,"dall-e-3":0.04,
};
function calcImageCost(model, count = 1) {
  const m = (model || "").toLowerCase();
  const k = Object.keys(IMAGE_COSTS).filter(x => m.startsWith(x)).sort((a,b)=>b.length-a.length)[0];
  const p = k ? IMAGE_COSTS[k] : 0.04;
  return +(p * Math.max(0, count)).toFixed(6);
}
async function trackUsage(req, model, endpoint, provider = "gemini", count = 1, jobType = null) {
  try {
    const tenantId = resolveTenantId(req);
    const userId   = await resolveUserId(req, tenantId);
    const jobId = jobType ? await logSyncJob(tenantId, jobType, { model, endpoint }) : null;
    const cost = endpoint === "image" ? calcImageCost(model, 1) : 0;
    for (let i = 0; i < Math.max(1, count); i++) {
      await logUsage(tenantId, userId, model || "unknown", endpoint, 0, 0, cost, null, provider, jobId);
    }
  } catch (e) { console.error("[trackUsage]", endpoint, e.message); }
}

function _sniffImage(buf) {
  if (buf.slice(0,8).toString("latin1").startsWith("\x89PNG")) return ["image/png","png"];
  if (buf[0] === 0xff && buf[1] === 0xd8) return ["image/jpeg","jpg"];
  if (buf.slice(0,4).toString("latin1")==="RIFF" && buf.slice(8,12).toString("latin1")==="WEBP") return ["image/webp","webp"];
  return ["image/png","png"];
}
// Synchronous image flows (google-native): ONE 'done' job + persist each image to
// R2/assets (durable + moat capture; were base64-only) + one usage row per image.
async function captureImageFlow(req, model, jobType, b64list, provider = "gemini", prompts = null) {
  try {
    const tenantId = resolveTenantId(req);
    const userId   = await resolveUserId(req, tenantId);
    const list = (b64list || []).filter(Boolean);
    const jobId = await logSyncJob(tenantId, jobType, { model, count: list.length });
    const cost = calcImageCost(model, 1);
    for (let i = 0; i < list.length; i++) {
      const buf = Buffer.from(list[i], "base64");
      const [ct, ext] = _sniffImage(buf);
      // Capture the generating prompt (string for all, or array aligned by index)
      // so source_prompt lands on the asset (moat) — was being discarded.
      const _p = Array.isArray(prompts) ? (prompts[i] || null) : (prompts || null);
      try {
        await persistAsset({ tenantId, userId, jobId, assetType: "image", sourceJobType: jobType,
          filename: `${jobType}_${i+1}.${ext}`, buffer: buf, contentType: ct,
          metadata: { model, ...(_p ? { prompt: _p } : {}) } });
      } catch (e) { console.error("[captureImageFlow] persist", e.message); }
      await logUsage(tenantId, userId, model || "unknown", "image", 0, 0, cost, null, provider, jobId);
    }
  } catch (e) { console.error("[captureImageFlow]", jobType, e.message); }
}

// ── WAV & batch helpers imported from utils.mjs ──

// ── Express ───────────────────────────────────────────────────────────────────
const app = express();
app.get("/health", (req, res) => res.sendStatus(200));
const googleCancelFlags=new Map(); // jobId -> true when cancelled
const ttsCancelFlags=new Map();   // jobId -> true when cancelled
app.use(cors());
// ── Clerk Webhook — POST /webhooks/clerk ──────────────────────────────────────
// MUST be before clerkMiddleware and requireAuth — webhooks are not user-authenticated.
// Uses express.raw() so Svix can verify the raw body signature.
app.post(
  "/webhooks/clerk",
  express.raw({ type: "application/json" }),
  async (req, res) => {
    const secret = process.env.CLERK_WEBHOOK_SECRET;
    if (!secret) {
      console.error("[webhook] CLERK_WEBHOOK_SECRET not set");
      return res.status(500).json({ error: "Webhook secret not configured" });
    }

    // Verify Svix signature
    let payload;
    try {
      const wh = new Webhook(secret);
      payload = wh.verify(req.body, {
        "svix-id":        req.headers["svix-id"],
        "svix-timestamp": req.headers["svix-timestamp"],
        "svix-signature": req.headers["svix-signature"],
      });
    } catch (err) {
      console.warn("[webhook] Invalid signature:", err.message);
      return res.status(400).json({ error: "Invalid webhook signature" });
    }

    const { type, data } = payload;
    console.log(`[webhook] event=${type} id=${data?.id || "?"}`);

    try {
      if (type === "user.created") {
        const email = data.email_addresses?.[0]?.email_address || "";
        const emailPrefix = email.split("@")[0] || data.id.slice(0, 12);
        const displayName = [data.first_name, data.last_name].filter(Boolean).join(" ") || emailPrefix;
        const slug = (emailPrefix + "-" + data.id.slice(-6)).toLowerCase().replace(/[^a-z0-9-]/g, "-");
        const now = new Date();
        const yearLater = new Date(now); yearLater.setFullYear(yearLater.getFullYear() + 1);

        // ── Provision tenant (idempotent, RLS-safe via SECURITY DEFINER fn) ──
        // tenant_id MUST be deterministic and match runtime resolveTenantId()
        // (uuid5 of "clerk-user-<id>") — otherwise webhook + runtime create
        // two separate tenants for the same user.
        const detTenantId = _uuid5(`clerk-user-${data.id}`);
        const provRes = await pool.query(
          `SELECT provision_tenant($1,$2,$3,$4,$5,$6,'admin') AS tenant_id`,
          [detTenantId, displayName || email.split("@")[0], slug, email, "free", data.id]
        );
        const tenantId = provRes.rows[0].tenant_id

        console.log(`[webhook] user.created → tenant=${tenantId} email=${email} display=${displayName}`);

      } else if (type === "user.updated") {
        const email = data.email_addresses?.[0]?.email_address || "";
        if (email) {
          await pool.query(
            `UPDATE users SET email=$1 WHERE external_id=$2`,
            [email, data.id]
          );
          console.log(`[webhook] user.updated → external_id=${data.id} email=${email}`);
        }

      } else if (type === "user.deleted") {
        await pool.query(
          `UPDATE users SET is_active=false WHERE external_id=$1`,
          [data.id]
        );
        console.log(`[webhook] user.deleted → external_id=${data.id} deactivated`);

      } else if (type === "organization.created") {
        const slug = data.slug || data.name.toLowerCase().replace(/[^a-z0-9]/g, "-");
        await pool.query(
          `SELECT provision_tenant($1,$2,$3,$4,$5,$6,'admin') AS tenant_id`,
          [data.id, data.name, slug, `org-${data.id}@clerk.placeholder`, "free", `org-${data.id}`]
        );
        console.log(`[webhook] organization.created → name=${data.name} slug=${slug}`);

      } else {
        console.log(`[webhook] unhandled event type: ${type}`);
      }
    } catch (dbErr) {
      console.error(`[webhook] DB error for ${type}:`, dbErr.message);
      return res.status(500).json({ error: "Database error" });
    }

    res.status(200).json({ ok: true, type });
  }
);

// ── Stripe Webhook — POST /webhooks/stripe ────────────────────────────────────
// MUST be before express.json — Stripe signature verification needs the RAW body.
// Idempotent (processed_stripe_events); credits the durable balance on success.
app.post(
  "/webhooks/stripe",
  express.raw({ type: "application/json" }),
  async (req, res) => {
    if (!billing.isConfigured() || !billing.WEBHOOK_SECRET) {
      console.error("[stripe] webhook hit but Stripe not configured");
      return res.status(500).json({ error: "stripe_not_configured" });
    }
    let event;
    try {
      event = billing.stripe.webhooks.constructEvent(
        req.body, req.headers["stripe-signature"], billing.WEBHOOK_SECRET);
    } catch (err) {
      console.warn("[stripe] invalid signature:", err.message);
      return res.status(400).json({ error: "invalid_signature" });
    }
    try {
      const r = await billing.handleStripeEvent(event);
      console.log(`[stripe] event=${event.type} id=${event.id} ->`, JSON.stringify(r));
      return res.status(200).json({ received: true, ...r });
    } catch (e) {
      console.error(`[stripe] handler error for ${event.type}:`, e.message);
      return res.status(500).json({ error: "handler_error" });
    }
  }
);

app.use(express.json({ limit:"200mb" })); // storyboard 26 scenes+images can be 5-20MB

app.use(clerkMiddleware()); // Clerk — must be after body-parser, before protected routes

// Step 3: per-request id (cross-service correlation) + Sentry request/tenant tags.
app.use((req, res, next) => {
  req.id = String(req.headers["x-request-id"] || "").trim() || randomUUID();
  res.setHeader("X-Request-Id", req.id);
  if (SENTRY_ON) {
    try {
      const scope = Sentry.getCurrentScope?.();
      if (scope) {
        scope.setTag("request_id", req.id);
        try { const t = resolveTenantId(req); if (t) scope.setTag("tenant_id", String(t)); } catch {}
      }
    } catch {}
  }
  next();
});

// ── Global API auth guard ───────────────────────────────────────────────────
// Every /api/* route requires a valid Clerk token EXCEPT the allowlist below and
// the /api/admin/* family, which is gated by the X-Admin-Secret header (adminGate)
// instead of Clerk — so the admin console (no Clerk session) can reach them.
const PUBLIC_API = new Set([
  "/api/health",
  // Project Dalang (WS-7): the pakem style + language catalogs are public,
  // non-sensitive reference data the UI fetches on mount to populate dropdowns.
  "/api/narration/styles",
  "/api/narration/languages",
]);
app.use("/api", (req, res, next) => {
  if (PUBLIC_API.has(req.path) || PUBLIC_API.has("/api" + req.path)) return next();
  // All /api/admin/* routes enforce X-Admin-Secret in-handler (adminGate); exempt
  // them from Clerk so they're reachable with the admin secret alone. req.path is
  // mount-relative here (the leading "/api" is stripped), so match "/admin/".
  if ((req.path || "").startsWith("/admin/")) return next();
  return requireAuth(req, res, next);
});

// ── Step 4: Billing (credits balance, Stripe checkout, customer portal) ───────
app.get("/api/billing/status", async (req, res) => {
  try {
    res.json(await billing.getBillingStatus(resolveTenantId(req)));
  } catch (e) { res.status(500).json({ error: e.message }); }
});
app.post("/api/billing/checkout", async (req, res) => {
  try {
    const tenantId = resolveTenantId(req);
    const userId = await resolveUserId(req, tenantId);
    const priceId = (req.body || {}).priceId;
    if (!priceId) return res.status(400).json({ error: "priceId required" });
    const url = await billing.createCheckoutSession({ tenantId, userId, priceId });
    res.json({ url });
  } catch (e) {
    const code = e.message === "stripe_not_configured" ? 503
               : e.message === "unknown_price" ? 400 : 500;
    res.status(code).json({ error: e.message });
  }
});
app.post("/api/billing/portal", async (req, res) => {
  try {
    const url = await billing.createPortalSession({ tenantId: resolveTenantId(req) });
    res.json({ url });
  } catch (e) {
    const code = e.message === "stripe_not_configured" ? 503
               : e.message === "no_customer" ? 400 : 500;
    res.status(code).json({ error: e.message });
  }
});
// ── Step 4: manual credit grant (no Stripe at bootstrap) ──────────────────────
// Runs in the live env, so creditTenant updates durable + Redis consistently.
// DISABLED until ADMIN_API_SECRET is set; then gate is the X-Admin-Secret header.
//   curl -X POST $URL/api/admin/grant -H "X-Admin-Secret: $SECRET" \
//        -H "Content-Type: application/json" \
//        -d '{"email":"user@example.com","credits":5000}'
const ADMIN_SECRET = process.env.ADMIN_API_SECRET || "";
app.post("/api/admin/grant", async (req, res) => {
  if (!ADMIN_SECRET) return res.status(503).json({ error: "admin grant disabled — set ADMIN_API_SECRET" });
  if ((req.headers["x-admin-secret"] || "") !== ADMIN_SECRET) return res.status(403).json({ error: "forbidden" });
  try {
    const b = req.body || {};
    const credits = parseInt(b.credits, 10);
    if (!credits) return res.status(400).json({ error: "credits required (non-zero integer)" });
    let tenantId = b.tenant_id || null;
    if (!tenantId && b.email) {
      const r = await pool.query("SELECT tenant_id_by_email($1) AS t", [b.email]);   // SECURITY DEFINER
      tenantId = r.rows[0]?.t || null;
    }
    if (!tenantId) return res.status(404).json({ error: "tenant not found (pass tenant_id or a known email)" });
    const reason = ["admin_adjust", "topup", "monthly_grant"].includes(b.reason) ? b.reason : "admin_adjust";
    const out = await billing.creditTenant(tenantId, credits, reason, b.op_id || null, { metadata: { source: "admin_api" } });
    res.json({ tenant_id: tenantId, credits_added: credits, applied: out.applied, balance: out.balance });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// ── Admin gate (shared by the cross-tenant admin endpoints below) ─────────────
// Same contract as /api/admin/grant: disabled (503) until ADMIN_API_SECRET is set,
// then requires a matching X-Admin-Secret header (403 otherwise). Returns true when
// authorised; on failure it has already written the response.
function adminGate(req, res) {
  if (!ADMIN_SECRET) { res.status(503).json({ error: "admin disabled — set ADMIN_API_SECRET" }); return false; }
  if ((req.headers["x-admin-secret"] || "") !== ADMIN_SECRET) { res.status(403).json({ error: "forbidden" }); return false; }
  return true;
}

// Edit a tenant's plan and/or active flag. Body: {tenant_id, plan?, is_active?}.
// Plan changes do NOT grant credits — use /api/admin/grant for that (kept independent).
const _ADMIN_PLANS = ["free", "starter", "pro", "enterprise"];
app.patch("/api/admin/tenant", async (req, res) => {
  if (!adminGate(req, res)) return;
  try {
    const b = req.body || {};
    const tenantId = b.tenant_id || null;
    if (!tenantId) return res.status(400).json({ error: "tenant_id required" });
    const plan = b.plan === undefined ? null : b.plan;
    if (plan !== null && !_ADMIN_PLANS.includes(plan))
      return res.status(400).json({ error: `plan must be one of ${_ADMIN_PLANS.join(", ")}` });
    const isActive = b.is_active === undefined ? null : !!b.is_active;
    if (plan === null && isActive === null)
      return res.status(400).json({ error: "nothing to update (pass plan and/or is_active)" });
    const r = await query(
      `UPDATE tenants SET plan=COALESCE($2,plan), is_active=COALESCE($3,is_active), updated_at=now()
        WHERE id=$1 RETURNING id, plan, is_active`,
      [tenantId, plan, isActive], tenantId);
    if (!r.rows.length) return res.status(404).json({ error: "tenant not found" });
    res.json({ ok: true, tenant: r.rows[0] });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// Cross-tenant asset delete (DB row + R2 object). Body/query: {tenant_id, key}.
// Mirrors the user DELETE /api/assets (hard delete), scoped to the given tenant.
app.delete("/api/admin/assets", async (req, res) => {
  if (!adminGate(req, res)) return;
  try {
    const tenantId = req.body?.tenant_id || req.query.tenant_id || null;
    const key = req.body?.key || req.query.key || null;
    if (!tenantId || !key) return res.status(400).json({ error: "tenant_id and key required" });
    const r = await query("DELETE FROM assets WHERE s3_key=$1 AND tenant_id=$2 RETURNING id", [key, tenantId], tenantId);
    if (!r.rows.length) return res.status(404).json({ error: "asset not found" });
    if (storage.isConfigured()) { try { await storage.del(key); } catch (e) { console.error("[admin delete asset] R2:", e.message); } }
    res.json({ ok: true });
  } catch (e) { console.error("[DELETE /api/admin/assets]", e.message); res.status(500).json({ error: e.message }); }
});

// Mark a feedback row handled (or un-handle). Body: {tenant_id, handled?=true}.
app.patch("/api/admin/feedback/:id", async (req, res) => {
  if (!adminGate(req, res)) return;
  try {
    const tenantId = req.body?.tenant_id || req.query.tenant_id || null;
    if (!tenantId) return res.status(400).json({ error: "tenant_id required" });
    const handled = req.body?.handled === undefined ? true : !!req.body.handled;
    const r = await query(
      `UPDATE feedback SET handled=$2, handled_at=CASE WHEN $2 THEN now() ELSE NULL END
        WHERE id=$1 RETURNING id, handled`,
      [req.params.id, handled], tenantId);
    if (!r.rows.length) return res.status(404).json({ error: "feedback not found" });
    res.json({ ok: true, feedback: r.rows[0] });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// Cross-tenant job cancel. Body: {tenant_id}. Durable DB status + best-effort signal
// to any active worker (live store + in-memory cancel flags). No balance writes —
// credit holds are released by the worker's own cancel path or expire via TTL.
app.post("/api/admin/jobs/:id/cancel", async (req, res) => {
  if (!adminGate(req, res)) return;
  try {
    const jobId = req.params.id;
    const tenantId = req.body?.tenant_id || req.query.tenant_id || null;
    if (!tenantId) return res.status(400).json({ error: "tenant_id required" });
    const live = await getLiveJob(jobId).catch(() => null);   // active worker?
    const newStatus = live ? "cancelling" : "cancelled";
    const r = await query(
      `UPDATE jobs SET status=$3::job_status_enum, updated_at=now()
        WHERE id=$1 AND tenant_id=$2 AND status IN ('queued','processing','running')
        RETURNING id, job_type, status`,
      [jobId, tenantId, newStatus], tenantId);
    if (!r.rows.length) return res.status(404).json({ error: "job not found or not cancellable" });
    if (live) {   // signal a same-process worker to stop promptly
      try { await updateLiveJob(jobId, (j) => { j.status = "cancelling"; return j; }); } catch {}
      googleCancelFlags.set(jobId, true);
      ttsCancelFlags.set(jobId, true);
      setTimeout(() => { googleCancelFlags.delete(jobId); ttsCancelFlags.delete(jobId); }, 60000);
    }
    res.json({ ok: true, job: r.rows[0] });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// ── In-app user feedback → feedback table (tenant + user captured from auth) ──
app.post("/api/feedback", async (req, res) => {
  try {
    const tenantId = resolveTenantId(req);
    const userId = await resolveUserId(req, tenantId);
    const body = String((req.body || {}).body || "").trim();
    if (!body) return res.status(400).json({ error: "feedback body required" });
    let userName = null, email = String((req.body || {}).email || "").trim() || null;
    try {
      if (userId) {
        const u = await query("SELECT display_name, email FROM users WHERE id=$1", [userId], tenantId);
        userName = u.rows[0]?.display_name || null;
        if (!email) email = u.rows[0]?.email || null;
      }
    } catch {}
    await query(
      "INSERT INTO feedback (tenant_id, user_id, user_name, email, body) VALUES ($1,$2,$3,$4,$5)",
      [tenantId, userId, userName, email, body.slice(0, 5000)], tenantId);
    res.json({ ok: true });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.use("/images", express.static(OUTPUT_DIR, {maxAge:"1h"}));
app.use("/audio",  express.static(TTS_DIR,    {maxAge:"1h"}));
app.use("/imgs",   express.static(IMG_DIR,    {maxAge:"1h"}));
// Root path serves the public marketing landing (ceritaAI). Registered before the
// static middleware so "/" returns landing.html instead of the default index.html.
// The studio app remains reachable at /index.html (where landing CTAs point).
app.get("/", (req, res) => {
  res.setHeader("Cache-Control", "no-cache");
  res.sendFile(path.join(__dirname, "public", "landing.html"));
});
app.use(express.static(path.join(__dirname,"public"),{
  setHeaders:(res,p)=>{ if(p.endsWith("index.html")) res.setHeader("Cache-Control","no-cache"); }
}));

// ── Health ────────────────────────────────────────────────────────────────────
app.get("/api/health", (_,res) => res.json({ ok:true, gemini:!!GEMINI_KEY, python:PYTHON_API }));
app.get("/api/config", requireAuth, async (req,res) => {
  try { res.json(await getConfig(resolveTenantId(req))); }
  catch(e) { res.status(500).json({ error: e.message }); }
});
app.post("/api/config", requireAuth, async (req,res) => {
  try {
    await setConfig(resolveTenantId(req), req.body || {});
    res.json({ ok: true });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// ── TTS Audio Profiles (persistent, server-side) ──────────────────────────────
app.get("/api/tts/profiles", requireAuth, async (req,res) => {
  try { res.json(await getTtsProfiles(resolveTenantId(req))); }
  catch(e) { res.status(500).json({ error: e.message }); }
});
app.post("/api/tts/profiles", requireAuth, async (req,res) => {
  try {
    const profiles = Array.isArray(req.body) ? req.body : [];
    await saveTtsProfiles(resolveTenantId(req), profiles);
    res.json({ ok: true, count: profiles.length });
  } catch(e) { res.status(500).json({ error: e.message }); }
});
app.delete("/api/tts/profiles/:id", requireAuth, async (req,res) => {
  try {
    const _tid = resolveTenantId(req);
    await deleteTtsProfile(_tid, req.params.id);
    const remaining = await getTtsProfiles(_tid);
    res.json({ ok: true, count: remaining.length });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// ══════════════════════════════════════════════════════════════════════════════
// CHAT  (proxy → Python)
// ══════════════════════════════════════════════════════════════════════════════
app.post("/api/chat", async (req,res) => {
  res.setHeader("Content-Type","text/event-stream");
  res.setHeader("Cache-Control","no-cache");
  res.setHeader("Connection","keep-alive");
  try {
    const lzk1 = req.headers["x-laozhang-api-key"] || "";
    const _auth1 = req.headers["authorization"] || "";
    const pyRes = await fetch(`${PYTHON_API}/chat/stream`, {
      method:"POST", headers:{"Content-Type":"application/json",...(lzk1&&{"X-LaoZhang-API-Key":lzk1}),...(_auth1&&{"Authorization":_auth1})},
      body: JSON.stringify({ session_id:req.body.sessionId, message:req.body.message,
        model:req.body.model||"gemini-2.5-flash", system:req.body.system||"You are a helpful assistant.",
        temperature:req.body.temperature||0.9, max_tokens:req.body.max_tokens||8192,
        images:Array.isArray(req.body.images)?req.body.images:[] }),
    });
    const reader = pyRes.body.getReader();
    req.on("close",()=>reader.cancel());
    while(true){ const {done,value}=await reader.read(); if(done)break; if(res.writableEnded)break; res.write(value); }
    if(!res.writableEnded) res.end();
  } catch(e){ if(!res.writableEnded){res.write(`data: [ERROR: ${e.message}]\n\n`);res.end();} }
});

// One-shot non-streaming chat — used by auto-pick video feature
app.post("/api/chat/once", async (req,res) => {
  try{
    const lzk=req.headers["x-laozhang-api-key"]||"";
    const _authO=req.headers["authorization"]||"";
    const pyRes=await fetch(`${PYTHON_API}/chat/once`,{
      method:"POST",headers:{"Content-Type":"application/json",...(lzk&&{"X-LaoZhang-API-Key":lzk}),...(_authO&&{"Authorization":_authO})},
      body:JSON.stringify({message:req.body.message,model:req.body.model||"gemini-2.5-flash",system:req.body.system||"You are a helpful assistant.",max_tokens:req.body.max_tokens||512}),
    });
    if(!pyRes.ok){const e=await pyRes.json().catch(()=>({error:pyRes.statusText}));return res.status(pyRes.status).json({error:e.error||e.detail});}
    res.json(await pyRes.json());
  }catch(e){res.status(500).json({error:e.message});}
});

// Non-streaming Google chat — for auto-pick video (Google mode)
app.post("/api/chat/google/once", async (req, res) => {
  try {
    const { message="", model="gemini-2.5-flash", system="", google_api_key="", max_tokens=12000 } = req.body || {};
    const effectiveKey = (google_api_key||"").trim() || GEMINI_KEY;
    if (!effectiveKey) return res.status(400).json({ error: "No Gemini API key" });
    const ai = mkAI(effectiveKey);
    const config = { temperature: 0.3, maxOutputTokens: max_tokens };
    if (system.trim()) config.systemInstruction = system;
    const r = await ai.models.generateContent({
      model, contents: [{ role: "user", parts: [{ text: message }] }], config
    });
    const text = r?.candidates?.[0]?.content?.parts?.map(p=>p.text||"").join("") || "";
    try{
      const _um=r?.usageMetadata||{}, _ti=_um.promptTokenCount||0, _to=_um.candidatesTokenCount||0;
      const _t=resolveTenantId(req), _u=await resolveUserId(req,_t);
      await logUsage(_t,_u,model,"chat",_ti,_to,calcGoogleCost(model,_ti,_to),null,"gemini");
    }catch(_){}
    res.json({ text: text.trim() });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

app.post("/api/chat/agentic", async (req,res) => {
  res.setHeader("Content-Type","text/event-stream");
  res.setHeader("Cache-Control","no-cache");
  res.setHeader("Connection","keep-alive");
  try {
    const lzk2 = req.headers["x-laozhang-api-key"] || "";
    const pyRes = await fetch(`${PYTHON_API}/chat/stream`, {
      method:"POST", headers:{"Content-Type":"application/json",...(lzk2&&{"X-LaoZhang-API-Key":lzk2}),...(req.headers["authorization"]&&{"Authorization":req.headers["authorization"]})},
      body: JSON.stringify({ session_id:req.body.sessionId, message:req.body.message,
        model:req.body.model||"claude-sonnet", system:req.body.system||"You are a helpful assistant. Use the search_files tool when the user asks about their documents.",
        temperature:req.body.temperature||0.7, max_tokens:8192, use_tools:true, mcp_paths:req.body.mcpPaths||"",
        images:Array.isArray(req.body.images)?req.body.images:[] }),
    });
    const reader = pyRes.body.getReader();
    req.on("close",()=>reader.cancel());
    while(true){ const {done,value}=await reader.read(); if(done)break; if(res.writableEnded)break; res.write(value); }
    if(!res.writableEnded) res.end();
  } catch(e){ if(!res.writableEnded){res.write(`data: [ERROR: ${e.message}]\n\n`);res.end();} }
});

app.post("/api/cancel/:id",  async(req,res)=>{ try{const _hc={...(req.headers["authorization"]&&{"Authorization":req.headers["authorization"]})};res.json(await(await fetch(`${PYTHON_API}/cancel/${req.params.id}`,{method:"POST",headers:_hc})).json());}catch(e){res.status(500).json({error:e.message});} });
app.get ("/api/history/:id", async(req,res)=>{ try{const _hh={...(req.headers["authorization"]&&{"Authorization":req.headers["authorization"]})};res.json(await(await fetch(`${PYTHON_API}/history/${req.params.id}`,{headers:_hh})).json());}catch(e){res.status(500).json({error:e.message});} });
app.post("/api/save",        async(req,res)=>{ try{const _hs={"Content-Type":"application/json",...(req.headers["authorization"]&&{"Authorization":req.headers["authorization"]})};res.json(await(await fetch(`${PYTHON_API}/save`,{method:"POST",headers:_hs,body:JSON.stringify(req.body)})).json());}catch(e){res.status(500).json({error:e.message});} });
app.delete("/api/session/:id",async(req,res)=>{ try{const _hd={...(req.headers["authorization"]&&{"Authorization":req.headers["authorization"]})};await fetch(`${PYTHON_API}/session/${req.params.id}`,{method:"DELETE",headers:_hd});res.json({status:"cleared"});}catch(e){res.status(500).json({error:e.message});} });
app.get("/api/models",       async(req,res)=>{ try{res.json(await(await fetch(`${PYTHON_API}/models`)).json());}catch(e){res.status(500).json({error:e.message});} });

// File upload for chat context
app.post("/api/upload", upload.single("file"), async(req,res)=>{
  if(!req.file) return res.status(400).json({error:"No file"});
  try{
    const form=new FormData();
    form.append("file",req.file.buffer,{filename:req.file.originalname,contentType:req.file.mimetype});
    const _uh={...form.getHeaders(),"Content-Length":String(form.getLengthSync()),
               ...(req.headers["authorization"]&&{"Authorization":req.headers["authorization"]})};
    const pyRes=await fetch(`${PYTHON_API}/upload`,{method:"POST",body:form.getBuffer(),headers:_uh});
    if(!pyRes.ok){const e=await pyRes.json();return res.status(pyRes.status).json(e);}
    res.json(await pyRes.json());
  }catch(e){res.status(500).json({error:e.message});}
});

// ══════════════════════════════════════════════════════════════════════════════
// IMAGE GENERATION  (proxy → Python)
// ══════════════════════════════════════════════════════════════════════════════
app.get ("/api/image-models",    (req,res)=>pyProxy(req,res,"/image-models"));
app.post("/api/generate-image",  (req,res)=>pyProxy(req,res,"/generate-image"));
app.post("/api/whisk",           (req,res)=>pyProxy(req,res,"/whisk"));

// ── Nano-banana model → Google native model name ────────────────────────────
const NANO_TO_GOOGLE_MODEL = {
  "nano-banana":        "gemini-2.5-flash-image",
  "nano-banana-hd":     "gemini-2.5-flash-image",
  "nano-banana-2":      "gemini-2.5-flash-image",
  "nano-banana-2-hd":   "gemini-2.5-flash-image",
  "nano-banana-pro":    "gemini-2.5-flash-image",
  "nano-banana-pro-hd": "gemini-2.5-flash-image",
};

// ── Flow images: Google native first, fallback LaoZhang on quota ─────────────
app.post("/api/flow/images/native", async (req, res) => {
  const { scenes=[], model="nano-banana-hd", aspect_ratio="16:9", image_style="", google_api_key="" } = req.body || {};
  if (!scenes.length) return res.status(400).json({ error:"scenes required" });

  const effectiveGoogleKey = google_api_key.trim() || GEMINI_KEY;
  const lzKey = req.headers["x-image-api-key"] || "";
  const nativeModel = NANO_TO_GOOGLE_MODEL[model];

  // ── Try Google native ───────────────────────────────────────────────────────
  if (effectiveGoogleKey && nativeModel) {
    try {
      const nativeAI = mkAI(effectiveGoogleKey);
      const styleSuffix = image_style ? ` ${image_style} style.` : "";

      // Direct fetch — more reliable than SDK for image-gen models
      const results = await Promise.allSettled(scenes.map(async (s, i) => {
        const prompt = `${s.description || s.title}. Camera: ${s.camera||""}.${styleSuffix} Cinematic still frame.`;
        const fetchResp = await fetch(
          `https://generativelanguage.googleapis.com/v1beta/models/${nativeModel}:generateContent?key=${effectiveGoogleKey}`,
          { method:"POST", headers:{"Content-Type":"application/json"},
            body: JSON.stringify({
              contents:[{role:"user",parts:[{text:prompt}]}],
              generationConfig:{responseModalities:["IMAGE","TEXT"]}
            })
          }
        );
        if (!fetchResp.ok) {
          const err = await fetchResp.text();
          throw new Error(`HTTP ${fetchResp.status}: ${err.slice(0,200)}`);
        }
        const data = await fetchResp.json();
        const parts = data?.candidates?.[0]?.content?.parts || [];
        const imgPart = parts.find(p => p.inlineData?.data);
        if (!imgPart) {
          const txt = parts.find(p=>p.text)?.text||"";
          console.log(`[flow/images/native] scene ${i} no image — text: ${txt.slice(0,100)}`);
        }
        return { index:i, image_b64: imgPart?.inlineData?.data || "" };
      }));

      const failed = results.filter(r => r.status==="rejected");
      if (failed.length) throw new Error(failed[0].reason?.message || String(failed[0].reason));
      const images = results.map((r,i) => r.status==="fulfilled" ? r.value : {index:i,image_b64:""});
      const okCount = images.filter(x=>x.image_b64).length;
      if (!okCount) throw new Error("Google native returned no images");
      console.log(`[flow/images/native] GOOGLE ok model=${nativeModel} scenes=${scenes.length} images=${okCount}`);
      await captureImageFlow(req, nativeModel, "flow_image", images.map(im=>im.image_b64), "gemini", (scenes||[]).map(s=>s.description||s.title||""));
      return res.json({ images, via:"google_native" });

    } catch(e) {
      console.log(`[flow/images/native] Google FAILED (→ fallback LaoZhang): ${e?.message||e}`);
      // any Google failure → fall through to LaoZhang
    }
  }

  // ── Fallback: LaoZhang ──────────────────────────────────────────────────────
  try {
    const r = await fetch(`${PYTHON_API}/flow/images`, {
      method:"POST",
      headers:{"Content-Type":"application/json","X-Image-API-Key": lzKey,
               ...(req.headers.authorization ? {authorization: req.headers.authorization} : {})},
      body: JSON.stringify({ scenes, model, aspect_ratio, image_style }),
    });
    const data = await r.json();
    console.log(`[flow/images/native] LAOZHANG fallback model=${model} scenes=${scenes.length}`);
    return res.json({ ...data, via:"laozhang_fallback" });
  } catch(e2) { res.status(500).json({ error: e2.message }); }
});

// FIX 3: dedicated handler with 10-min timeout (pyProxy has no timeout — browser drops connection on 26-scene runs)
app.post("/api/flow/storyboard", async (req, res) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 600_000); // 10 min
  try {
    const headers = { "Content-Type": "application/json" };
    for (const h of ["x-image-api-key","X-Image-API-Key","x-laozhang-api-key","X-LaoZhang-API-Key","authorization"]) {
      const v = req.headers[h.toLowerCase()] || req.headers[h];
      if (v) headers[h] = v;
    }
    const pyRes = await fetch(`${PYTHON_API}/flow/storyboard`, {
      method: "POST", headers, body: JSON.stringify(req.body), signal: controller.signal
    });
    clearTimeout(timer);
    if (!pyRes.ok) { const e = await pyRes.json().catch(()=>({error:pyRes.statusText})); return res.status(pyRes.status).json({error:e.error||e.detail||pyRes.statusText}); }
    res.json(await pyRes.json());
  } catch(e) { clearTimeout(timer); res.status(500).json({ error: e.message }); }
});

// ── Script → TTS transcript (LaoZhang) ───────────────────────────────────
app.post("/api/script/tts", (req,res)=>pyProxy(req,res,"/script/tts"));

// ── Narasi AI — LaoZhang (outline + brief + chapter) ─────────────────────
app.post("/api/narasi/outline",  (req,res)=>pyProxy(req,res,"/narasi/outline"));
app.post("/api/narasi/generate", (req,res)=>pyProxy(req,res,"/narasi/generate"));
// review requires auth (python Depends) + DeepSeek routing. pyProxy forwards
// Authorization + X-LaoZhang-API-Key + X-DeepSeek-Route; the old inline handler
// dropped Authorization → python 401 "Missing or invalid Authorization header".
app.post("/api/narasi/review", (req,res)=>pyProxy(req,res,"/narasi/review"));
// ── WS-8 (Project Dalang) — converged narration job contract ─────────────────
// ONE job endpoint shared by the Python UI and this Node path. The Node Google
// handler (/api/narasi/generate/google) keeps its own in-process loop, but new
// callers (and the unified UI) drive the SAME background job via these proxies:
//   POST   /api/narration            → 202 {job_id,status:"running",total}
//   GET    /api/narration/:id        → {status,done,total,chapters,output?}
//   POST   /api/narration/:id/cancel → {status:"cancel_requested"}
// pyProxy forwards Authorization (Clerk JWT) + the per-request key headers and
// preserves the Python status code (so a 402 credit-hold surfaces unchanged).
// Public pakem catalogs (allowlisted above) — MUST precede the `:id` route so
// Express matches these literals before treating "styles"/"languages" as a job id.
app.get ("/api/narration/styles",     (req,res)=>pyProxy(req,res,"/narration/styles"));
app.get ("/api/narration/languages",  (req,res)=>pyProxy(req,res,"/narration/languages"));
app.post("/api/narration",            (req,res)=>pyProxy(req,res,"/narration"));
app.get ("/api/narration/:id",        (req,res)=>pyProxy(req,res,`/narration/${req.params.id}`));
app.post("/api/narration/:id/cancel", (req,res)=>pyProxy(req,res,`/narration/${req.params.id}/cancel`));
// SAVE-EDIT  (proxy → Python) — captures correction pairs for the moat (WS-G Task 5)
app.post("/api/narasi/save-edit/:jobId", async(req,res)=>{
  try{
    const pyRes=await fetch(`${PYTHON_API}/narasi/save-edit/${req.params.jobId}`,{
      method:"POST",
      headers:{"Content-Type":"application/json",...(req.headers["authorization"]&&{"Authorization":req.headers["authorization"]})},
      body:JSON.stringify(req.body),
    });
    if(!pyRes.ok){const e=await pyRes.json().catch(()=>({error:pyRes.statusText}));return res.status(pyRes.status).json({error:e.error||e.detail||pyRes.statusText});}
    res.json(await pyRes.json());
  }catch(e){res.status(500).json({error:e.message});}
});

// ONE-SHOT FIX  (proxy → Python)
app.post("/api/narasi/oneshot-fix", async(req,res)=>{
  try{
    const lzk=req.headers["x-laozhang-api-key"]||"";
    const pyRes=await fetch(`${PYTHON_API}/narasi/oneshot-fix`,{
      method:"POST",
      headers:{"Content-Type":"application/json",...(lzk&&{"X-LaoZhang-API-Key":lzk}),...(req.headers["authorization"]&&{"Authorization":req.headers["authorization"]})},
      body:JSON.stringify(req.body),
    });
    if(!pyRes.ok){const e=await pyRes.json().catch(()=>({error:pyRes.statusText}));return res.status(pyRes.status).json({error:e.error||e.detail||pyRes.statusText});}
    res.json(await pyRes.json());
  }catch(e){res.status(500).json({error:e.message});}
});
app.get("/api/narasi/oneshot-fix/status/:jobId", async(req,res)=>{
  try{
    const pyRes=await fetch(`${PYTHON_API}/narasi/oneshot-fix/status/${req.params.jobId}`,{headers:{...(req.headers["authorization"]&&{"Authorization":req.headers["authorization"]})}});
    if(!pyRes.ok){const e=await pyRes.json().catch(()=>({error:pyRes.statusText}));return res.status(pyRes.status).json({error:e.error||e.detail||pyRes.statusText});}
    res.json(await pyRes.json());
  }catch(e){res.status(500).json({error:e.message});}
});
app.get("/api/narasi/oneshot-fix/result/:jobId", async(req,res)=>{
  try{
    const pyRes=await fetch(`${PYTHON_API}/narasi/oneshot-fix/result/${req.params.jobId}`,{headers:{...(req.headers["authorization"]&&{"Authorization":req.headers["authorization"]})}});
    if(!pyRes.ok){const e=await pyRes.json().catch(()=>({error:pyRes.statusText}));return res.status(pyRes.status).json({error:e.error||e.detail||pyRes.statusText});}
    res.json(await pyRes.json());
  }catch(e){res.status(500).json({error:e.message});}
});

app.post("/api/narasi/cancel/:jobId", (req,res)=>{
  googleCancelFlags.set(req.params.jobId, true);
  setTimeout(()=>googleCancelFlags.delete(req.params.jobId), 60000); // cleanup after 1min
  pyProxy(req,res,`/narasi/cancel/${req.params.jobId}`);
});
// PERSIST (proxy → Python) — durable write of generated chapters + capture cols
// (retrieved_ids/source_prompt). Used by the Google handler server-side and the
// Phase-1 E2E suite; tenant-scoped + auth-guarded by python Depends(get_current_user).
app.post("/api/narasi/persist", (req,res)=>pyProxy(req,res,"/narasi/persist"));
app.get("/api/narasi/jobs", (req,res)=>pyProxy(req,res,"/narasi/jobs"));
app.post("/api/narasi/rate", (req,res)=>pyProxy(req,res,"/narasi/rate"));
app.post("/api/narasi/rate-all", (req,res)=>pyProxy(req,res,"/narasi/rate-all"));
app.get("/api/narasi/chapters/:jobId", (req,res)=>pyProxy(req,res,`/narasi/chapters/${req.params.jobId}`));
app.get("/api/narasi/status/:jobId", (req,res)=>pyProxy(req,res,`/narasi/status/${req.params.jobId}`));
app.post("/api/narasi/stitch/:jobId", async(req,res)=>{
  try{
    const{jobId}=req.params;
    const body=req.body||{};
    const path=(await import("node:path"));
    const fsp=(await import("node:fs/promises"));
    // try node-side temp dir first, then proxy to python
    const nodeDir=path.join("/app/narasi_temp",jobId);
    let files=[];
    try{files=await fsp.readdir(nodeDir);}catch{}
    if(files.length){
      files.sort((a,b)=>{const n=s=>parseInt(s)||0;return n(a)-n(b);});
      const parts=await Promise.all(files.filter(f=>f.endsWith(".txt")).map(f=>fsp.readFile(path.join(nodeDir,f),"utf8")));
      const bodyText=parts.join("\n");
      const totalWords=bodyText.split(/\s+/).filter(Boolean).length;
      const lang=body.language||"id";
      const langLabel=resolveLang(lang);
      const markdown=`> **Gaya:** ${body.style||"storytelling"} | **Bahasa:** ${langLabel} | **${totalWords} kata**

---

${bodyText}`;
      return res.json({ok:true,markdown,total_words:totalWords});
    }
    // fallback to python stitch
    return pyProxy(req,res,`/narasi/stitch/${jobId}`);
  }catch(e){res.status(500).json({error:e.message});}
});

// ── Narasi AI — Google native ─────────────────────────────────────────────

// ── Project Dalang (WS-7): style-rule canon REMOVED from Node. ───────────────
// The ~180-line NARASI_STYLE_RULES_JS object that duplicated laozhang_api.py
// STYLE_RULES is deleted. Style rules (and OUTPUT LANGUAGE / factual integrity /
// ordering) now come from the pakem (python/pakem) via POST
// ${PYTHON_API}/narration/prompt — see _pakemNarrationPrompt + _narasiGoogleHandler.
// _getStyleRulesJS is kept ONLY as a deprecated no-op shim so any untraced caller
// degrades gracefully (returns ""); the real rules are injected by the pakem.
function _getStyleRulesJS(_style){ return ""; }

// ── Anti-drift helpers (mirrors laozhang_api.py) ───────────────────────────

function _extractVoiceFingerprint(text){
  if(!text) return "";
  const paras=text.split("\n\n").map(p=>p.trim()).filter(Boolean);
  return paras.slice(0,3).join("\n\n").slice(0,600);
}

function _detectDriftSignals(text, style=""){
  const signals=[];
  const tl=text.toLowerCase();
  const words=tl.split(/\s+/);
  const total=Math.max(words.length,1);

  // 1. Sentence-opener repetition
  const sentences=text.split(/[.!?]+/).map(s=>s.trim()).filter(Boolean);
  for(let j=0;j<sentences.length-2;j++){
    const starters=sentences.slice(j,j+3).map(s=>(s.split(/\s+/)[0]||"").toLowerCase());
    if(new Set(starters).size===1&&starters[0])
      { signals.push(`repeated sentence opener: "${starters[0]}"`); break; }
  }

  // 2. Abstract vocabulary overuse
  const abstractWords=new Set(["harapan","keberanian","semangat","perjuangan","perjalanan","makna","nilai",
    "warisan","identitas","peradaban","hope","courage","journey","legacy","meaning","spirit","heritage",
    "resilience","destiny","triumph","glory"]);
  const abstractHits=words.filter(w=>abstractWords.has(w.replace(/[.,!?;:]/g,""))).length;
  if(abstractHits/total>0.018)
    signals.push("abstract/generic vocabulary overuse (hope, courage, legacy, heritage, etc.)");

  // 3. Predictable opener
  const openerWords=text.trim().split(/\s+/);
  const opener=(openerWords[0]||"").toLowerCase();
  const opener2=(openerWords[1]||"").toLowerCase();
  if(["pada","di","inilah","demikianlah","itulah","thus","therefore","hence","the","in"].includes(opener))
    signals.push(`predictable/generic chapter opener: "${opener} ${opener2}"`);

  // 4. Conclusion fatigue
  const last250=text.slice(-250).toLowerCase();
  const fatigues=["inilah pelajaran","kita bisa belajar","kesimpulannya","pada akhirnya","demikianlah",
    "in conclusion","thus we see","as we have seen","it is clear that","history teaches us",
    "the lesson is","ultimately,","in the end,","to summarize","to conclude"];
  for(const ph of fatigues){ if(last250.includes(ph)){ signals.push(`generic conclusion phrase: "${ph}"`); break; } }

  // 5. Passive heroism
  const heroism=["tak gentar","penuh semangat","gagah berani","brave ancestors","fearless","spirited people",
    "our courageous","without fear","with great courage","selflessly"];
  for(const ph of heroism){ if(tl.includes(ph)){ signals.push(`passive heroism cliché: "${ph}"`); break; } }

  // ── Harari-specific ────────────────────────────────────────────────────────
  const isHarari=["harari","diamond","sapiens"].some(k=>style.toLowerCase().includes(k));
  if(isHarari){
    // 6. Banned generic transitions (rule 14)
    const banned=["throughout history","it is important to note","this suggests that","in many ways",
      "scholars have long debated","since the dawn of time","it is worth noting","one cannot help but wonder"];
    for(const bt of banned){ if(tl.includes(bt)){ signals.push(`banned generic transition (Harari rule 14): "${bt}"`); break; } }

    // 7. Hedging overuse (rule 18)
    const hedges=["perhaps","possibly","arguably","it could be argued","some might say","may have","might have","could have been"];
    const hedgeCount=hedges.reduce((n,h)=>n+(tl.split(h).length-1),0);
    if(hedgeCount>Math.max(3,Math.floor(total/120)))
      signals.push(`excessive hedging (Harari rule 18): ${hedgeCount} instances of perhaps/possibly/arguably/may have`);

    // 8. Missing punchline (rule 8)
    const sentLengths=sentences.map(s=>s.split(/\s+/).length);
    const hasPunchline=sentLengths.some((l,j)=>j<sentLengths.length-1&&l>25&&sentLengths[j+1]<=7);
    if(!hasPunchline&&sentences.length>4)
      signals.push("missing Punchline Rule (Harari rule 8): no short punchy sentence after a long complex one");

    // 9. "Imagine" as opener
    if(text.trim().toLowerCase().startsWith("imagine"))
      signals.push('ABSOLUTE FORBIDDEN: chapter starts with "Imagine" — banned after chapter 1');

    // 10. Comfortable conclusion
    const lastPara=(text.trim().split("\n\n").pop()||"").toLowerCase();
    const comforts=["shows us the way","gives us hope","can inspire","we are not so different",
      "reminds us that","proves that humanity","testament to","triumph of the human","never give up","enduring legacy"];
    for(const cp of comforts){ if(lastPara.includes(cp)){ signals.push(`comfortable/unearned conclusion: "${cp}"`); break; } }

    // 11. Missing sensory anchor (rule 7)
    const sensory=["smell","taste","sound","touch","felt","heard","saw","cold","hot","bitter","wet","dry",
      "rough","smooth","silence","noise","weight","aroma","texture","freezing","burning","rain","dust","mud",
      "dark","light","shadow","heat","sweat","blood","breath","wind","fog"];
    if(!sensory.some(w=>tl.includes(w))&&words.length>200)
      signals.push("missing Sensory Anchor (Harari rule 7): no sensory/physical experience translated from data");

    // 12. Missing quantified data
    if(!/\b\d[\d,.]*\s*(%|km|meters?|years?|BCE|CE|AD|centuries|percent|million|billion|thousand)\b/i.test(text)
      &&!/\b(1[0-9]{2,}|[2-9][0-9]+)\b/.test(text)&&words.length>150)
      signals.push("missing quantified data point (Harari required): no numbers, dates, or measurements");
  }

  // Deduplicate
  return [...new Set(signals)];
}

function _buildAntidriftBlock(chapIndex, totalChapters, voiceSample, driftSignals, prevOpeners, style=""){
  if(chapIndex===0) return "";
  const ratio=chapIndex/Math.max(totalChapters,1);
  const isHarari=["harari","diamond","sapiens"].some(k=>style.toLowerCase().includes(k));

  let phase,urgency;
  if(ratio<0.35){      phase="EARLY-MIDDLE"; urgency="Monitor carefully:"; }
  else if(ratio<0.65){ phase="MIDDLE";       urgency="⚠ DRIFT WARNING — MIDDLE CHAPTERS:"; }
  else{                phase="LATE";         urgency="🚨 CRITICAL — LATE CHAPTER DRIFT PREVENTION:"; }

  const L=["","=".repeat(60)];
  L.push(`ANTI-DRIFT PROTOCOL — Chapter ${chapIndex+1}/${totalChapters} (${phase})`);
  L.push("=".repeat(60));

  if(voiceSample){
    L.push("\nVOICE ANCHOR — This is the exact voice established in Chapter 1.");
    L.push("Match this register, rhythm, and specificity level precisely:");
    L.push(`"""\n${voiceSample}\n"""`);
    L.push("Your chapter must feel written by the same person.");
  }

  L.push(`\n${urgency}`);
  if(phase==="EARLY-MIDDLE"){
    L.push("- Vary sentence rhythm — do NOT repeat the opener pattern from the previous chapter");
    L.push("- Keep specificity: names, dates, measurements — no vague references without antecedent");
    L.push("- Punchlines must land as hard as in Chapter 1, not softer");
  } else if(phase==="MIDDLE"){
    L.push("- DANGER ZONE for drift. Actively fight these tendencies:");
    L.push("  • Repeated sentence rhythms (you will default to them — override)");
    L.push("  • Softened implications (you will hedge more — don't)");
    L.push("  • 'Safe intellectualism' — comfortable ideas that don't surprise anyone");
    L.push("  • Weakened punchlines — the last sentence must be as sharp as Chapter 1");
    L.push("- Reread the Voice Anchor above. If your draft sounds softer, rewrite the opening.");
  } else {
    L.push("- LATE CHAPTERS are where quality collapses. Fight every instinct to:");
    L.push("  • Summarize what was already said (DO NOT recap previous chapters)");
    L.push("  • Use fatigue phrases: 'ultimately', 'in the end', 'as we have seen', 'in conclusion'");
    L.push("  • Write generic conclusions — end with a SPECIFIC image or unresolved tension");
    L.push("  • Lose specificity — this chapter must have MORE named facts than Chapter 1, not fewer");
    L.push("- The final sentence must be quotable. Test it: would someone screenshot it?");
  }

  if(isHarari){
    L.push("\nHARARI MECHANICS CHECKLIST — verify before finishing this chapter:");
    if(phase==="EARLY-MIDDLE"){
      L.push("  ✓ Micro-to-Macro zoom (rule 5): anchor in one microscopic detail before going cosmic");
      L.push("  ✓ Sensory Anchor (rule 7): translate one data point into a lived prehistoric experience");
      L.push("  ✓ Punchline Rule (rule 8): follow one long academic sentence with a brutal 3–6 word verdict");
    } else if(phase==="MIDDLE"){
      L.push("  ✓ Shared Fiction frame (rule 6): frame human institutions as 'imagined realities'");
      L.push("  ✓ Historical Contingency (rule 9): deny the reader the comfort of destiny");
      L.push("  ✓ Cognitive Threat (rule 19 equivalent): one realization that destabilizes a modern assumption");
      L.push("  ✓ Anti-hedging: remove 'perhaps/possibly/arguably' unless evidence truly requires it");
      L.push("  ✓ NO banned transitions: 'Throughout history', 'It is important to note', 'In many ways'");
    } else {
      L.push("  ✓ Deep Time Collapse: connect a human moment to consequences over centuries/millennia");
      L.push("  ✓ Unsettling Question: end NOT with resolution but a disturbing reframe or open paradox");
      L.push("  ✓ Civilizational Humility: remind reader modern civilization is not the final form");
      L.push("  ✓ Human Cost: the civilizational shift must carry visible human loss");
      L.push("  ✓ Thesis Pressure: one claim strong enough a serious scholar could disagree with");
      L.push("  ✓ Chapter bridge: final 1–2 sentences opening next tension — not a summary");
      L.push("  ✗ FORBIDDEN: 'Imagine' as opener after chapter 1");
      L.push("  ✗ FORBIDDEN: 'This is the great lesson for all of humanity'");
    }
  }

  if(driftSignals.length){
    L.push("\nDETECTED DRIFT IN PREVIOUS CHAPTERS — do NOT repeat these:");
    driftSignals.slice(-6).forEach(s=>L.push(`  ✗ ${s}`));
  }

  if(prevOpeners.length>=2){
    L.push("\nPREVIOUS CHAPTER OPENERS — your first sentence must NOT start with:");
    prevOpeners.slice(-4).forEach(op=>L.push(`  ✗ '${op}'`));
  }

  L.push("\nREMINDER: You are still writing a LIVING narrative. This chapter should feel like the BEST one yet.");
  L.push("=".repeat(60)+"\n");
  return L.join("\n");
}

const _narasiGoogleHandler=async(req,res)=>{
  try{
    const body=req.body||{};
    const apiKey=body.google_api_key||process.env.GEMINI_API_KEY;
    if(!apiKey)return res.status(400).json({error:"No Google API key"});
    const {GoogleGenAI}=await import("@google/genai");
    const ai=new GoogleGenAI({apiKey});
    // Google API only supports Gemini models — guard against Claude/GPT being passed
    let model=body.model||"gemini-2.5-flash";
    if(!model.startsWith("gemini"))model="gemini-2.5-flash";
    const action=body.action||"outline";

    let prompt="";
    const chapCount=parseInt(body.chap_count)||10;
    let maxTok=Math.max(6000, chapCount*600+2000);
    const lang=body.language||"id";
    const style=body.style||"storytelling";
    const langLabel=resolveLang(lang);
    console.log(`[narasi-google] action=${action} raw_language=${JSON.stringify(lang)} -> langLabel=${JSON.stringify(langLabel)}`);

    if(action==="brief"){
      prompt=`You are writing a ${style} narrative titled: "${body.topic}"\nLanguage: ${langLabel}\n\nOutline:\n${body.outline||""}\n\nWrite a concise NARRATIVE BRIEF (max 300 words) covering: overall tone, voice, emotional arc, key themes, how chapters connect, recurring motifs.\nWrite in ${langLabel}. Return ONLY the brief text, no headings, no markdown.`;
      maxTok=1000;
    } else if(action==="chapter"){
      const c=body.chapter||{};
      // RAG: fetch Gutenberg passages from python-api
      let ragText="";
      if(body.use_rag){
        try{
          const ragResp=await fetch(`${PYTHON_API}/rag/context`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({topic:`${body.topic} — ${c.title}`,style,top_k:5})});
          const ragData=await ragResp.json();
          if(ragData.ok&&ragData.context_text){ragText=ragData.context_text;console.log(`[RAG] Google path: passages=${ragData.passages}`);}
        }catch(e){console.warn("[RAG] failed:",e.message);}
      }
      // Project Dalang (WS-7): style rules + OUTPUT LANGUAGE + ordering come from
      // the pakem assembler, not inline strings. Forward {system,user} to Google.
      const _pk=await _pakemNarrationPrompt({
        style, language:lang, mode:(body.video_mode?"video":"text"),
        outline:body.outline||"", brief:body.brief||"",
        chapter:{id:c.id,title:c.title,summary:c.description||"",
                 word_target:body.word_target,word_min:body.word_min,word_max:body.word_max},
        rag_passages:ragText||null, model,
      });
      if(_pk){
        prompt=(_pk.system?_pk.system+"\n\n":"")+_pk.user;
      } else {
        // Fallback: pakem unreachable — minimal inline prompt (no style canon).
        prompt=`OUTPUT LANGUAGE: ${langLabel}. Write the ENTIRE chapter ONLY in ${langLabel}.\n\n`
          +(ragText?ragText+"\n":"")
          +(body.brief?`NARRATIVE BRIEF:\n${body.brief}\n\n`:"")
          +(body.outline?`FULL OUTLINE:\n${body.outline}\n\n`:"")
          +`THIS CHAPTER:\n  Title: ${c.title}\n  Summary: ${c.description}\n  Target: ${body.word_target} words.\n\nWrite EXACTLY ${body.word_target} words in ${langLabel}. Do NOT include chapter title. Return ONLY the body text.`;
      }
      maxTok=Math.max(2000,Math.ceil((body.word_max||500)*1.5*1.2));

    } else if(body.chapters&&Array.isArray(body.chapters)&&body.chapters.length){
      // ── Multi-chapter sequential generate with anti-drift ──────────────────
      const{randomUUID}=await import("node:crypto");
      const fsp=(await import("node:fs/promises"));
      const nodePath=(await import("node:path"));
      const jobId=(body.pre_job_id||randomUUID().slice(0,8));
      const tmpDir=nodePath.join("/app/narasi_temp",jobId);
      await fsp.mkdir(tmpDir,{recursive:true});
      const chapters=body.chapters;
      const errors=[];
      const persistChapters=[];   // Step 1.2: collect for Postgres persist

      // Anti-drift state
      let voiceSample="";
      let driftSignals=[];
      let prevOpeners=[];
      const totalChapters=chapters.length;

      for(let i=0;i<chapters.length;i++){
        const c=chapters[i];
        const wt=c.words||400;
        const wmin=Math.floor(wt*0.9),wmax=Math.ceil(wt*1.1);

        // Anti-drift is a Node-side continuity construct (voice anchoring + drift
        // signals across chapters). It is NOT part of the pakem canon, so it stays
        // here and is APPENDED to the pakem-assembled prompt below.
        const antidrift=_buildAntidriftBlock(i,totalChapters,voiceSample,driftSignals,prevOpeners,style);

        // Check cancel flag before each chapter
        if(googleCancelFlags.get(jobId)){
          console.warn(`[narasi-google] job ${jobId} cancelled at bab ${c.id}`);
          break;
        }
        // RAG: fetch Gutenberg passages for this chapter (we still fetch here so we
        // capture passage_ids for the persist step; the text is handed to the pakem
        // assembler as rag_passages instead of being inlined).
        let chRagText="";
        let chPassageIds=[];
        if(body.use_rag){
          try{
            const rr=await fetch(`${PYTHON_API}/rag/context`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({topic:`${body.topic||""} — ${c.title}`,style,top_k:5})});
            const rd=await rr.json();
            if(rd.ok&&rd.context_text){chRagText=rd.context_text;chPassageIds=rd.passage_ids||[];console.log(`[RAG] Google multi-chapter bab=${c.id} passages=${rd.passages}`);}
          }catch(e){console.warn("[RAG] multi-chapter failed:",e.message);}
        }

        // ── Project Dalang (WS-7): assemble via the pakem (ONE source of truth).
        // Style rules, OUTPUT LANGUAGE, factual integrity, brief & outline ordering
        // all come from /narration/prompt. Anti-drift is appended afterward.
        let cp;
        const _pk=await _pakemNarrationPrompt({
          style, language:lang, mode:(body.video_mode?"video":"text"),
          outline:body.outline||"", brief:body.brief||"",
          chapter:{id:c.id,title:c.title,summary:c.description||"",
                   index:i,total:totalChapters,
                   word_target:wt,word_min:wmin,word_max:wmax},
          rag_passages:chRagText||null, job_id:jobId, model,
        });
        if(_pk){
          cp=(_pk.system?_pk.system+"\n\n":"")
            +(antidrift?antidrift+"\n":"")
            +_pk.user;
        } else {
          // Fallback: pakem unreachable — minimal inline prompt (no style canon).
          cp=`OUTPUT LANGUAGE: ${langLabel}. Write the ENTIRE chapter ONLY in ${langLabel}; references/context may be in another language but do NOT mirror them.

You are writing Chapter ${c.id} of a ${style} narrative titled: "${body.topic||""}"
Language: ${langLabel}

${chRagText?chRagText+"\n":""}${antidrift}${body.brief?`NARRATIVE BRIEF:\n${body.brief}\n\n`:""}${body.outline?`FULL OUTLINE:\n${body.outline}\n\n`:""}THIS CHAPTER:
  Title: ${c.title}
  Summary: ${c.description||""}
  Target: ${wt} words (range: ${wmin}–${wmax})

Write EXACTLY ${wt} words (count carefully) in ${langLabel}. Do NOT include chapter title. Return ONLY body text.`;
        }

        // Token budget scales with THIS chapter's word target (wt). ~2 tokens
        // per word for Latin-script + generous headroom for non-English scripts
        // (Javanese/Arabic/CJK use more tokens/word). Ceiling 24000 covers even
        // an ~800-word chapter without the 65536 slowdown.
        const chapMaxTok=Math.min(24000,Math.max(1024,Math.ceil(wt*4)+512));
        // thinkingConfig only valid on Gemini 2.5; budget scales with length.
        const genCfg={maxOutputTokens:chapMaxTok};
        if(/gemini-2\.5/.test(model)){
          genCfg.thinkingConfig={thinkingBudget:Math.min(4096,Math.ceil(wt*1.5))};
        }
        try{
          const r2=await ai.models.generateContent({
            model,
            contents:[{role:"user",parts:[{text:cp}]}],
            config:genCfg,
          });
          let ct=(r2.text||"").trim();
          let _genResp=r2;
          let finishReason=r2.candidates?.[0]?.finishReason||"unknown";
          // Retry if empty or too short
          if(!ct||ct.split(/\s+/).filter(Boolean).length<50){
            console.warn(`[narasi-google] bab ${c.id} EMPTY/SHORT (${ct.split(/\s+/).filter(Boolean).length} words) -- retrying`);
            const r3=await ai.models.generateContent({
              model,contents:[{role:"user",parts:[{text:cp}]}],
              config:genCfg,
            });
            if(r3.text&&r3.text.trim()){
              ct=r3.text.trim();
              _genResp=r3;
              finishReason=r3.candidates?.[0]?.finishReason||"unknown";
            }
          }
          const wordCount=ct.split(/\s+/).filter(Boolean).length;
          console.warn(`[narasi-google] bab ${c.id} finish_reason=${finishReason} words=${wordCount} model=${model}`);

          // Update anti-drift state
          if(i===0){
            voiceSample=_extractVoiceFingerprint(ct);
          } else {
            const newSigs=_detectDriftSignals(ct,style);
            if(newSigs.length){
              driftSignals=[...driftSignals,...newSigs];
              // deduplicate keeping last-seen, cap at 10
              const seen=new Set();
              driftSignals=[...driftSignals].reverse().filter(s=>seen.has(s)?false:seen.add(s)).reverse().slice(-10);
              console.warn(`[narasi-google] drift signals bab ${c.id}:`,newSigs);
            }
          }
          if(ct){
            const fw=(ct.trim().split(/\s+/)[0]||"").toLowerCase().replace(/[.,!?;:]/g,"");
            prevOpeners.push(fw);
          }

          await fsp.writeFile(
            nodePath.join(tmpDir,`${c.id}.txt`),
            `## Bab ${c.id}: ${c.title}\n\n${ct}\n`,
            "utf8"
          );
          persistChapters.push({
            index:i, id:c.id, title:c.title, content:ct,
            source_prompt:cp, retrieved_ids:chPassageIds, word_count:wordCount,
            model, tokens_in:_genResp?.usageMetadata?.promptTokenCount||0,
            tokens_out:_genResp?.usageMetadata?.candidatesTokenCount||0,
            rag_used:!!(body.use_rag&&chPassageIds.length),
          });
        }catch(e){
          errors.push({id:c.id,error:e.message});
          await fsp.writeFile(
            nodePath.join(tmpDir,`${c.id}.txt`),
            `## Bab ${c.id}: ${c.title}\n\n<!-- ERROR bab ${c.id}: ${e.message} -->\n`,
            "utf8"
          );
        }
      }
      // Step 4: charge the google-path narasi generation (sum per-chapter tokens)
      try{
        const _t=resolveTenantId(req), _u=await resolveUserId(req,_t);
        const _tin=persistChapters.reduce((s,c)=>s+(c.tokens_in||0),0);
        const _tout=persistChapters.reduce((s,c)=>s+(c.tokens_out||0),0);
        if(_tin||_tout) await logUsage(_t,_u,model,"narasi",_tin,_tout,calcGoogleCost(model,_tin,_tout),null,"gemini");
      }catch(e){ console.warn("[narasi-google] usage charge failed:",e.message); }
      // Step 1.2: persist to Postgres via Python (database.py = source of truth)
      try{
        const _pr=await fetch(`${PYTHON_API}/narasi/persist`,{
          method:"POST",
          headers:{"Content-Type":"application/json",
                   ...(req.headers["authorization"]&&{"Authorization":req.headers["authorization"]})},
          body:JSON.stringify({job_id:jobId, topic:body.topic||"", style, chapters:persistChapters}),
        });
        if(!_pr.ok) console.warn(`[narasi-google] persist HTTP ${_pr.status} — chapters NOT saved (auth/tenant?)`);
        else console.log(`[narasi-google] persisted ${persistChapters.length} chapters -> DB`);
      }catch(e){console.warn("[narasi-google] persist failed (non-fatal):",e.message);}

      return res.json({ok:true,job_id:jobId,errors,drift_signals_detected:driftSignals});

    } else {
      // ── Outline generation — plain text format (avoids JSON quote issues) ──
      const revise=body.revise_instruction&&body.current_outline;
      const outlineFmt=`Return ONLY a plain-text list, one chapter per line, using this EXACT format with pipe separators:\nID|TITLE|WORDS|DESCRIPTION\n\nExample:\n1|Prolog: Bayangan Raksasa|400|Memperkenalkan lanskap Jawa Tengah pada abad kedelapan dan surplus pertanian yang memungkinkan berdirinya peradaban kompleks.\n2|Geografi sebagai Takdir|500|Analisis bagaimana abu vulkanik Merapi menciptakan tanah subur yang menjadi fondasi ekonomi surplus.\n\nRules:\n- NO pipes inside TITLE or DESCRIPTION fields — rephrase if needed\n- WORDS must be integers only\n- Total words must sum to ${body.word_min}–${body.word_max}\n- Deeper/climactic chapters get MORE words; intro/epilog get FEWER\n- NO markdown, NO JSON, NO fences, NO extra lines`;
      if(revise){
        prompt=`OUTPUT LANGUAGE: ${langLabel}. ALL titles and descriptions MUST be in ${langLabel} (the example below may be in another language — do NOT copy its language).\n\nRevise this narrative outline for: "${body.topic}"\nStyle: ${style} | Language: ${langLabel}\n\nCURRENT OUTLINE:\n${body.current_outline}\n\nREVISION INSTRUCTION: ${body.revise_instruction}\n\n${outlineFmt}`;
      } else {
        prompt=`OUTPUT LANGUAGE: ${langLabel}. ALL chapter titles and descriptions MUST be written in ${langLabel} (the format example below may be in another language — do NOT copy its language).\n\nCreate a narrative outline for a ${style} narrative titled: "${body.topic}"\nLanguage: ${langLabel} | Chapters: EXACTLY ${chapCount} (no more, no less)\n\nCRITICAL: Return EXACTLY ${chapCount} pipe-delimited line(s). Do NOT split into extra Pembuka/Isi/Penutup entries beyond the ${chapCount} total.\n\nWORD WEIGHTS: Do NOT divide equally. Climactic chapters get MORE words. Intro/conclusion get FEWER. Total must sum to ${body.word_min}–${body.word_max}.\n\n${outlineFmt}`;
      }
    }

    const result=await ai.models.generateContent({model,contents:[{role:"user",parts:[{text:prompt}]}],config:{maxOutputTokens:maxTok}});
    // Step 4: charge the google-path outline/brief/chapter call
    try{
      const _um=result?.usageMetadata||{}, _tin=_um.promptTokenCount||0, _tout=_um.candidatesTokenCount||0;
      const _t=resolveTenantId(req), _u=await resolveUserId(req,_t);
      await logUsage(_t,_u,model,"narasi",_tin,_tout,calcGoogleCost(model,_tin,_tout),null,"gemini");
    }catch(e){ console.warn("[narasi-google] outline usage failed:",e.message); }
    let text=(result.text||"").trim().replace(/^```(?:json)?\s*/mg,"").replace(/\n?```\s*$/mg,"").trim();

    if(action==="brief") return res.json({ok:true,brief:text});
    if(action==="chapter") return res.json({ok:true,text});

    // ── Parse pipe-delimited outline format ──────────────────────────────────
    const lines=text.split("\n").map(l=>l.trim()).filter(l=>l&&/^\d+\|/.test(l));
    if(lines.length>=1){
      const chapters=lines.map(l=>{
        const parts=l.split("|");
        return{id:parts[0]?.trim()||"",title:parts[1]?.trim()||"",words:parseInt(parts[2])||400,description:parts.slice(3).join("|").trim()};
      }).filter(c=>c.id&&c.title);
      // Truncate to requested chapter count if AI returned more
      if(chapters.length > chapCount) chapters.splice(chapCount);
      if(chapters.length){
        // Enforce word count
        const wMin=parseInt(body.word_min)||290;
        const wMax=parseInt(body.word_max)||300;
        const total=chapters.reduce((s,c)=>s+c.words,0);
        if(total<wMin||total>wMax){
          const ratio=wMin/Math.max(total,1);
          chapters.forEach(c=>c.words=Math.max(50,Math.round(c.words*ratio)));
          const diff=wMin-chapters.reduce((s,c)=>s+c.words,0);
          if(diff)chapters.reduce((a,b)=>a.words>b.words?a:b).words+=diff;
        }
        const outlineText=chapters.map(c=>`## ${c.id}. ${c.title}\n*${c.words} kata*\n${c.description}`).join("\n\n");
        // Persist outline to narasi_outlines via Python (mirror LaoZhang's inline save_outline)
        try{
          const _opr=await fetch(`${PYTHON_API}/narasi/outline/persist`,{
            method:"POST",
            headers:{"Content-Type":"application/json",
                     ...(req.headers["authorization"]&&{"Authorization":req.headers["authorization"]})},
            body:JSON.stringify({topic:body.topic||"",style,language:lang,chap_count:chapters.length,outline_text:outlineText,chapters,model}),
          });
          if(!_opr.ok) console.warn(`[narasi-google] outline persist HTTP ${_opr.status}`);
          else console.log(`[narasi-google] outline persisted -> narasi_outlines`);
        }catch(e){console.warn("[narasi-google] outline persist failed (non-fatal):",e.message);}
        return res.json({ok:true,chapters,outline_text:outlineText});
      }
    }

    // clean LLM JSON: strip fences + trailing commas (safe)
    const cleanJson=(s)=>s
      .replace(/^```[^\n]*\n?/mg,"").replace(/\n?```$/mg,"")
      .replace(/,([\s\r\n]*[}\]])/g,"$1")
      .trim();

    const extractChapters=(data)=>{
      if(Array.isArray(data))return data;
      if(Array.isArray(data.chapters))return data.chapters;
      if(Array.isArray(data.outline))return data.outline;
      if(Array.isArray(data.bab))return data.bab;
      for(const v of Object.values(data)){
        if(v&&typeof v==="object"&&Array.isArray(v.chapters))return v.chapters;
        if(Array.isArray(v)&&v.length&&v[0].id!==undefined)return v;
      }
      return null;
    };
    const extractOutlineText=(data)=>
      data.outline_text||data.outlineText||data.outline_md||data.markdown||"";

    // Repair unescaped double quotes inside JSON string values
    const repairJson=(s)=>{
      let out="",inStr=false,esc=false;
      for(let i=0;i<s.length;i++){
        const c=s[i];
        if(esc){out+=c;esc=false;}
        else if(c==="\\"){out+=c;esc=true;}
        else if(c==='"'){
          if(!inStr){inStr=true;out+=c;}
          else{
            const rest=s.slice(i+1).replace(/^\s*/,"");
            if(!rest.length||/^[,:}\]]/.test(rest)){inStr=false;out+=c;}
            else{out+='\\"';}
          }
        } else {out+=c;}
      }
      return out;
    };

    const tryParse=(s)=>{
      for(const candidate of [s, repairJson(s)]){
        try{
          const d=JSON.parse(cleanJson(candidate));
          const ch=extractChapters(d);
          if(ch&&ch.length)return{ok:true,chapters:ch,outline_text:extractOutlineText(d)};
        }catch{}
      }
      return null;
    };

    let parsed=tryParse(text);
    if(!parsed){const m=text.match(/\{[\s\S]+\}/);if(m)parsed=tryParse(m[0]);}
    if(!parsed){const m=text.match(/\[[\s\S]+\]/);if(m)parsed=tryParse(m[0]);}

    if(parsed)return res.json(parsed);
    return res.status(500).json({error:"Tidak bisa parse outline — raw: "+text.slice(0,400)});
  }catch(e){res.status(500).json({error:e.message});}
};
app.post("/api/narasi/outline/google",  _narasiGoogleHandler);
app.post("/api/narasi/generate/google", _narasiGoogleHandler);

// ── Script → TTS transcript (Google native) ──────────────────────────────
app.post("/api/script/tts/google", async(req,res)=>{
  try{
    const {script,model="gemini-2.5-flash",google_api_key}=req.body;
    if(!script)return res.status(400).json({error:"script is required"});
    const apiKey=google_api_key||process.env.GEMINI_API_KEY;
    if(!apiKey)return res.status(400).json({error:"No Google API key"});
    const {GoogleGenAI}=await import("@google/genai");
    const ai=new GoogleGenAI({apiKey});
    const systemPrompt=
      "You are a professional TTS script editor. "+
      "Your ONLY job is to add intonation tags and split the script into paragraphs. "+
      "You must NEVER add, remove, rephrase, summarize, or change any word.\n\n"+
      "RULES — NON-NEGOTIABLE:\n"+
      "- Copy every word VERBATIM from the original script\n"+
      "- Do NOT add new sentences, commentary, or transitions\n"+
      "- Do NOT remove any sentence, clause, or word\n"+
      "- Do NOT rephrase, reorder, or paraphrase anything\n"+
      "- Only split long continuous text into smaller paragraphs at natural breath/idea breaks\n"+
      "- Prefix each paragraph with exactly one intonation tag in square brackets\n"+
      "- Keep the original language unchanged\n\n"+
      "Available tags (use these and invent others as needed based on context):\n"+
      "[curiosity] [information] [reflection] [revelation] [quiet confidence] [deadpan]\n"+
      "[wonder] [weight] [intimacy] [urgency] [melancholy] [wry] [gravity] [tenderness]\n"+
      "[disbelief] [sadness] [joy] [anger] [fear] [awe] [humor] [sarcasm] [suspense]\n"+
      "[nostalgia] [pride] [empathy] [determination] [irony] [warmth] [solemnity]\n\n"+
      "Output format: paragraphs separated by blank lines, each starting with [tag].\n"+
      "Return ONLY the tagged transcript. No explanation, no markdown, no added text.";
    const result=await ai.models.generateContent({
      model,
      contents:[{role:"user",parts:[{text:`Transform this script into a tagged TTS transcript:\n\n${script}`}]}],
      config:{systemInstruction:systemPrompt,maxOutputTokens:8000}
    });
    let text=(result.text||"").trim().replace(/^```[^\n]*\n?/mg,"").replace(/\n?```$/mg,"").trim();
    const paragraphs=text.split(/\n\n+/).map(p=>p.trim()).filter(Boolean);
    try{
      const _um=result?.usageMetadata||{}, _ti=_um.promptTokenCount||0, _to=_um.candidatesTokenCount||0;
      const _t=resolveTenantId(req), _u=await resolveUserId(req,_t);
      await logUsage(_t,_u,model,"chat",_ti,_to,calcGoogleCost(model,_ti,_to),null,"gemini");   // text transform → bill as tokens + charge
    }catch(_){}
    res.json({ok:true,transcript:text,paragraphs,count:paragraphs.length});
  }catch(e){res.status(500).json({error:e.message});}
});
app.post("/api/flow/images/lz",          (req,res)=>pyProxy(req,res,"/flow/images"));

// ══════════════════════════════════════════════════════════════════════════════
// VEO  (proxy → Python)
// ══════════════════════════════════════════════════════════════════════════════
// ── Veo: DEFAULT Google (Gemini); a LaoZhang key (X-Veo-API-Key) overrides ────
app.post("/api/veo/submit", async (req,res)=>{
  // Provider is chosen EXPLICITLY by the UI (main-Veo "LaoZhang model" checkbox /
  // Flow apiMode toggle) via body.provider — NOT inferred from key presence.
  // Legacy fallback: a LaoZhang key present → LaoZhang, else Google.
  const provider = String(req.body?.provider||"").toLowerCase();
  const lzKey = (req.headers["x-veo-api-key"]||"").trim();
  const goLaoZhang = provider==="laozhang" || (provider!=="google" && !!lzKey);
  if (goLaoZhang) return pyProxy(req,res,"/veo/submit");       // LaoZhang (Python meters it as provider=laozhang)
  try {                                                        // Google
    const tenantId = resolveTenantId(req);
    const userId   = await resolveUserId(req, tenantId);
    const b = req.body || {};
    const aspect = (b.preset && typeof b.preset==="object" && b.preset.aspectRatio) || b.aspect || "16:9";
    const out = await googleVeoSubmit({
      prompt:b.prompt, model:b.model, refB64:b.ref_image_b64, refMime:b.ref_image_mime,
      negativePrompt:b.negative_prompt, aspectRatio:aspect, googleKey:(b.google_api_key||"").trim(),
      tenantId, userId });
    // usage row with the CORRECT provider (google) — was mislabeled laozhang.
    // cost 0 for now (no Google Veo price wired) → 0 credits charged.
    try { await logUsage(tenantId, userId, b.model||"veo", "video", 0, 0, 0, null, "google", null); } catch(_){}
    res.json(out);
  } catch(e){
    const noKey = e.code==="no_google_key";
    res.status(noKey?400:502).json({ error: noKey
      ? "No Google API key — set GEMINI_API_KEY on the server, or pick a LaoZhang model"
      : "Google Veo: "+String(e.message||e).slice(0,300) });
  }
});
app.get("/api/veo/status/:id", async (req,res)=>{
  if (!isGoogleVeo(req.params.id)) return pyProxy(req,res,`/veo/status/${req.params.id}`);
  res.json(await googleVeoStatus(req.params.id));
});
app.get ("/api/veo/download/:id", async(req,res)=>{
  if (isGoogleVeo(req.params.id)) return veoServeGoogle(req,res,true);
  try{
    const LZ_KEY = process.env.LAOZHANG_IMAGE_API_KEY || process.env.LAOZHANG_API_KEY || "";
    const r = await fetch(`https://api.laozhang.ai/v1/videos/${req.params.id}/content`,
      {headers:{"Authorization":`Bearer ${LZ_KEY}`}, timeout:60000});
    if(!r.ok){return res.status(r.status).json({error:"Download failed: "+r.statusText});}
    const buf = Buffer.from(await r.arrayBuffer());
    res.setHeader("Content-Type","video/mp4");
    res.setHeader("Content-Disposition",`attachment; filename="veo-${req.params.id.slice(-8)}.mp4"`);
    res.setHeader("Content-Length", buf.length);
    res.end(buf);
  }catch(e){res.status(500).json({error:e.message});}
});
app.get ("/api/veo/stream/:id", async(req,res)=>{
  if (isGoogleVeo(req.params.id)) return veoServeGoogle(req,res,false);
  try{
    const headers={}; if(req.headers["x-veo-api-key"]) headers["X-Veo-API-Key"]=req.headers["x-veo-api-key"];
    const pyRes=await fetch(`${PYTHON_API}/veo/stream/${req.params.id}`,{headers});
    if(!pyRes.ok){return res.status(pyRes.status).json({error:await pyRes.text()});}
    res.setHeader("Content-Type","video/mp4");res.setHeader("Cache-Control","no-store");
    res.end(Buffer.from(await pyRes.arrayBuffer()));
  }catch(e){if(!res.writableEnded)res.status(500).json({error:e.message});}
});
// Serve a finished Google Veo video — from R2 if already captured, else fetch it
// from Google and persist once (moat: modality=video + source_prompt).
async function veoServeGoogle(req,res,asDownload){
  const taskId = req.params.id;
  const sendBytes = (bytes)=>{
    res.setHeader("Content-Type","video/mp4");
    res.setHeader("Cache-Control","no-store");
    if (asDownload) res.setHeader("Content-Disposition",`attachment; filename="veo-${taskId.slice(-8)}.mp4"`);
    res.setHeader("Content-Length", bytes.length);
    res.end(bytes);
  };
  const job = getGoogleVeoJob(taskId);
  if (job && job.r2Key && storage.isConfigured()){
    try{ return sendBytes(await storage.downloadBytes(job.r2Key)); }catch(_){ /* fall through to re-fetch */ }
  }
  try{
    const r = await googleVeoResult(taskId);
    if (!r.done) return res.status(202).json({ status:"processing" });
    if (job && !job.r2Key && job.tenantId){
      try{
        const a = await persistAsset({ tenantId:job.tenantId, userId:job.userId||null,
          assetType:"video", sourceJobType:"veo", filename:`${taskId}.mp4`,
          buffer:r.bytes, contentType:"video/mp4", metadata:{ provider:"google", prompt:job.prompt } });
        if (a) markGoogleVeoR2(taskId, a.key);
      }catch(e){ console.warn("[googleVeo] persist failed (non-fatal):", String(e.message||e).slice(0,120)); }
    }
    sendBytes(r.bytes);
  }catch(e){ if(!res.writableEnded) res.status(502).json({ error:"Google Veo: "+String(e.message||e).slice(0,300) }); }
}

// ══════════════════════════════════════════════════════════════════════════════
// SORA  (proxy → Python)
// ══════════════════════════════════════════════════════════════════════════════
app.post("/api/sora/submit",     (req,res)=>pyProxy(req,res,"/sora/submit"));
app.get ("/api/sora/status/:id", (req,res)=>pyProxy(req,res,`/sora/status/${req.params.id}`));
app.get ("/api/sora/stream/:id", async(req,res)=>{
  try{
    const headers={}; if(req.headers["x-sora-api-key"]) headers["X-Sora-API-Key"]=req.headers["x-sora-api-key"];
    const pyRes=await fetch(`${PYTHON_API}/sora/stream/${req.params.id}`,{headers});
    if(!pyRes.ok){return res.status(pyRes.status).json({error:await pyRes.text()});}
    res.setHeader("Content-Type","video/mp4");res.setHeader("Cache-Control","no-store");
    res.end(Buffer.from(await pyRes.arrayBuffer()));
  }catch(e){if(!res.writableEnded)res.status(500).json({error:e.message});}
});

// ══════════════════════════════════════════════════════════════════════════════
// MCP  (proxy → sidecar)
// ══════════════════════════════════════════════════════════════════════════════
const mcpGet = async(res,path)=>{
  try{const r=await fetch(`${MCP_API}${path}`);res.json(await r.json());}
  catch{res.json({available:false,error:"MCP not running"});}
};
app.get ("/api/mcp/status",  async(_,res)=>{ try{const r=await fetch(`${MCP_API}/`);const d=await r.json();res.json({available:true,...d});}catch{res.json({available:false,url:MCP_API});} });
app.get ("/api/mcp/info",    async(_,res)=>mcpGet(res,"/"));
app.get ("/api/mcp/files",   async(_,res)=>mcpGet(res,"/files"));
app.get ("/api/mcp/search",  async(req,res)=>mcpGet(res,`/search?q=${encodeURIComponent(req.query.q||"")}`));
app.get ("/api/mcp/context", async(req,res)=>mcpGet(res,`/context?paths=${encodeURIComponent(req.query.paths||"")}&q=${encodeURIComponent(req.query.q||"")}`));
app.get ("/api/mcp/file",    async(req,res)=>mcpGet(res,`/file?path=${encodeURIComponent(req.query.path||"")}`));
app.post("/api/mcp/reindex", async(_,res)=>{ try{const r=await fetch(`${MCP_API}/reindex`,{method:"POST"});res.json(await r.json());}catch(e){res.status(500).json({error:e.message});} });

// ── MCP: hot-swap folder (no restart needed) ─────────────────────────────
app.post("/api/mcp/set-folder", async(req,res)=>{
  try{
    const r=await fetch(`${MCP_API}/set-folder`,{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(req.body),
    });
    res.json(await r.json());
  }catch(e){res.status(500).json({error:e.message});}
});

// ── MCP: SRT polish (long-running — 5 min timeout) ───────────────────────
app.post("/api/mcp/srt/polish", async(req,res)=>{
  try{
    const ctrl=new AbortController();
    const timer=setTimeout(()=>ctrl.abort(),1800_000); // 30 min
    // Inject Google API key: use client-provided key first, fallback to server env
    const body = { ...req.body };
    if (body.api_provider === "google" && !body.google_api_key && GEMINI_KEY) {
      body.google_api_key = GEMINI_KEY;  // fallback to server .env
    }
    const r=await fetch(`${MCP_API}/srt/polish`,{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(body),
      signal:ctrl.signal,
    });
    clearTimeout(timer);
    res.json(await r.json());
  }catch(e){res.status(500).json({error:e.message});}
});

// ── Additional MCP routes (semantic search, mode, tool-capable models) ────────
app.get ("/api/mcp/search_semantic", async(req,res)=>mcpGet(res,`/search_semantic?q=${encodeURIComponent(req.query.q||"")}&top_k=${req.query.top_k||25}`));
app.get ("/api/mcp/mode",            async(_,res)=>mcpGet(res,"/mode"));
app.get ("/api/mcp/tool-models",     async(_,res)=>{ try{const r=await fetch(`${PYTHON_API}/mcp/tool-capable-models`);res.json(await r.json());}catch(e){res.status(500).json({error:e.message});} });

// ══════════════════════════════════════════════════════════════════════════════
// BATCH IMAGES  (direct @google/genai)
// ══════════════════════════════════════════════════════════════════════════════
app.get("/api/jobs", requireAuth, async(req,res)=>{
  try{
    const rows = await listJobs(resolveTenantId(req), "batch_image");
    // Re-expose the batch-shaped record the UI expects (it lived in result_payload).
    res.json(rows.map(r => ({ ...(r.result_payload||{}), id:r.id, state:(r.result_payload?.state)||r.status, createdAt:r.created_at })));
  }catch(e){ res.status(500).json({error:e.message}); }
});
app.post("/api/submit", requireAuth, async(req,res)=>{
  if(!ai) return res.status(400).json({error:"No GEMINI_API_KEY in .env"});
  try{
    const tenantId = resolveTenantId(req);
    const userId   = await resolveUserId(req, tenantId);
    const {settings={},jobs=[]}=req.body||{};
    if(!jobs.length) return res.status(400).json({error:"No jobs"});
    const model=settings.modelId||"gemini-3-pro-image-preview";
    const stamp=Date.now(),jsonlPath=path.join(TMP_DIR,`batch-${stamp}.jsonl`);
    fs.writeFileSync(jsonlPath,buildJsonl(settings,jobs));
    const uploaded=await ai.files.upload({file:jsonlPath,config:{mimeType:"jsonl",displayName:"image-batch-requests"}});
    const batch=await ai.batches.create({model,src:uploaded.name,config:{displayName:settings.displayName||"image-generation-batch"}});
    const mapping=jobs.map((j,i)=>({key:`image-${i+1}`,output:(j.output||`image-${i+1}`).trim()}));
    const record={jobName:batch.name,model,count:jobs.length,displayName:settings.displayName||"image-generation-batch",
      aspectRatio:settings.aspectRatio||"16:9",imageSize:settings.imageSize||"1K",
      state:stateName(batch.state)||"JOB_STATE_PENDING",createdAt:new Date().toISOString(),mapping};
    // Durable record in jobs table. Google-specific fields live in result_payload.
    const jobId = await createJob(tenantId, userId, "batch_image", record.displayName);
    await completeJob(jobId, record);   // payload set; batch tracks real lifecycle in payload.state
    try{ await logUsage(tenantId, userId, model, "batch", 0, 0, calcImageCost(model, record.count||0), null, "gemini"); }
    catch(e){ console.error("[batch] logUsage failed:", e.message); }
    try{fs.unlinkSync(jsonlPath);}catch{}
    res.json({ok:true,jobName:batch.name,record:{...record,id:jobId}});
  }catch(e){console.error(e);res.status(500).json({error:e?.message||String(e)});}
});
app.get("/api/status", requireAuth, async(req,res)=>{
  if(!ai) return res.status(400).json({error:"No API key"});
  const name=req.query.name;if(!name)return res.status(400).json({error:"name required"});
  try{
    const tenantId = resolveTenantId(req);
    const job=await ai.batches.get({name}),state=stateName(job.state);
    const row = await findJobByJobName(tenantId, name);
    if(row) await patchJobPayload(row.id, { state });
    res.json({ok:true,state,destFile:job?.dest?.fileName||null});
  }catch(e){res.status(500).json({error:e?.message||String(e)});}
});
app.post("/api/retrieve", requireAuth, async(req,res)=>{
  if(!ai) return res.status(400).json({error:"No API key"});
  try{
    const tenantId = resolveTenantId(req);
    const userId   = await resolveUserId(req, tenantId);
    const {jobName}=req.body||{};if(!jobName)return res.status(400).json({error:"jobName required"});
    const row = await findJobByJobName(tenantId, jobName);
    const record = row?.result_payload || null;
    const mapOf=Object.fromEntries((record?.mapping||[]).map(m=>[m.key,m.output]));
    const job=await ai.batches.get({name:jobName}),state=stateName(job.state);
    if(state!=="JOB_STATE_SUCCEEDED") return res.json({ok:false,state,message:"Not ready"});
    let destFile=job?.dest?.fileName;
    if(!destFile)return res.status(500).json({error:"No result file"});
    if(!destFile.startsWith("files/"))destFile=`files/${destFile}`;
    const r=await fetch(`https://generativelanguage.googleapis.com/download/v1beta/${destFile}:download?alt=media`,{headers:{"x-goog-api-key":GEMINI_KEY}});
    if(!r.ok)throw new Error(`Download failed (${r.status})`);
    const text=await r.text();let saved=0,failed=0;const files=[];
    for(const line of text.split("\n")){
      if(!line.trim())continue;
      let parsed;try{parsed=JSON.parse(line);}catch{failed++;continue;}
      const key=parsed.key||`image-${saved+1}`,filename=mapOf[key]||key;
      if(parsed.error){failed++;continue;}
      const parts=parsed?.response?.candidates?.[0]?.content?.parts||[];
      const part=parts.find(p=>p?.inlineData?.data||p?.inline_data?.data);
      const data=part?.inlineData?.data||part?.inline_data?.data;
      if(!data){failed++;continue;}
      const outName=`${filename}.png`;const _buf=Buffer.from(data,"base64");
      fs.writeFileSync(path.join(OUTPUT_DIR,outName),_buf);
      const _rec={key,file:outName};
      try{ const a=await persistAsset({tenantId,userId,jobId:row?.id||null,assetType:"image",sourceJobType:"batch_image",filename:outName,buffer:_buf,contentType:"image/png",metadata:{batch:jobName}});
           if(a){_rec.assetKey=a.key;_rec.assetId=a.id;} }
      catch(e){console.error("[batch] R2 persist failed:",e.message);}
      files.push(_rec);saved++;
    }
    if(row) await patchJobPayload(row.id, { state, retrieved: saved });
    res.json({ok:true,state,saved,failed,files});
  }catch(e){console.error(e);res.status(500).json({error:e?.message||String(e)});}
});
app.get("/api/images",(_,res)=>{try{res.json(fs.readdirSync(OUTPUT_DIR).filter(f=>/\.png$/i.test(f)).map(f=>({file:f,url:`/images/${encodeURIComponent(f)}`})));}catch{res.json([]);}});

// ══════════════════════════════════════════════════════════════════════════════
// TTS  (direct @google/genai, sequential with key rotation)
// ══════════════════════════════════════════════════════════════════════════════

// ── LaoZhang TTS (OpenAI-compatible /v1/audio/speech) ──────────────────────────
// Docs: https://docs.laozhang.ai/en/api-capabilities/audio-transcription
// Models: tts-1, tts-1-hd  | Voices: alloy, echo, fable, onyx, nova, shimmer
//
// Smart text chunker for audiobook mode — splits on paragraph → sentence → hard
// boundaries, keeping each chunk under maxChars (default 4000, below the
// /v1/audio/speech 4096-char limit).
function chunkTextForAudiobook(text, maxChars=4000){
  const chunks=[];
  let buf="";
  const flush=()=>{ if(buf.trim()){chunks.push(buf.trim());} buf=""; };
  const paragraphs=text.split(/\n\n+/);
  for(const p of paragraphs){
    if((buf.length+p.length+2)<=maxChars){
      buf+=(buf?"\n\n":"")+p;
    }else{
      flush();
      if(p.length<=maxChars){ buf=p; continue; }
      // paragraph too long — split by sentence (., !, ?, …, plus newline)
      const sentences=p.match(/[^.!?…\n]+[.!?…]+\s*|\S[^.!?…\n]*$/g)||[p];
      for(const s of sentences){
        if((buf.length+s.length)<=maxChars){ buf+=s; }
        else{
          flush();
          if(s.length<=maxChars){ buf=s; }
          else{ // hard split — sentence itself > maxChars
            for(let i=0;i<s.length;i+=maxChars) chunks.push(s.slice(i,i+maxChars));
          }
        }
      }
    }
  }
  flush();
  return chunks;
}
async function runLaozhangTtsJob(jobId,{tenantId,userId,apiKeys,model,voice,speed,language,audiobookMode,silenceSeconds,audioProfile,transcriptBody,outputPrefix}){
  let job=await getLiveJob(jobId);
  // Audiobook mode = treat whole transcript as continuous prose, chunk at ~4000 char.
  // Default = split by blank line (one file per paragraph), legacy behaviour.
  const lines = audiobookMode
    ? chunkTextForAudiobook(transcriptBody.trim(),4000)
    : transcriptBody.trim().split(/\n\n+/).filter(l=>l.trim());
  job.total=lines.length;job.progress=0;job.status="running";
  let keyIdx=0;
  const log=msg=>{job.logs.push(msg);};
  const flush=()=>setLiveJob(jobId,job);   // persist live object to Redis
  await flush();
  const langTag=(language&&language!=="auto")?`_${language}`:"";
  log(`🟣 LaoZhang TTS · model=${model} voice=${voice} speed=${speed}${audiobookMode?" · 📖 audiobook mode":""}${langTag?" · lang="+language:""} · ${lines.length} chunk(s)`);
  for(let i=0;i<lines.length;){
    if(ttsCancelFlags.get(jobId)){
      log("🚫 Cancelled by user");
      job.status="cancelled";
      ttsCancelFlags.delete(jobId);
      await flush();
      await failJob(jobId, "Cancelled by user");   // terminal: mark DB row 'error'
      return;
    }
    const line=lines[i],filename=`${outputPrefix||"tts"}${langTag}_${String(i+1).padStart(2,"0")}.wav`;
    log(`[${i+1}/${lines.length}] ${filename} (${line.length} chars)`);
    // NOTE: OpenAI/LaoZhang TTS auto-detects language from the input text itself
    // (Han chars → Chinese, kana → Japanese, etc.) — there is NO language param.
    // The `language` dropdown is purely a filename suffix, matching the docs example:
    //   for lang, text in texts.items():
    //       ...create(input=text); stream_to_file(f"welcome_{lang}.mp3")
    // Prepending the language name caused the TTS to literally speak "Indonesian".
    const inputText = line;
    try{
      const resp=await fetch("https://api.laozhang.ai/v1/audio/speech",{
        method:"POST",
        headers:{
          "Content-Type":"application/json",
          "Authorization":`Bearer ${apiKeys[keyIdx]}`,
        },
        body:JSON.stringify({
          model,
          voice,
          input:inputText.length>4000?inputText.slice(0,4000):inputText,  // safety cap (chunker keeps ≤4000 in audiobook mode)
          speed:Number(speed)||1.0,
          response_format:"wav",
        }),
      });
      if(!resp.ok){
        const errTxt=await resp.text().catch(()=>resp.statusText);
        const quota=resp.status===429||resp.status===401||resp.status===403;
        if(quota&&keyIdx+1<apiKeys.length){
          keyIdx++;log(`🔄 Key #${keyIdx+1} (${resp.status})`);continue;
        }else if(quota){log(`🔴 All keys exhausted (${resp.status})`);break;}
        else{log(`🔴 ${resp.status}: ${errTxt.slice(0,120)}`);await new Promise(r=>setTimeout(r,3000));i++;job.progress=i;await flush();continue;}
      }
      const ab=await resp.arrayBuffer();
      let wav=Buffer.from(ab);
      // Try to prepend silence — assumes WAV. If header is missing/incompatible,
      // prependSilence may throw; in that case just write the raw bytes.
      if(silenceSeconds>0){
        try{wav=prependSilence(wav,silenceSeconds);}catch(_){/* keep original */}
      }
      fs.writeFileSync(path.join(TTS_DIR,filename),wav);
      const _f={file:filename,url:`/audio/${encodeURIComponent(filename)}`};
      job.files.push(_f);
      try{ const a=await persistAsset({tenantId,userId,jobId,assetType:"audio",sourceJobType:"tts",filename,buffer:wav,contentType:"audio/wav",metadata:{model,voice,text:inputText}});
           if(a){_f.key=a.key;_f.assetId=a.id;} }
      catch(e){ log(`⚠️  R2 persist failed: ${String(e.message||e).slice(0,80)}`); }
      log(`✅ ${filename}`);
      await new Promise(r=>setTimeout(r,400));
      i++;job.progress=i;await flush();
    }catch(e){
      const msg=String(e);
      log(`🔴 ${msg.slice(0,120)}`);
      await new Promise(r=>setTimeout(r,3000));
      i++;job.progress=i;await flush();
    }
  }
  job.status="done";log("✅ Complete");
  await flush();
  await completeJob(jobId, { files: job.files, total: job.total });
  try{ await logUsage(tenantId, userId, model, "tts", 0, 0, (transcriptBody||"").length/1000*0.10, null, "laozhang"); }
  catch(e){ console.error("[tts] logUsage failed:", e.message); }
}

async function runTtsJob(jobId,{tenantId,userId,apiKeys,model,voice,silenceSeconds,audioProfile,transcriptBody,outputPrefix}){
  let job=await getLiveJob(jobId);
  const lines=transcriptBody.trim().split(/\n\n+/).filter(l=>l.trim());
  job.total=lines.length;job.progress=0;job.status="running";
  let keyIdx=0,ai2=new GoogleGenAI({apiKey:apiKeys[keyIdx]});
  const log=msg=>{job.logs.push(msg);};
  const flush=()=>setLiveJob(jobId,job);
  await flush();
  for(let i=0;i<lines.length;){
    // Check cancel flag
    if(ttsCancelFlags.get(jobId)){
      log("🚫 Cancelled by user");
      job.status="cancelled";
      ttsCancelFlags.delete(jobId);
      await flush();
      await failJob(jobId, "Cancelled by user");
      return;
    }
    const line=lines[i],filename=`${outputPrefix||"tts"}_${String(i+1).padStart(2,"0")}.wav`;
    log(`[${i+1}/${lines.length}] ${filename}`);
    try{
      const resp=await ai2.models.generateContent({model,contents:[{role:"user",parts:[{text:`${audioProfile}\n\n## Transcript:\n${line}`}]}],
        config:{temperature:1.0,responseModalities:["AUDIO"],speechConfig:{voiceConfig:{prebuiltVoiceConfig:{voiceName:voice}}}}});
      const inlineData=resp?.candidates?.[0]?.content?.parts?.[0]?.inlineData;
      if(inlineData?.data){
        const raw=Buffer.from(inlineData.data,"base64");
        let wav=convertToWav(raw,inlineData.mimeType||"audio/L16;rate=24000");
        if(silenceSeconds>0)wav=prependSilence(wav,silenceSeconds);
        fs.writeFileSync(path.join(TTS_DIR,filename),wav);
        const _f={file:filename,url:`/audio/${encodeURIComponent(filename)}`};
        job.files.push(_f);
        try{ const a=await persistAsset({tenantId,userId,jobId,assetType:"audio",sourceJobType:"tts",filename,buffer:wav,contentType:"audio/wav",metadata:{model,voice,text:line}});
             if(a){_f.key=a.key;_f.assetId=a.id;} }
        catch(e){ log(`⚠️  R2 persist failed: ${String(e.message||e).slice(0,80)}`); }
        log(`✅ ${filename}`);
      }else{log(`⚠️  No audio for line ${i+1}`);}
      await new Promise(r=>setTimeout(r,1000));i++;job.progress=i;await flush();
    }catch(e){
      const msg=String(e);
      const quota=msg.includes("429")||msg.includes("RESOURCE_EXHAUSTED")||(msg.includes("400")&&msg.includes("INVALID_ARGUMENT"));
      if(quota&&keyIdx+1<apiKeys.length){keyIdx++;ai2=new GoogleGenAI({apiKey:apiKeys[keyIdx]});log(`🔄 Key #${keyIdx+1}`);}
      else if(quota){log("🔴 All keys exhausted");break;}
      else{log(`🔴 ${msg.slice(0,80)}`);await new Promise(r=>setTimeout(r,5000));i++;job.progress=i;await flush();}
    }
  }
  job.status="done";log("✅ Complete");
  await flush();
  await completeJob(jobId, { files: job.files, total: job.total });
  try{ await logUsage(tenantId, userId, model, "tts", 0, 0, (transcriptBody||"").length/1000*0.10, null, "gemini"); }
  catch(e){ console.error("[tts] logUsage failed:", e.message); }
}
app.get("/api/tts/jobs", requireAuth, async(req,res)=>{
  try{
    const rows = await listJobs(resolveTenantId(req), "tts");
    res.json(await Promise.all(rows.map(async r => ({
      jobId:r.id, status:r.status, model:(r.result_payload?.model)||null,
      voice:(r.result_payload?.voice)||null, outputPrefix:r.output_prefix,
      total:(r.result_payload?.total)||0, files: await signFiles(r.result_payload?.files),
      createdAt:r.created_at
    }))));
  }catch(e){ res.status(500).json({error:e.message}); }
});
app.get("/api/tts/files", (_,res)=>{try{res.json(fs.readdirSync(TTS_DIR).filter(f=>/\.wav$/i.test(f)).map(f=>({file:f,url:`/audio/${encodeURIComponent(f)}`})));}catch{res.json([]);}});
app.get("/api/tts/job/:id", requireAuth, async(req,res)=>{
  try{
    const live = await getLiveJob(req.params.id);   // running job → rich live object
    if(live){ live.files = await signFiles(live.files); return res.json(live); }
    const row = await getJob(resolveTenantId(req), req.params.id);   // finished → DB row
    if(row) return res.json({
      status:row.status,
      progress:(row.result_payload?.total)||0,
      total:(row.result_payload?.total)||0,
      files: await signFiles(row.result_payload?.files),
      logs:[]
    });
    res.status(404).json({error:"Not found"});
  }catch(e){ res.status(500).json({error:e.message}); }
});
app.post("/api/tts/start", requireAuth, async(req,res)=>{
  const{apiMode="google",apiKeys=[],model="gemini-3.1-flash-tts-preview",voice="Enceladus",speed=1.0,language="auto",audiobookMode=false,silenceSeconds=0.5,audioProfile="",transcriptBody="",outputPrefix="tts"}=req.body||{};
  const rawKeys=[...(Array.isArray(apiKeys)?apiKeys:[apiKeys])].filter(Boolean);
  // Google falls back to server env GEMINI_KEY; LaoZhang has no env fallback here
  const keys=apiMode==="laozhang"?rawKeys:[...rawKeys,GEMINI_KEY].filter(Boolean);
  if(!keys.length)return res.status(400).json({error:apiMode==="laozhang"?"No LaoZhang API key":"No API keys"});
  if(!transcriptBody.trim())return res.status(400).json({error:"Empty transcript"});
  const tenantId = resolveTenantId(req);
  const userId   = await resolveUserId(req, tenantId);
  // Preview count = audiobook chunks for LaoZhang+audiobook mode, else paragraphs
  const lines=(apiMode==="laozhang"&&audiobookMode)
    ? chunkTextForAudiobook(transcriptBody.trim(),4000)
    : transcriptBody.trim().split(/\n\n+/).filter(l=>l.trim());
  // Durable row (status 'processing'); UUID id becomes the jobId the UI polls.
  const jobId = await createJob(tenantId, userId, "tts", outputPrefix);
  // Live object in Redis (replaces activeJobs.set + the JSON file unshift).
  await setLiveJob(jobId, { jobId, apiMode, model, voice, outputPrefix,
    status:"queued", total:lines.length, progress:0, logs:[], files:[],
    createdAt:new Date().toISOString() });
  // Seed result_payload so list/restart shows model/voice/total before completion.
  await patchJobPayload(jobId, { apiMode, model, voice, total:lines.length });
  const runner=apiMode==="laozhang"?runLaozhangTtsJob:runTtsJob;
  runner(jobId,{tenantId,userId,apiKeys:keys,model,voice,speed,language,audiobookMode,silenceSeconds,audioProfile,transcriptBody,outputPrefix})
    .catch(async e=>{ await failJob(jobId, e.message);
      await updateLiveJob(jobId,(j)=>{j.status="error";(j.logs=j.logs||[]).push("Fatal: "+e.message);return j;}); });
  res.json({ok:true,jobId,total:lines.length});
});
app.post("/api/tts/cancel/:id", requireAuth, async(req,res)=>{
  const jobId=req.params.id;
  const job=await getLiveJob(jobId);
  if(!job)return res.status(404).json({error:"Job not found"});
  if(job.status!=="running"&&job.status!=="queued")return res.json({ok:false,reason:"Job not running",status:job.status});
  ttsCancelFlags.set(jobId,true);
  job.status="cancelling";
  await setLiveJob(jobId, job);
  res.json({ok:true,jobId,status:"cancelling"});
});

// ══════════════════════════════════════════════════════════════════════════════
// ASSETS — signed-URL refresh (Step 2.4)
// ══════════════════════════════════════════════════════════════════════════════
// Frontend calls this to (re)mint a signed URL when one expires (10-min TTL).
// RLS-scoped to the caller's tenant.
app.get("/api/assets/sign", requireAuth, async (req, res) => {
  try {
    const tenantId = resolveTenantId(req);
    const key = req.query.key;
    if (!key) return res.status(400).json({ error: "key required" });
    const url = await signedUrlForKey(tenantId, key);
    if (!url) return res.status(404).json({ error: "asset not found" });
    res.json({ url, expiresIn: 600 });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// Same-origin content proxy — streams an asset's bytes from R2 so the browser can
// fetch() text (for TXT/PDF/HTML download) without hitting R2 CORS. RLS-scoped:
// only returns content for a key the caller's tenant owns.
app.get("/api/assets/content", requireAuth, async (req, res) => {
  try {
    const tenantId = resolveTenantId(req);
    const key = req.query.key;
    if (!key) return res.status(400).json({ error: "key required" });
    const owns = await query(
      "SELECT content_type FROM assets WHERE s3_key=$1 AND tenant_id=$2 AND is_deleted=false LIMIT 1",
      [key, tenantId], tenantId
    );
    if (!owns.rows.length) return res.status(404).json({ error: "asset not found" });
    if (!storage.isConfigured()) return res.status(503).json({ error: "storage not configured" });
    const buf = await storage.downloadBytes(key);
    res.setHeader("Content-Type", owns.rows[0].content_type || "application/octet-stream");
    res.send(buf);
  } catch (e) { console.error("[/api/assets/content]", e.message); res.status(500).json({ error: e.message }); }
});

// Delete a single asset from the vault (R2 object + DB row). RLS-scoped — the
// DELETE only matches rows belonging to the caller's tenant.
app.delete("/api/assets", requireAuth, async (req, res) => {
  try {
    const tenantId = resolveTenantId(req);
    const key = req.query.key;
    if (!key) return res.status(400).json({ error: "key required" });
    const r = await query("DELETE FROM assets WHERE s3_key=$1 AND tenant_id=$2 RETURNING id", [key, tenantId], tenantId);
    if (!r.rows.length) return res.status(404).json({ error: "asset not found" });
    if (storage.isConfigured()) { try { await storage.del(key); } catch (e) { console.error("[delete asset] R2:", e.message); } }
    res.json({ ok: true });
  } catch (e) { console.error("[DELETE /api/assets]", e.message); res.status(500).json({ error: e.message }); }
});

// Tier-based vault retention sweep: delete expired assets (DB via SECURITY DEFINER
// fn cleanup_expired_assets — free 7d/starter 14d/pro 30d/enterprise 90d) + their
// R2 objects. Idempotent; runs daily + on a manual trigger.
async function runAssetRetention() {
  try {
    const r = await query("SELECT s3_key FROM cleanup_expired_assets()", [], null);
    const keys = r.rows.map((x) => x.s3_key).filter(Boolean);
    let r2 = 0;
    if (storage.isConfigured()) for (const k of keys) { try { await storage.del(k); r2++; } catch (e) {} }
    if (keys.length) console.log(`[retention] removed ${keys.length} expired assets (${r2} R2 objects)`);
    return keys.length;
  } catch (e) { console.error("[retention]", e.message); return -1; }
}
// Admin-gated: this runs a destructive cross-tenant retention sweep, so it must
// NOT be reachable by an ordinary authenticated user. The daily/boot sweep calls
// runAssetRetention() directly (see below), so it is unaffected by this gate.
app.post("/api/admin/cleanup-assets", async (req, res) => {
  if (!adminGate(req, res)) return;
  try { const n = await runAssetRetention(); res.json({ ok: n >= 0, removed: Math.max(0, n) }); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

// Durable "Media Vault" retrieval — lists the tenant's assets straight from the
// assets table (survives redeploy + job cleanup), newest first, each with a fresh
// 10-min signed R2 URL. Categorised for the gallery tabs:
//   ?category=image|video|flow|whisk|narasi|narasi_review   (or omit = all)
// Also: ?limit=, paginate with ?before=<previous page's nextBefore>.
const VAULT_CATEGORIES = {
  image:         { sourceJobTypes: ["generate_image", "imagen", "batch_image"] },
  video:         { sourceJobTypes: ["veo", "sora"] },
  flow:          { sourceJobTypes: ["flow_image", "flow_storyboard"] },
  whisk:         { sourceJobTypes: ["whisk"] },
  narasi:        { assetType: "document", metadataKind: "narasi" },
  outline:       { assetType: "document", metadataKind: "outline" },
  chat:          { assetType: "document", metadataKind: "chat" },
  narasi_review: { assetType: "document", metadataKind: "narasi_review" },
  uploads:       { metadataKind: "upload" },   // user-uploaded reference assets
};
app.get("/api/assets", requireAuth, async (req, res) => {
  try {
    const tenantId = resolveTenantId(req);
    const { category = null, limit = 50, before = null } = req.query;
    const cat = category && category !== "all" ? (VAULT_CATEGORIES[category] || {}) : {};
    const rows = await listAssets(tenantId, { ...cat, limit, before });
    const assets = await Promise.all(rows.map(async (r) => ({
      id: r.id,
      assetType: r.asset_type,
      sourceJobType: r.source_job_type,
      filename: r.original_filename,
      contentType: r.content_type,
      sizeBytes: Number(r.size_bytes),
      createdAt: r.created_at,
      key: r.s3_key,
      metadata: r.metadata,
      signedUrl: storage.isConfigured() ? await storage.signedUrl(r.s3_key, 600) : null,
    })));
    res.json({ assets, nextBefore: rows.length ? rows[rows.length - 1].created_at : null });
  } catch (e) {
    console.error("[/api/assets]", e.message);
    res.status(500).json({ error: e.message });
  }
});

// ══════════════════════════════════════════════════════════════════════════════
// IMAGEN  (direct @google/genai, sequential)
// ══════════════════════════════════════════════════════════════════════════════
const PAUSE_MODELS=["imagen-4.0-ultra-generate-001","imagen-4.0-generate-001","imagen-4.0-fast-generate-001"];
async function runImagenJob(jobId,{tenantId,userId,apiKey,model,prompts,outputPrefix,aspectRatio,resolution}){
  let job=await getLiveJob(jobId);job.total=prompts.length;job.progress=0;job.status="running";
  const flush=()=>setLiveJob(jobId,job);
  await flush();
  const ai3=new GoogleGenAI({apiKey:apiKey||GEMINI_KEY});
  const [w,h]=(resolution||"1920x1080").split("x").map(Number);
  const log=msg=>job.logs.push(msg);
  for(let i=0;i<prompts.length;i++){
    const filename=`${outputPrefix||"imagen"}_${String(i+1).padStart(3,"0")}.jpeg`;
    log(`[${i+1}/${prompts.length}] ${filename}`);
    try{
      const resp=await ai3.models.generateImages({model:model.startsWith("models/")?model:`models/${model}`,prompt:prompts[i],
        config:{numberOfImages:1,outputMimeType:"image/jpeg",personGeneration:"allow_adult",aspectRatio:aspectRatio||"16:9"}});
      const gen=resp?.generatedImages?.[0];
      if(!gen){log(`⚠️  No image for prompt ${i+1}`);}
      else{
        const buf=Buffer.from(gen.image.imageBytes,"base64");
        const img=await Jimp.read(buf);img.resize(w,h);
        const resized=await img.getBufferAsync(Jimp.MIME_JPEG);
        fs.writeFileSync(path.join(IMG_DIR,filename),resized);
        const _f={file:filename,url:`/imgs/${encodeURIComponent(filename)}`};
        job.files.push(_f);
        try{ const a=await persistAsset({tenantId,userId,jobId,assetType:"image",sourceJobType:"imagen",filename,buffer:resized,contentType:"image/jpeg",metadata:{model,width:w,height:h,prompt:prompts[i]}});
             if(a){_f.key=a.key;_f.assetId=a.id;} }
        catch(e){ log(`⚠️  R2 persist failed: ${String(e.message||e).slice(0,80)}`); }
        log(`✅ ${filename}`);
      }
      if(PAUSE_MODELS.includes(model)&&(i+1)%20===0&&i+1<prompts.length){log("⏸️  Pausing 90s…");await new Promise(r=>setTimeout(r,90000));log("▶️  Resuming");}
      else await new Promise(r=>setTimeout(r,500));
    }catch(e){log(`🔴 ${String(e).slice(0,80)}`);await new Promise(r=>setTimeout(r,3000));}
    job.progress=i+1;await flush();
  }
  job.status="done";log("✅ Complete");
  await flush();
  await completeJob(jobId, { files: job.files, total: job.total });
  try{ await logUsage(tenantId, userId, model, "image", 0, 0, calcImageCost(model, (job.files||[]).length), null, "gemini"); }
  catch(e){ console.error("[imagen] logUsage failed:", e.message); }
}
app.get("/api/imagen/jobs", requireAuth, async(req,res)=>{
  try{
    const rows = await listJobs(resolveTenantId(req), "imagen");
    res.json(await Promise.all(rows.map(async r => ({
      jobId:r.id, status:r.status, model:(r.result_payload?.model)||null,
      outputPrefix:r.output_prefix, total:(r.result_payload?.total)||0,
      files: await signFiles(r.result_payload?.files), createdAt:r.created_at
    }))));
  }catch(e){ res.status(500).json({error:e.message}); }
});
app.get("/api/imagen/files", (_,res)=>{try{res.json(fs.readdirSync(IMG_DIR).filter(f=>/\.jpe?g$/i.test(f)).map(f=>({file:f,url:`/imgs/${encodeURIComponent(f)}`})));}catch{res.json([]);}});
app.get("/api/imagen/job/:id", requireAuth, async(req,res)=>{
  try{
    const live = await getLiveJob(req.params.id);
    if(live){ live.files = await signFiles(live.files); return res.json(live); }
    const row = await getJob(resolveTenantId(req), req.params.id);
    if(row) return res.json({
      status:row.status,
      progress:(row.result_payload?.total)||0,
      total:(row.result_payload?.total)||0,
      files: await signFiles(row.result_payload?.files),
      logs:[]
    });
    res.status(404).json({error:"Not found"});
  }catch(e){ res.status(500).json({error:e.message}); }
});
app.post("/api/imagen/start", requireAuth, async(req,res)=>{
  const{apiKey="",model="imagen-4.0-generate-001",prompts=[],outputPrefix="imagen",aspectRatio="16:9",resolution="1920x1080"}=req.body||{};
  const key=apiKey||GEMINI_KEY;if(!key)return res.status(400).json({error:"No API key"});
  if(!prompts.length)return res.status(400).json({error:"No prompts"});
  const tenantId = resolveTenantId(req);
  const userId   = await resolveUserId(req, tenantId);
  const jobId = await createJob(tenantId, userId, "imagen", outputPrefix);
  await setLiveJob(jobId, { jobId, model, outputPrefix, status:"queued",
    total:prompts.length, progress:0, logs:[], files:[], createdAt:new Date().toISOString() });
  await patchJobPayload(jobId, { model, total:prompts.length });
  runImagenJob(jobId,{tenantId,userId,apiKey:key,model,prompts,outputPrefix,aspectRatio,resolution})
    .catch(async e=>{ await failJob(jobId, e.message);
      await updateLiveJob(jobId,(j)=>{j.status="error";(j.logs=j.logs||[]).push("Fatal:"+e.message);return j;}); });
  res.json({ok:true,jobId,total:prompts.length});
});



// ══════════════════════════════════════════════════════════════════════════════
// CHAT / GOOGLE  (direct @google/genai streaming — uses GEMINI_API_KEY)
// ══════════════════════════════════════════════════════════════════════════════
app.post("/api/chat/google", async (req, res) => {
  res.setHeader("Content-Type",  "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection",    "keep-alive");

  const FILE_OUTPUT_INSTRUCTION = `\n\n## File Output\nWhen the user asks you to generate a file (code, config, document, CSV, JSON, script, etc.), wrap the file content in a \`<file>\` XML tag so the frontend can offer it as a download:\n\`\`\`\n<file name="example.py" mime="text/x-python">\nprint("hello world")\n</file>\n\`\`\`\nRules:\n- \`name\` = suggested filename (with extension).\n- \`mime\` = MIME type.\n- Content inside the tag is the raw file body — no extra markdown fences.\n- You may emit multiple \`<file>\` blocks in one response.\n- You can still include normal explanation text outside the tags.\n- Only use \`<file>\` when the user wants a downloadable artifact; short inline code snippets do NOT need the tag.`;

  try {
    const { message="", model="gemini-2.5-flash", system="", history=[], temperature=1.0, thinkingLevel="", google_api_key="", max_tokens=8192, images=[], sessionId="" } = req.body || {};
    // Client key takes priority; fallback to server .env
    const effectiveKey = google_api_key.trim() || GEMINI_KEY;
    if (!effectiveKey) return res.status(400).json({ error: "No Gemini API key — set GEMINI_API_KEY in .env or enter a key in the UI" });
    console.log(`[chat/google] key=${google_api_key.trim()?"CLIENT("+google_api_key.trim().slice(-6)+")":"SERVER_ENV"} model=${model} images=${images.length}`);
    const chatAI = mkAI(effectiveKey);

    // ── Tenant + user resolution (Clerk, falls back to dev IDs) ──────────
    const tenantId = resolveTenantId(req);
    const userId   = await resolveUserId(req, tenantId);   // real users.id UUID | null
    // Normalise sessionId: use provided value or derive one from tenantId+timestamp
    const sid = sessionId || _uuid5(`google-session-${tenantId}-${Date.now()}`);

    // ── Upsert chat session before streaming ──────────────────────────────
    try {
      await getOrCreateSession(tenantId, userId, sid, model, system);
    } catch (dbErr) {
      console.error("[chat/google] getOrCreateSession error:", dbErr.message);
    }

    // Build current user-turn parts: text + any inline images
    const userParts = [{ text: message }];
    for (const img of images) {
      if (img && img.b64 && img.mime) {
        userParts.push({ inlineData: { mimeType: img.mime, data: img.b64 } });
      }
    }

    // history: [{role:"user"|"assistant", content:"..."}]
    const contents = [
      ...history.map(h => ({
        role: h.role === "assistant" ? "model" : "user",
        parts: [{ text: h.content || "" }]
      })),
      { role: "user", parts: userParts }
    ];

    const config = { temperature, maxOutputTokens: max_tokens };
    if (system.trim()) config.systemInstruction = system + FILE_OUTPUT_INSTRUCTION;
    else config.systemInstruction = FILE_OUTPUT_INSTRUCTION;
    if (thinkingLevel && model.startsWith("gemini-3")) {
      config.thinkingConfig = { thinkingLevel };
    }

    const stream = await chatAI.models.generateContentStream({ model, contents, config });

    let inputTokens = 0, outputTokens = 0;
    const replyChunks = [];
    for await (const chunk of stream) {
      if (res.writableEnded) break;
      const t = chunk.text || "";
      if (t) {
        replyChunks.push(t);
        const encoded = t.replace(/\\/g, '\\\\').replace(/\n/g, '\\n');
        res.write(`data: ${encoded}\n\n`);
      }
      if (chunk.usageMetadata) {
        inputTokens  = chunk.usageMetadata.promptTokenCount     || inputTokens;
        outputTokens = chunk.usageMetadata.candidatesTokenCount || outputTokens;
      }
    }
    if (!res.writableEnded) {
      if (inputTokens || outputTokens)
        res.write(`data: [USAGE:${JSON.stringify({input:inputTokens,output:outputTokens})}]\n\n`);
      res.write("data: [DONE]\n\n");
      res.end();
    }

    // ── Persist messages + usage after stream closes ──────────────────────
    if (replyChunks.length) {
      const reply    = replyChunks.join("");
      const cost     = calcGoogleCost(model, inputTokens, outputTokens);
      const storedUser = images.length
        ? `${message}\n\n[Attached ${images.length} image(s)]`
        : message;
      try {
        await appendMessage(tenantId, sid, "user",      storedUser, model);
        await appendMessage(tenantId, sid, "assistant", reply,      model, inputTokens, outputTokens);
        await logUsage(tenantId, userId, model, "chat",
                       inputTokens, outputTokens, cost, sid, "gemini");
      } catch (dbErr) {
        console.error("[chat/google] DB persist error:", dbErr.message);
      }
    }
  } catch(e) {
    if (!res.writableEnded) { res.write(`data: [ERROR: ${e.message}]\n\n`); res.end(); }
  }
});

// ══════════════════════════════════════════════════════════════════════════════
// GOOGLE-NATIVE IMAGE GEN  (direct @google/genai — uses GEMINI_API_KEY)
// ══════════════════════════════════════════════════════════════════════════════
const GOOGLE_IMG_MODELS = ["imagen-4.0-fast-generate-001","imagen-4.0-generate-001","imagen-4.0-ultra-generate-001"];

app.post("/api/generate-image/google", async (req,res) => {
  try {
    const { prompt, model="imagegeneration@006", aspect_ratio="16:9", nusantara_corpus=false, ref_image_b64, ref_image_mime } = req.body || {};
    if (!prompt) return res.status(400).json({ error:"prompt required" });
    console.log(`[generate-image/google→vertex] model=${model} corpus=${nusantara_corpus} ref=${ref_image_b64?"yes":"no"}`);
    const r = await fetch(`${PYTHON_API}/generate-image/vertex`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ prompt, model, aspect_ratio, nusantara_corpus, ...(ref_image_b64 && { ref_image_b64, ref_image_mime: ref_image_mime || "image/jpeg" }) }),
    });
    const data = await r.json();
    if (!r.ok) return res.status(r.status).json(data);
    if (data.image_b64) await captureImageFlow(req, model, "generate_image", [data.image_b64], "vertex", prompt);
    res.json(data);
  } catch(e) { res.status(500).json({ error: e?.message || String(e) }); }
});

app.get("/api/generate-image/google/models", (_,res) => res.json(GOOGLE_IMG_MODELS));

// ── Admin: re-embed the Nusantara corpus into Qdrant (OAuth, no GEMINI key) ───
// Behind Clerk auth (the /api guard) + the X-Reembed-Secret the Python service checks.
app.post("/api/corpus/reembed", async (req,res) => {
  try {
    const r = await fetch(`${PYTHON_API}/corpus/reembed`, {
      method:"POST",
      headers:{ "Content-Type":"application/json", "X-Reembed-Secret": req.get("X-Reembed-Secret") || "" },
    });
    const data = await r.json();
    res.status(r.status).json(data);
  } catch(e) { res.status(500).json({ error: e?.message || String(e) }); }
});

// ── Google-native Whisk (Gemini multimodal → image generation) ───────────────
app.post("/api/whisk/google", async (req,res) => {
  if (!ai) return res.status(400).json({ error:"No GEMINI_API_KEY in .env" });
  try {
    const { subject_image_b64, subject_description="", subject_image_mime="image/jpeg",
            scene_image_b64,   scene_description="",   scene_image_mime="image/jpeg",
            style_image_b64,   style_description="",   style_image_mime="image/jpeg",
            aspect_ratio="1:1" } = req.body || {};

    // Build multimodal parts
    const parts = [];
    if (subject_image_b64) { parts.push({ inlineData:{ data:subject_image_b64, mimeType:subject_image_mime } }); }
    if (subject_description.trim()) { parts.push({ text:`Subject: ${subject_description}` }); }
    if (scene_image_b64)   { parts.push({ inlineData:{ data:scene_image_b64,   mimeType:scene_image_mime   } }); }
    if (scene_description.trim())   { parts.push({ text:`Scene: ${scene_description}` }); }
    if (style_image_b64)   { parts.push({ inlineData:{ data:style_image_b64,   mimeType:style_image_mime   } }); }
    if (style_description.trim())   { parts.push({ text:`Style: ${style_description}` }); }
    if (!parts.length) return res.status(400).json({ error:"At least one slot required" });
    parts.push({ text:`Create a single creative image that blends the subject, scene, and style above. Aspect ratio: ${aspect_ratio}. Return the image only.` });

    const resp = await ai.models.generateContent({
      model: "models/gemini-2.0-flash-exp",
      contents: [{ role:"user", parts }],
      config: { responseModalities:["IMAGE","TEXT"] }
    });
    const imgPart = resp?.candidates?.[0]?.content?.parts?.find(p => p.inlineData?.data);
    if (!imgPart) throw new Error("Gemini returned no image — try LaoZhang mode");
    await captureImageFlow(req, "gemini-2.0-flash-exp", "whisk", [imgPart.inlineData.data]);
    res.json({ image_b64: imgPart.inlineData.data });
  } catch(e) { res.status(500).json({ error: e?.message || String(e) }); }
});

// ── Google-native Flow storyboard images (Imagen 4 fast per scene) ───────────
// GOOGLE-NATIVE FLOW STORYBOARD TEXT (uses GEMINI_API_KEY for scene generation)
app.post("/api/flow/storyboard/google/text", async (req,res) => {
  try {
    const { script="", style="cinematic", scene_count=4, chat_model="gemini-2.5-flash", scene_offset=0, total_scenes=0, image_style="", google_api_key="" } = req.body || {};
    const effectiveKey = google_api_key.trim() || GEMINI_KEY;
    if (!effectiveKey) return res.status(400).json({ error:"No Gemini API key — set GEMINI_API_KEY in .env or enter a key in the UI" });
    console.log(`[flow/text] key=${google_api_key.trim()?"CLIENT("+google_api_key.trim().slice(-6)+")":"SERVER_ENV"} model=${chat_model}`);
    const flowTextAI = mkAI(effectiveKey);
    if (!script.trim()) return res.status(400).json({ error:"script required" });
    const total = (total_scenes && total_scenes > scene_count) ? total_scenes : scene_count;
    const start = scene_offset + 1, end = scene_offset + scene_count;
    const scope = total > scene_count
      ? `This script is being broken into exactly ${total} cinematic scenes total. Generate ONLY scenes ${start} to ${end} (${scene_count} scenes) as a coherent part of that sequence — they must flow naturally from the overall narrative.`
      : `Break the given script into exactly ${scene_count} cinematic scenes.`;
    const styleClause = image_style ? ` Visual render style: ${image_style}.` : "";
    const systemPrompt = [
      `You are a professional cinematographer and storyboard artist.`,
      `${scope} Use a ${style}-style narrative.${styleClause}`,
      `ALL field values must be written in English regardless of the script language.`,
      ``,
      `Return ONLY a valid JSON array of exactly ${scene_count} objects. Each object must have these keys:`,
      `- index: integer, 0-based position within this batch`,
      `- title: string, 5-8 words in English`,
      `- description: string, 60-90 words. Rich English visual prompt for AI image/video generation. Read the script carefully and faithfully extract the SPECIFIC location, geography, time period, and environmental details. Give equal weight to the landscape, environment, and atmosphere as to any human subject — for historical, geographic, or nature-focused scenes the landscape IS the primary subject. Cover: specific setting and environment, any human presence and action, lighting quality and direction, color palette, mood and atmosphere${image_style ? `, rendered in ${image_style} visual style` : ""}. Write in vivid evocative language. No quote characters inside this string.`,
      `- camera: string. Full technical camera note covering shot size, angle, movement and lens character. English only. No quote characters.`,
      `- audio: string. Structured SFX and sound design prompt layered foreground to background. Label layers with [FG] [MID] [SCORE] [BG]. English only. No quote characters.`,
      `- duration: integer, either 5 or 8`,
      `- start_kalimat: the exact opening sentence or phrase (8-15 words) from the ORIGINAL script text that this scene is directly based on. Copy verbatim from the script, preserving the original language.`,
      ``,
      `CRITICAL: Output pure JSON only. No markdown, no code fences, no explanation. All string values must be properly JSON-escaped.`
    ].join("\n");
    const userPrompt = `Script:\n\n${script}`;
    const resp = await flowTextAI.models.generateContent({
      model: chat_model.startsWith("models/") ? chat_model : `models/${chat_model}`,
      contents: [{ role:"user", parts:[{text: userPrompt}] }],
      config: { systemInstruction: systemPrompt, responseMimeType:"application/json" }
    });
    let raw = resp?.candidates?.[0]?.content?.parts?.[0]?.text || "[]";
    // Strip markdown code fences if present despite responseMimeType
    raw = raw.replace(/^```(?:json)?\s*/m, "").replace(/\s*```\s*$/m, "").trim();
    let scenes;
    try {
      scenes = JSON.parse(raw);
    } catch {
      const m = raw.match(/\[[\s\S]+?\]/);
      try { scenes = JSON.parse(m?.[0] || "[]"); } catch { scenes = []; }
    }
    if (!Array.isArray(scenes)) scenes = [];
    try{
      const _um=resp?.usageMetadata||{}, _ti=_um.promptTokenCount||0, _to=_um.candidatesTokenCount||0;
      const _t=resolveTenantId(req), _u=await resolveUserId(req,_t);
      await logUsage(_t,_u,chat_model,"chat",_ti,_to,calcGoogleCost(chat_model,_ti,_to),null,"gemini");   // scene-split text → bill tokens + charge
    }catch(_){}
    res.json({ scenes, style, scene_count: scenes.length });
  } catch(e) { res.status(500).json({ error: e?.message || String(e) }); }
});

app.post("/api/flow/storyboard/google", async (req,res) => {
  try {
    const { scenes=[], aspect_ratio="16:9", model="imagen-4.0-fast-generate-001", image_style="", google_api_key="" } = req.body || {};
    const effectiveKey = google_api_key.trim() || GEMINI_KEY;
    if (!effectiveKey) return res.status(400).json({ error:"No Gemini API key — set GEMINI_API_KEY in .env or enter a key in the UI" });
    if (!scenes.length) return res.status(400).json({ error:"scenes required" });
    console.log(`[flow/images] key=${google_api_key.trim()?"CLIENT("+google_api_key.trim().slice(-6)+")":"SERVER_ENV"} model=${model} scenes=${scenes.length}`);
    const flowImgAI = mkAI(effectiveKey);
    const mdl = model.startsWith("models/") ? model : `models/${model}`;
    const styleSuffix = image_style ? ` ${image_style} style.` : "";
    const results = await Promise.allSettled(scenes.map(async (s,i) => {
      const prompt = `${s.description || s.title}. Camera: ${s.camera||""}.${styleSuffix} Cinematic still frame.`;
      const resp = await flowImgAI.models.generateImages({ model:mdl, prompt,
        config:{ numberOfImages:1, outputMimeType:"image/jpeg", aspectRatio:aspect_ratio } });
      const data = resp?.generatedImages?.[0]?.image?.imageBytes;
      return { index:i, image_b64: data||"" };
    }));
    const images = results.map((r,i) => r.status==="fulfilled" ? r.value : { index:i, image_b64:"" });
    await captureImageFlow(req, model, "flow_image", images.map(im=>im.image_b64), "gemini", (scenes||[]).map(s=>s.description||s.title||""));
    res.json({ images });
  } catch(e) { res.status(500).json({ error: e?.message || String(e) }); }
});

// Video assembly engine (Step 6): enqueue-only API surface. The heavy work runs
// in the separate worker process (backend/video/worker-entry.mjs) — these routes
// only add BullMQ jobs + read state, so ffmpeg never touches the API event loop.
mountVideoRoutes(app, { requireAuth, resolveTenantId, resolveUserId });

// Step 3: Sentry Express error handler — after all routes, before listen.
if (SENTRY_ON) {
  Sentry.setupExpressErrorHandler(app);
}

const server = app.listen(PORT,()=>{ console.log(`🎬 Cerita AI Studio :${PORT}`); console.log(`   Gemini: ${GEMINI_KEY?"set ✅":"MISSING ⚠️"}`); console.log(`   Python: ${PYTHON_API}`); });
// Vault retention: daily sweep + once shortly after boot (tier-based, see cleanup_expired_assets).
setInterval(runAssetRetention, 24*60*60*1000);
setTimeout(runAssetRetention, 60*1000);
// Prevent blank screen on long-running requests (storyboard 26+ scenes with images)
server.timeout = 660000;          // 11 min — must be > storyboard AbortController (10 min)
server.keepAliveTimeout = 660000;
server.headersTimeout = 661000;

// ── Step 6: optionally run the BullMQ video workers IN THIS process ──────────
// When a dedicated video-worker service isn't available (e.g. a single-service
// deploy), set VIDEO_INPROCESS_WORKER=1 to run audio/visual/check/stitch here.
// ffmpeg/generation then run in the API container — fine for small/low-traffic
// deploys; split to a separate process (VIDEO_ROLE=worker) when scale demands.
if (process.env.VIDEO_INPROCESS_WORKER === "1") {
  (async () => {
    try {
      const { markVideoWorker } = await import("./video/runtime.mjs");
      const { startWorkers, makeDeps } = await import("./video/workers.mjs");
      const { httpGenerationClient } = await import("./video/generationClient.mjs");
      if (!process.env.INTERNAL_SERVICE_SECRET) {
        console.warn("[video] in-process workers WITHOUT INTERNAL_SERVICE_SECRET — per-scene calls will be unmetered + no RLS");
      }
      markVideoWorker();   // lift the ffmpeg guard for this process
      startWorkers(makeDeps({ generationClient: httpGenerationClient() }));
      console.log("[video] in-process BullMQ workers started (VIDEO_INPROCESS_WORKER=1)");
      try {
        const storage = await import("./storage.mjs");
        if (storage.isConfigured?.()) storage.ensureVideoLifecycle().catch(() => {});
      } catch {}
    } catch (e) {
      console.error("[video] in-process worker start failed:", e?.message || e);
    }
  })();
}
