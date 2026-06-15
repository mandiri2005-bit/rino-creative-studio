// ═══════════════════════════════════════════════════════════════════════════
// googleVeo.mjs — Google-native Veo video generation (default provider)
//
// Veo routing rule (per product design): default to Google's Gemini Veo API;
// a LaoZhang key (X-Veo-API-Key) overrides to the LaoZhang path in Python.
// This module is the Google side. It mirrors the LaoZhang submit→poll→stream
// flow so the frontend (and the completion-capture trigger) need no changes:
//   submit → task_id ("gveo_…")  → status polls the long-running operation →
//   stream downloads the finished MP4 and hands it to server.js to persist.
//
// State lives in an in-memory Map keyed by task_id (holds the live operation
// instance + tenant + prompt). NOTE: lost on a Node restart → an in-flight
// Google job is orphaned (the user just regenerates). Durable jobs-table
// backing is a clean follow-up.
// ═══════════════════════════════════════════════════════════════════════════
import { GoogleGenAI } from "@google/genai";
import crypto from "crypto";

const GEMINI_KEY = process.env.GEMINI_API_KEY || "";

// task_id → { op, opName, prompt, tenantId, userId, model, key, status, r2Key }
const _jobs = new Map();

export function isGoogleVeo(id) {
  return typeof id === "string" && id.startsWith("gveo_");
}
export function getGoogleVeoJob(taskId) {
  return _jobs.get(taskId) || null;
}

// Map the frontend veo model id → a current Gemini API Veo model id.
// Pass a valid Gemini Veo id straight through (the frontend's "- Native" options
// send these directly); otherwise best-guess map a legacy id.
const _GOOGLE_VEO = new Set([
  "veo-3.1-generate-preview", "veo-3.1-fast-generate-preview", "veo-3.1-lite-generate-preview",
  "veo-2.0-generate-001", "veo-3.0-generate-001", "veo-3.0-fast-generate-001",
]);
function mapModel(m) {
  if (_GOOGLE_VEO.has(m)) return m;
  const x = String(m || "").toLowerCase();
  if (x.includes("3.1")) return x.includes("fast") ? "veo-3.1-fast-generate-preview" : "veo-3.1-generate-preview";
  if (x.includes("fast")) return "veo-3.0-fast-generate-001";
  return "veo-3.0-generate-001";
}

export async function googleVeoSubmit({ prompt, model, refB64, refMime, negativePrompt, aspectRatio, googleKey, tenantId, userId }) {
  const key = (googleKey || "").trim() || GEMINI_KEY;
  if (!key) { const e = new Error("no_google_key"); e.code = "no_google_key"; throw e; }
  const ai = new GoogleGenAI({ apiKey: key });
  const gModel = mapModel(model);
  const config = { numberOfVideos: 1 };
  if (aspectRatio) config.aspectRatio = aspectRatio;
  if (negativePrompt) config.negativePrompt = negativePrompt;
  const params = { model: gModel, prompt: prompt || "", config };
  if (refB64) params.image = { imageBytes: refB64, mimeType: refMime || "image/png" };

  console.log(`[googleVeo] submit model=${gModel} hasImage=${!!refB64} promptLen=${(prompt || "").length}`);
  const op = await ai.models.generateVideos(params);
  if (!op?.name) throw new Error("generateVideos returned no operation name");

  const taskId = "gveo_" + crypto.randomUUID();
  _jobs.set(taskId, { op, opName: op.name, prompt: prompt || "", tenantId, userId, model: gModel, key, status: "processing", r2Key: null });
  console.log(`[googleVeo] submitted task=${taskId} op=${op.name}`);
  return { task_id: taskId, status: "queued", model: gModel };
}

// Poll the live operation INSTANCE — getVideosOperation needs the real object
// returned by generateVideos (it calls a prototype method), not a {name} literal.
// Store the refreshed instance back on the job for the next poll.
async function _poll(job) {
  const ai = new GoogleGenAI({ apiKey: job.key });
  const op = await ai.operations.getVideosOperation({ operation: job.op });
  job.op = op;
  return op;
}

export async function googleVeoStatus(taskId) {
  const job = _jobs.get(taskId);
  if (!job) return { status: "error", error: "job not found (server restarted — please regenerate)" };
  try {
    const op = await _poll(job);
    if (op?.error) { job.status = "error"; return { status: "error", error: JSON.stringify(op.error).slice(0, 300) }; }
    job.status = op?.done ? "completed" : "processing";
    return { status: job.status, progress: op?.done ? 100 : 50 };
  } catch (e) {
    // Transient poll error → keep the UI polling.
    console.warn(`[googleVeo] status ${taskId} poll error:`, String(e.message || e).slice(0, 200));
    return { status: "processing" };
  }
}

// Download the finished MP4 bytes. The Gemini Developer API returns a `uri`
// (videoBytes is not inline); the download needs ?alt=media + the
// `x-goog-api-key` header — NOT a key= query param.
async function _downloadVideo(vid, key) {
  if (vid.videoBytes) return Buffer.from(vid.videoBytes, "base64");
  if (!vid.uri) throw new Error("operation done but video has neither bytes nor uri");
  let url = vid.uri;
  if (!/[?&]alt=media/.test(url)) url += (url.includes("?") ? "&" : "?") + "alt=media";
  const r = await fetch(url, { headers: { "x-goog-api-key": key } });
  if (!r.ok) throw new Error("video download failed: HTTP " + r.status);
  return Buffer.from(await r.arrayBuffer());
}

// Poll and, if done, return the finished MP4 bytes + job context for persistence.
// Returns { done:false } while running, or { done:true, bytes, prompt, tenantId, userId } / throws.
export async function googleVeoResult(taskId) {
  const job = _jobs.get(taskId);
  if (!job) throw new Error("job not found (server restarted — please regenerate)");
  const op = await _poll(job);
  if (!op?.done) return { done: false };
  if (op.error) throw new Error("generation failed: " + JSON.stringify(op.error).slice(0, 300));
  const vid = op.response?.generatedVideos?.[0]?.video;
  if (!vid) throw new Error("operation done but no video in response");
  const bytes = await _downloadVideo(vid, job.key);
  console.log(`[googleVeo] result ${taskId} ready (${bytes.length} bytes)`);
  return { done: true, bytes, prompt: job.prompt, tenantId: job.tenantId, userId: job.userId, job };
}

export function markGoogleVeoR2(taskId, r2Key) {
  const job = _jobs.get(taskId);
  if (job) job.r2Key = r2Key;
}
