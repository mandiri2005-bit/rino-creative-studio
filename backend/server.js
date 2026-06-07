/**
 * Rino Creative Studio — unified backend
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
         createJob, updateJobProgress, completeJob, failJob, getJob, listJobs, findJobByJobName, patchJobPayload } from "./db.js";
import { clerkMiddleware, requireAuth, getUserId } from "./auth.js";
import { Webhook } from "svix";
import { pool } from "./db.js";
import { setLiveJob, getLiveJob, updateLiveJob, pushLiveLog, delLiveJob } from "./redis.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT        = process.env.PORT        || 3000;
const PYTHON_API  = process.env.PYTHON_API_URL || "http://127.0.0.1:8000";
const MCP_API     = process.env.MCP_API_URL    || "http://127.0.0.1:8001";
const GEMINI_KEY  = process.env.GEMINI_API_KEY || "";

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

// ── WAV & batch helpers imported from utils.mjs ──

// ── Express ───────────────────────────────────────────────────────────────────
const app = express();
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
        const provRes = await pool.query(
          `SELECT provision_tenant($1,$2,$3,$4,$5,$6,'admin') AS tenant_id`,
          [null, displayName || email.split("@")[0], slug, email, "free", data.id]
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

app.use(express.json({ limit:"200mb" })); // storyboard 26 scenes+images can be 5-20MB

app.use(clerkMiddleware()); // Clerk — must be after body-parser, before protected routes
app.use("/images", express.static(OUTPUT_DIR, {maxAge:"1h"}));
app.use("/audio",  express.static(TTS_DIR,    {maxAge:"1h"}));
app.use("/imgs",   express.static(IMG_DIR,    {maxAge:"1h"}));
app.use(express.static(path.join(__dirname,"public")));

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
      method:"POST", headers:{"Content-Type":"application/json",...(lzk2&&{"X-LaoZhang-API-Key":lzk2})},
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
app.post("/api/save",        async(req,res)=>{ try{res.json(await(await fetch(`${PYTHON_API}/save`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(req.body)})).json());}catch(e){res.status(500).json({error:e.message});} });
app.delete("/api/session/:id",async(req,res)=>{ try{const _hd={...(req.headers["authorization"]&&{"Authorization":req.headers["authorization"]})};await fetch(`${PYTHON_API}/session/${req.params.id}`,{method:"DELETE",headers:_hd});res.json({status:"cleared"});}catch(e){res.status(500).json({error:e.message});} });
app.get("/api/models",       async(req,res)=>{ try{res.json(await(await fetch(`${PYTHON_API}/models`)).json());}catch(e){res.status(500).json({error:e.message});} });

// File upload for chat context
app.post("/api/upload", upload.single("file"), async(req,res)=>{
  if(!req.file) return res.status(400).json({error:"No file"});
  try{
    const form=new FormData();
    form.append("file",req.file.buffer,{filename:req.file.originalname,contentType:req.file.mimetype});
    const pyRes=await fetch(`${PYTHON_API}/upload`,{method:"POST",body:form.getBuffer(),headers:{...form.getHeaders(),"Content-Length":String(form.getLengthSync())}});
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
      headers:{"Content-Type":"application/json","X-Image-API-Key": lzKey},
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
    for (const h of ["x-image-api-key","X-Image-API-Key","x-laozhang-api-key","X-LaoZhang-API-Key"]) {
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
app.post("/api/narasi/review", async (req,res) => {
  try {
    const lzk = req.headers["x-laozhang-api-key"] || "";
    const pyRes = await fetch(`${PYTHON_API}/narasi/review`, {
      method:"POST",
      headers:{"Content-Type":"application/json",...(lzk&&{"X-LaoZhang-API-Key":lzk})},
      body: JSON.stringify(req.body),
    });
    if (!pyRes.ok) { const e = await pyRes.json().catch(()=>({error:pyRes.statusText})); return res.status(pyRes.status).json({error:e.error||e.detail||pyRes.statusText}); }
    res.json(await pyRes.json());
  } catch(e) { res.status(500).json({error:e.message}); }
});
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
app.get("/api/narasi/jobs", (req,res)=>pyProxy(req,res,"/narasi/jobs"));
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
      const langLabel=lang==="id"?"Bahasa Indonesia":"English";
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

// ── Style rules (mirrors laozhang_api.py STYLE_RULES) ─────────────────────
const NARASI_STYLE_RULES_JS={
  "creative non-fiction":`STYLE: Creative Non-Fiction
= Techniques of fiction (concrete scenes, specific POV, sensory detail) applied to REAL FACTS.

STRUCTURE PER CHAPTER:
1. COLD OPEN — One specific cinematic scene. Specific object, person, moment — NOT abstract.
   BAD: "Para leluhur membawa harapan ke cakrawala."
   GOOD: "Di geladak sempit: benih padi dibungkus daun pisang, seekor babi betina bunting diikat di tiang."
2. UNTOLD STORY — The fact most people don't know. Specific data: %, dates, species names, site names.
3. SUDUT PANDANG — At least one scene from a specific character's human POV.

FORBIDDEN: "harapan", "keberanian", "gema purba", "kita adalah kelanjutan mereka", "penjelajah tak gentar"
REQUIRED: Min 2 specific facts with numbers/dates per section. 1 concrete object/sensory detail per paragraph.`,

  "storytelling":`STYLE: Storytelling — Narrative Drama
= Story-first. Every historical fact must be delivered through SCENE and CHARACTER, not exposition.

STRUCTURE PER CHAPTER:
1. SCENE OPENER — Drop into the middle of a moment. In medias res. Who, what, where — in the first sentence.
2. CONFLICT/TENSION — Every chapter needs a problem or stakes.
3. DIALOGUE — At least 2 lines of spoken dialogue per chapter.
4. TURN — A moment where something changes: a realization, a surprise, a decision.

FORBIDDEN: Passive summary of events. Telling emotion instead of showing. Generic descriptions.
REQUIRED: Named or clearly characterized figures. Cause-and-effect within scenes. Physical action.`,

  "bedtime story":`STYLE: Bedtime Story — Gentle, Soothing
= Warm narrator voice, gentle wonder, age-appropriate vocabulary.

STRUCTURE PER CHAPTER:
1. SOFT OPENING — Begin with a peaceful image or a gentle question. No drama, no conflict.
2. SENSE OF WONDER — Each chapter reveals one amazing thing as a gift, not a lesson.
3. COMFORTING CLOSE — End with warmth. A sense that things turned out okay.

FORBIDDEN: Violence, conflict, darkness. Complex syntax. Academic jargon.
REQUIRED: Short sentences. Soft vocabulary. Metaphors from nature and everyday life.`,

  "harari":`STYLE: Harari / Jared Diamond — Big History (Sapiens-style)
= Claim → Evidence → Implication. Zoom from the specific to the cosmic.
TONE: Interdisciplinary, analytical, slightly provocative, intellectually fair.
LANGUAGE: English. Topic can be ANY historical/civilizational subject.

STRUCTURE PER CHAPTER:
1. OPENING — Rotate types. NEVER use "Imagine" as first word more than once per book.
   TYPE A — Direct reversal of common belief.
   TYPE B — Specific paradox with named evidence and date.
   TYPE C — Bold historical verdict a scholar could argue with.
   TYPE D — Cognitive/evolutionary hook about human nature.

2. EVIDENCE STACK — Min 2 named researchers + dates. Min 2 quantified data points.
   SCHOLARLY TENSION — mandatory: name one counter-theory or scholarly debate.
   Acknowledge what is NOT yet known.

3. COMPARATIVE LENS — mandatory, intellectually fair.
   FORBIDDEN: Claiming one civilization was "smarter" or "braver" than another.
   REQUIRED: Explain differences through geography, ecology, or resource constraints.

4. IMPLICATION — vary framing every chapter. NEVER repeat "This is the great lesson for all of humanity."
   End with a bridge: 1–2 sentences opening the next tension.

ADVANCED MECHANICS (all mandatory):
5. MICRO-TO-MACRO ZOOM — anchor in one microscopic/mundane detail before going cosmic.
6. SHARED FICTION FRAME — frame human institutions as "imagined realities" or "collective fictions."
7. SENSORY ANCHOR — translate one data point into a lived, sensory prehistoric human experience.
8. PUNCHLINE RULE — follow one long complex sentence with a brutal 3–6 word verdict.
9. HISTORICAL CONTINGENCY — deny the reader the comfort of destiny. Acknowledge the role of chance.
10. COGNITIVE THREAT — one realization that destabilizes a modern assumption about civilization or progress.

BANNED PHRASES: "Throughout history", "It is important to note", "This suggests that", "In many ways",
"Scholars have long debated", "Since the dawn of time", "It is worth noting", "One cannot help but wonder"

ANTI-HEDGING: Avoid stacking: "perhaps", "possibly", "arguably", "may have". Use uncertainty only when evidence requires it.

ABSOLUTE FORBIDDEN:
- "Imagine" as chapter opener more than once in the entire book
- "This is the great lesson for all of humanity"
- Passive heroism: "brave/fearless/spirited ancestors"
- Comfortable unearned conclusions

REQUIRED ONCE PER CHAPTER:
- One bold claim a scholar could disagree with
- One named counter-theory bridging two disciplines
- One quantified data point
- One fresh implication framing
- One Micro-to-Macro zoom
- One Sensory Anchor
- One Punchline Rule moment
- One chapter bridge`,

  "pov":`STYLE: POV — First Person Immersive
= You ARE the historical figure. First person, present tense, immediate sensory experience.

STRUCTURE PER CHAPTER:
1. IMMEDIATE SENSORY OPENING — First sentence places reader in a body, in a moment.
2. INNER MONOLOGUE — Thoughts, fears, calculations.
3. SPECIFIC OBSERVATION — What do I see/hear/smell/touch that reveals historical context?
4. DECISION OR ACTION — The POV character does or decides something that moves history.

FORBIDDEN: Third person. Omniscient narrator intrusions. Modern sensibility projected onto ancient figure.
REQUIRED: Present tense throughout. Specific sensory details — not abstract emotions.`,

  "national geographic":`STYLE: National Geographic Documentary
= Science anchored in beauty. Every fact arrives inside a visual, environmental description.

STRUCTURE PER CHAPTER:
1. LANDSCAPE SHOT — Open with the physical environment as it looks/feels/smells.
2. ZOOM TO SUBJECT — From landscape to a specific creature, artifact, or human activity.
3. SCIENTIFIC EXPLANATION — The "how does this work" in accessible, precise language.
4. CONSERVATION/SIGNIFICANCE FRAME — Why does this matter today?

FORBIDDEN: Vague wonder without specificity. Human-centric framing that ignores ecology.
REQUIRED: Species names, geological terms, GPS-level location specificity. Present tense for ongoing phenomena.`,

  "youtube":`STYLE: YouTube — Popular Science
= Hook in first sentence. Curiosity loops. Reframe what viewer thinks they know.

STRUCTURE PER CHAPTER:
1. HOOK — First sentence must be a question, surprising fact, or counterintuitive claim.
2. SETUP THE MYSTERY — What's the weird thing we're about to explain?
3. EXPLAIN WITH ANALOGY — One modern analogy per complex concept.
4. PAYOFF + REFRAME — Answer the question, then add what that means for today.

FORBIDDEN: Academic tone. Passive voice. Long blocks without a hook or punchline.
REQUIRED: Short punchy sentences mixed with longer ones. Direct address. At least one modern analogy.`,

  "journalistic":`STYLE: Journalistic — Long Form
= Report the past like a journalist covering a breaking story.

STRUCTURE PER CHAPTER:
1. LEAD — The most important/surprising fact first. Then context.
2. NUT GRAF — What is this chapter really about? Why does it matter?
3. SCENE + VOICE — At least one reconstructed scene + one quoted source.
4. MULTIPLE ANGLES — Show competing interpretations.

FORBIDDEN: Single narrative voice without tension. Unverified claims presented as fact.
REQUIRED: Attribution language. Present tense for dramatic reconstruction. Specific numbers and sources.`,

  "literary essay":`STYLE: Literary Essay
= Personal intellectual voice. Digressive. Thinking on the page, not presenting conclusions.

STRUCTURE PER CHAPTER:
1. PERSONAL/ASSOCIATIVE OPENING — Start with an observation or cultural reference that connects obliquely.
2. DIGRESSION — Follow one idea sideways before returning to the main thread.
3. COMPLEXITY — Resist simple conclusions. Show what we don't know. Sit with ambiguity.
4. RESONANT CLOSE — End not with a conclusion but a lingering image or open question.

FORBIDDEN: Thesis statements. Bullet-point logic. Authoritative declarations.
REQUIRED: First-person or intimate narrator voice. Cultural references. Sentences that think out loud.`,

  "podcast narrative":`STYLE: Podcast Narrative
= Written for the ear, not the eye. Conversational, signposted, built on spoken rhythm.

STRUCTURE PER CHAPTER:
1. CONVERSATIONAL HOOK — Address the listener directly. Short sentence.
2. SCENE — Tell a short story in present tense, as if recounting to a friend.
3. EXPLANATION — "And here's what's interesting..." — signpost the insight clearly.
4. LISTENER TAKEAWAY — End with what this means for the listener's worldview.

FORBIDDEN: Complex nested sentences. Dense data without analogies.
REQUIRED: Short sentences (max 20 words for key points). Signpost phrases. Rhythm that works read aloud.`,

  "academic popular":`STYLE: Academic Popular (like Sapiens)
= Big claim → evidence → implication. Accessible language for complex ideas.

STRUCTURE PER CHAPTER:
1. BOLD OPENING CLAIM — State the argument plainly. No hedging.
2. EVIDENCE STACK — 3–4 specific data points. Studies, sites, percentages.
3. THOUGHT EXPERIMENT — "Imagine if..." — use hypothetical to make abstract concrete.
4. IMPLICATION FOR TODAY — Connect past to present human behavior or society.

FORBIDDEN: Jargon without definition. Evidence without interpretation. Hedging that kills momentum.
REQUIRED: Footnote-worthy specifics in accessible language. Comparative lens. One thought experiment per chapter.`,

  "cinematic voiceover":`STYLE: Cinematic Voiceover
= Written for a narrator's voice over moving images. Short. Punchy. Visual. Rhythmic.

STRUCTURE PER CHAPTER:
1. VISUAL ESTABLISHING LINE — One sentence, one image. What is the camera seeing?
2. NARRATION IN SHORT BURSTS — 2–4 sentence paragraphs max.
3. EMOTIONAL BEAT — One moment of human connection. Brief.
4. TITLE CARD CLOSE — End with a short, quotable line. One sentence. Strikes like a title card.

FORBIDDEN: Long complex sentences. Explanatory exposition. Anything that can't be spoken in one breath.
REQUIRED: Present tense. Fragments allowed for rhythm. Visual-first, emotion-second.`
};

function _getStyleRulesJS(style){
  const sl=style.toLowerCase();
  for(const key of Object.keys(NARASI_STYLE_RULES_JS)){
    if(sl.includes(key)) return NARASI_STYLE_RULES_JS[key];
  }
  return NARASI_STYLE_RULES_JS["creative non-fiction"];
}

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
    const langLabel=lang==="id"?"Bahasa Indonesia":"English";

    if(action==="brief"){
      prompt=`You are writing a ${style} narrative titled: "${body.topic}"\nLanguage: ${langLabel}\n\nOutline:\n${body.outline||""}\n\nWrite a concise NARRATIVE BRIEF (max 300 words) covering: overall tone, voice, emotional arc, key themes, how chapters connect, recurring motifs.\nWrite in ${langLabel}. Return ONLY the brief text, no headings, no markdown.`;
      maxTok=1000;
    } else if(action==="chapter"){
      const c=body.chapter||{};
      // RAG: fetch Gutenberg passages from python-api
      let ragBlock="";
      if(body.use_rag){
        try{
          const ragResp=await fetch(`${PYTHON_API}/rag/context`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({topic:`${body.topic} — ${c.title}`,style,top_k:5})});
          const ragData=await ragResp.json();
          if(ragData.ok&&ragData.context_text){ragBlock=ragData.context_text+"\n";console.log(`[RAG] Google path: passages=${ragData.passages}`);}
        }catch(e){console.warn("[RAG] failed:",e.message);}
      }
      prompt=`You are writing Chapter ${c.id} of a ${style} narrative titled: "${body.topic}"\nLanguage: ${langLabel}\n\n`
        +ragBlock
        +_getStyleRulesJS(style)+"\n\n"
        +(body.brief?`NARRATIVE BRIEF:\n${body.brief}\n\n`:"")
        +(body.outline?`FULL OUTLINE:\n${body.outline}\n\n`:"")
        +`THIS CHAPTER:\n  Title: ${c.title}\n  Summary: ${c.description}\n  Target: ${body.word_target} words (range: ${body.word_min}–${body.word_max})\n\nWrite EXACTLY ${body.word_target} words. Do NOT include chapter title. Return ONLY the body text.`;
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

      // Anti-drift state
      let voiceSample="";
      let driftSignals=[];
      let prevOpeners=[];
      const totalChapters=chapters.length;

      for(let i=0;i<chapters.length;i++){
        const c=chapters[i];
        const wt=c.words||400;
        const wmin=Math.floor(wt*0.9),wmax=Math.ceil(wt*1.1);

        const styleRules=_getStyleRulesJS(style);
        const antidrift=_buildAntidriftBlock(i,totalChapters,voiceSample,driftSignals,prevOpeners,style);

        // Check cancel flag before each chapter
        if(googleCancelFlags.get(jobId)){
          console.warn(`[narasi-google] job ${jobId} cancelled at bab ${c.id}`);
          break;
        }
        // RAG: fetch Gutenberg passages for this chapter
        let chRagBlock="";
        if(body.use_rag){
          try{
            const rr=await fetch(`${PYTHON_API}/rag/context`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({topic:`${body.topic||""} — ${c.title}`,style,top_k:5})});
            const rd=await rr.json();
            if(rd.ok&&rd.context_text){chRagBlock=rd.context_text+"\n";console.log(`[RAG] Google multi-chapter bab=${c.id} passages=${rd.passages}`);}
          }catch(e){console.warn("[RAG] multi-chapter failed:",e.message);}
        }
        const cp=`You are writing Chapter ${c.id} of a ${style} narrative titled: "${body.topic||""}"
Language: ${langLabel}

${chRagBlock}${styleRules}
${antidrift}${body.brief?`NARRATIVE BRIEF:\n${body.brief}\n\n`:""}${body.outline?`FULL OUTLINE:\n${body.outline}\n\n`:""}THIS CHAPTER:
  Title: ${c.title}
  Summary: ${c.description||""}
  Target: ${wt} words (range: ${wmin}–${wmax})

Write EXACTLY ${wt} words (count carefully). Do NOT include chapter title. Return ONLY body text.`;

        try{
          const r2=await ai.models.generateContent({
            model,
            contents:[{role:"user",parts:[{text:cp}]}],
            config:{maxOutputTokens:65536},
            generationConfig:{maxOutputTokens:65536}
          });
          let ct=(r2.text||"").trim();
          let finishReason=r2.candidates?.[0]?.finishReason||"unknown";
          // Retry if empty or too short
          if(!ct||ct.split(/\s+/).filter(Boolean).length<50){
            console.warn(`[narasi-google] bab ${c.id} EMPTY/SHORT (${ct.split(/\s+/).filter(Boolean).length} words) -- retrying`);
            const r3=await ai.models.generateContent({
              model,contents:[{role:"user",parts:[{text:cp}]}],
              config:{maxOutputTokens:65536},
            generationConfig:{maxOutputTokens:65536}
            });
            if(r3.text&&r3.text.trim()){
              ct=r3.text.trim();
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
        }catch(e){
          errors.push({id:c.id,error:e.message});
          await fsp.writeFile(
            nodePath.join(tmpDir,`${c.id}.txt`),
            `## Bab ${c.id}: ${c.title}\n\n<!-- ERROR bab ${c.id}: ${e.message} -->\n`,
            "utf8"
          );
        }
      }
      return res.json({ok:true,job_id:jobId,errors,drift_signals_detected:driftSignals});

    } else {
      // ── Outline generation — plain text format (avoids JSON quote issues) ──
      const revise=body.revise_instruction&&body.current_outline;
      const outlineFmt=`Return ONLY a plain-text list, one chapter per line, using this EXACT format with pipe separators:\nID|TITLE|WORDS|DESCRIPTION\n\nExample:\n1|Prolog: Bayangan Raksasa|400|Memperkenalkan lanskap Jawa Tengah pada abad kedelapan dan surplus pertanian yang memungkinkan berdirinya peradaban kompleks.\n2|Geografi sebagai Takdir|500|Analisis bagaimana abu vulkanik Merapi menciptakan tanah subur yang menjadi fondasi ekonomi surplus.\n\nRules:\n- NO pipes inside TITLE or DESCRIPTION fields — rephrase if needed\n- WORDS must be integers only\n- Total words must sum to ${body.word_min}–${body.word_max}\n- Deeper/climactic chapters get MORE words; intro/epilog get FEWER\n- NO markdown, NO JSON, NO fences, NO extra lines`;
      if(revise){
        prompt=`Revise this narrative outline for: "${body.topic}"\nStyle: ${style} | Language: ${langLabel}\n\nCURRENT OUTLINE:\n${body.current_outline}\n\nREVISION INSTRUCTION: ${body.revise_instruction}\n\n${outlineFmt}`;
      } else {
        prompt=`Create a narrative outline for a ${style} narrative titled: "${body.topic}"\nLanguage: ${langLabel} | Chapters: EXACTLY ${chapCount} (no more, no less)\n\nCRITICAL: Return EXACTLY ${chapCount} pipe-delimited line(s). Do NOT split into extra Pembuka/Isi/Penutup entries beyond the ${chapCount} total.\n\nWORD WEIGHTS: Do NOT divide equally. Climactic chapters get MORE words. Intro/conclusion get FEWER. Total must sum to ${body.word_min}–${body.word_max}.\n\n${outlineFmt}`;
      }
    }

    const result=await ai.models.generateContent({model,contents:[{role:"user",parts:[{text:prompt}]}],config:{maxOutputTokens:maxTok}});
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
    res.json({ok:true,transcript:text,paragraphs,count:paragraphs.length});
  }catch(e){res.status(500).json({error:e.message});}
});
app.post("/api/flow/images/lz",          (req,res)=>pyProxy(req,res,"/flow/images"));

// ══════════════════════════════════════════════════════════════════════════════
// VEO  (proxy → Python)
// ══════════════════════════════════════════════════════════════════════════════
app.post("/api/veo/submit",       (req,res)=>pyProxy(req,res,"/veo/submit"));
app.get ("/api/veo/status/:id",   (req,res)=>pyProxy(req,res,`/veo/status/${req.params.id}`));
app.get ("/api/veo/download/:id", async(req,res)=>{
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
  try{
    const headers={}; if(req.headers["x-veo-api-key"]) headers["X-Veo-API-Key"]=req.headers["x-veo-api-key"];
    const pyRes=await fetch(`${PYTHON_API}/veo/stream/${req.params.id}`,{headers});
    if(!pyRes.ok){return res.status(pyRes.status).json({error:await pyRes.text()});}
    res.setHeader("Content-Type","video/mp4");res.setHeader("Cache-Control","no-store");
    res.end(Buffer.from(await pyRes.arrayBuffer()));
  }catch(e){if(!res.writableEnded)res.status(500).json({error:e.message});}
});

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
      const outName=`${filename}.png`;fs.writeFileSync(path.join(OUTPUT_DIR,outName),Buffer.from(data,"base64"));
      files.push({key,file:outName});saved++;
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
async function runLaozhangTtsJob(jobId,{tenantId,apiKeys,model,voice,speed,language,audiobookMode,silenceSeconds,audioProfile,transcriptBody,outputPrefix}){
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
      job.files.push({file:filename,url:`/audio/${encodeURIComponent(filename)}`});
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
}

async function runTtsJob(jobId,{tenantId,apiKeys,model,voice,silenceSeconds,audioProfile,transcriptBody,outputPrefix}){
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
        job.files.push({file:filename,url:`/audio/${encodeURIComponent(filename)}`});
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
}
app.get("/api/tts/jobs", requireAuth, async(req,res)=>{
  try{
    const rows = await listJobs(resolveTenantId(req), "tts");
    res.json(rows.map(r => ({
      jobId:r.id, status:r.status, model:(r.result_payload?.model)||null,
      voice:(r.result_payload?.voice)||null, outputPrefix:r.output_prefix,
      total:(r.result_payload?.total)||0, files:(r.result_payload?.files)||[],
      createdAt:r.created_at
    })));
  }catch(e){ res.status(500).json({error:e.message}); }
});
app.get("/api/tts/files", (_,res)=>{try{res.json(fs.readdirSync(TTS_DIR).filter(f=>/\.wav$/i.test(f)).map(f=>({file:f,url:`/audio/${encodeURIComponent(f)}`})));}catch{res.json([]);}});
app.get("/api/tts/job/:id", requireAuth, async(req,res)=>{
  try{
    const live = await getLiveJob(req.params.id);   // running job → rich live object
    if(live) return res.json(live);
    const row = await getJob(resolveTenantId(req), req.params.id);   // finished → DB row
    if(row) return res.json({
      status:row.status,
      progress:(row.result_payload?.total)||0,
      total:(row.result_payload?.total)||0,
      files:(row.result_payload?.files)||[],
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
  runner(jobId,{tenantId,apiKeys:keys,model,voice,speed,language,audiobookMode,silenceSeconds,audioProfile,transcriptBody,outputPrefix})
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
// IMAGEN  (direct @google/genai, sequential)
// ══════════════════════════════════════════════════════════════════════════════
const PAUSE_MODELS=["imagen-4.0-ultra-generate-001","imagen-4.0-generate-001","imagen-4.0-fast-generate-001"];
async function runImagenJob(jobId,{tenantId,apiKey,model,prompts,outputPrefix,aspectRatio,resolution}){
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
        job.files.push({file:filename,url:`/imgs/${encodeURIComponent(filename)}`});
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
}
app.get("/api/imagen/jobs", requireAuth, async(req,res)=>{
  try{
    const rows = await listJobs(resolveTenantId(req), "imagen");
    res.json(rows.map(r => ({
      jobId:r.id, status:r.status, model:(r.result_payload?.model)||null,
      outputPrefix:r.output_prefix, total:(r.result_payload?.total)||0,
      files:(r.result_payload?.files)||[], createdAt:r.created_at
    })));
  }catch(e){ res.status(500).json({error:e.message}); }
});
app.get("/api/imagen/files", (_,res)=>{try{res.json(fs.readdirSync(IMG_DIR).filter(f=>/\.jpe?g$/i.test(f)).map(f=>({file:f,url:`/imgs/${encodeURIComponent(f)}`})));}catch{res.json([]);}});
app.get("/api/imagen/job/:id", requireAuth, async(req,res)=>{
  try{
    const live = await getLiveJob(req.params.id);
    if(live) return res.json(live);
    const row = await getJob(resolveTenantId(req), req.params.id);
    if(row) return res.json({
      status:row.status,
      progress:(row.result_payload?.total)||0,
      total:(row.result_payload?.total)||0,
      files:(row.result_payload?.files)||[],
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
  runImagenJob(jobId,{tenantId,apiKey:key,model,prompts,outputPrefix,aspectRatio,resolution})
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
    const { prompt, model="imagen-4.0-fast-generate-001", aspect_ratio="16:9", google_api_key="" } = req.body || {};
    const effectiveKey = google_api_key.trim() || GEMINI_KEY;
    if (!effectiveKey) return res.status(400).json({ error:"No Gemini API key — set GEMINI_API_KEY in .env or enter a key in the UI" });
    if (!prompt) return res.status(400).json({ error:"prompt required" });
    console.log(`[generate-image/google] key=${google_api_key.trim()?"CLIENT("+google_api_key.trim().slice(-6)+")":"SERVER_ENV"} model=${model}`);
    const imgAI = mkAI(effectiveKey);
    const mdl = model.startsWith("models/") ? model : `models/${model}`;
    const resp = await imgAI.models.generateImages({ model:mdl, prompt,
      config:{ numberOfImages:1, outputMimeType:"image/jpeg", aspectRatio:aspect_ratio } });
    const imgData = resp?.generatedImages?.[0]?.image?.imageBytes;
    if (!imgData) throw new Error("No image returned");
    res.json({ image_b64: imgData });
  } catch(e) { res.status(500).json({ error: e?.message || String(e) }); }
});

app.get("/api/generate-image/google/models", (_,res) => res.json(GOOGLE_IMG_MODELS));

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
    res.json({ images });
  } catch(e) { res.status(500).json({ error: e?.message || String(e) }); }
});

const server = app.listen(PORT,()=>{ console.log(`🎬 Rino Creative Studio :${PORT}`); console.log(`   Gemini: ${GEMINI_KEY?"set ✅":"MISSING ⚠️"}`); console.log(`   Python: ${PYTHON_API}`); });
// Prevent blank screen on long-running requests (storyboard 26+ scenes with images)
server.timeout = 660000;          // 11 min — must be > storyboard AbortController (10 min)
server.keepAliveTimeout = 660000;
server.headersTimeout = 661000;
