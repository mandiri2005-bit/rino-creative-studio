// ─────────────────────────────────────────────────────────────────────────────
// video/generationClient.mjs — the per-scene generation seam.
//
// The workers call THIS, not the upstream APIs directly, so the orchestration is
// testable with a mock and the real wiring is swappable. Two implementations:
//
//   syntheticGenerationClient — generates a solid-colour still + a sine-tone wav
//     locally with ffmpeg/lavfi. No API keys, deterministic, used by the e2e
//     test and local demos to PROVE the whole engine (dispatch→stitch) end to end.
//
//   httpGenerationClient — calls the live Python backend (PYTHON_API). Per-scene
//     metering already happens INSIDE those endpoints (Step 4), so the client
//     just calls them. Image (/generate-image) and clip (/veo/submit+/veo/status)
//     are wired from the verified route shapes; the TTS-AUDIO endpoint is the one
//     integration point to confirm against the live backend (the live /script/tts
//     is text-enrichment, not synthesis) — see synthesizeAudio below.
//
// Each method writes its asset into `tmpDir` and returns { path, kind?, durationSeconds? }.
// ─────────────────────────────────────────────────────────────────────────────
import { spawn } from "node:child_process";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { FFMPEG } from "./ffmpeg.mjs";

function run(bin, args) {
  return new Promise((resolve, reject) => {
    const p = spawn(bin, args);
    let err = "";
    p.stderr.on("data", (d) => (err += d));
    p.on("error", reject);
    p.on("close", (c) => (c === 0 ? resolve() : reject(new Error(`${bin} ${c}: ${err.slice(-400)}`))));
  });
}

// A last-resort, on-brand still. When real image generation is unavailable (a
// flaky upstream 500, a content block, an exhausted clip→image fallback), the
// scene degrades to a dark card instead of failing — the burnt caption + Ken
// Burns still render over it, so ONE bad generation never kills the whole video.
// Pure ffmpeg/lavfi: no API, no keys, always available wherever the stitch runs.
async function makePlaceholder(tmpDir, sceneIndex, W = 1280, H = 720) {
  const out = join(tmpDir, `img_${sceneIndex}.png`);
  await run(FFMPEG, ["-v", "error", "-y", "-f", "lavfi", "-i",
    `color=c=0x14142a:s=${W}x${H}`, "-frames:v", "1", out]);
  return { path: out, kind: "image", placeholder: true };
}

// ── Synthetic (no API keys) ───────────────────────────────────────────────────
export function syntheticGenerationClient(opts = {}) {
  const W = opts.width || 1280, H = opts.height || 720, fps = opts.fps || 30;
  const COLORS = ["navy", "teal", "maroon", "darkgreen", "indigo", "sienna", "slateblue", "olive"];
  return {
    async synthesizeAudio(scene, tmpDir) {
      const dur = Math.max(0.5, Number(scene.estSeconds) || 2);
      const out = join(tmpDir, `audio_${scene.sceneIndex}.wav`);
      const freq = 300 + ((scene.sceneIndex || 0) % 8) * 60;
      await run(FFMPEG, ["-v", "error", "-y", "-f", "lavfi", "-i",
        `sine=frequency=${freq}:duration=${dur}`, out]);
      return { path: out, durationSeconds: dur };
    },
    async generateVisual(scene, tmpDir) {
      const wantClip = scene.kind === "clip";
      if (wantClip) {
        const dur = Math.max(0.5, Number(scene.estSeconds) || 2);
        const out = join(tmpDir, `clip_${scene.sceneIndex}.mp4`);
        await run(FFMPEG, ["-v", "error", "-y", "-f", "lavfi", "-i",
          `testsrc=size=${W}x${H}:rate=${fps}`, "-t", String(dur), "-pix_fmt", "yuv420p", out]);
        return { path: out, kind: "clip" };
      }
      const out = join(tmpDir, `img_${scene.sceneIndex}.png`);
      const color = COLORS[(scene.sceneIndex || 0) % COLORS.length];
      await run(FFMPEG, ["-v", "error", "-y", "-f", "lavfi", "-i",
        `color=c=${color}:s=${W}x${H}`, "-frames:v", "1", out]);
      return { path: out, kind: "image" };
    },
    async placeholderImage(scene, tmpDir) {
      return makePlaceholder(tmpDir, scene.sceneIndex, W, H);
    },
    async refundVideoJob() { return { skipped: "synthetic" }; },
    async meterUsage() { return { credits: 0, skipped: "synthetic" }; },
  };
}

// Gemini TTS via Google's GenAI SDK — the OpenAI-compatible /v1/audio/speech route
// doesn't serve Gemini voices, so the worker (Node) generates them directly, the
// exact path the Studio uses. Returns a WAV Buffer. Throws if no key / no audio so
// the caller can fall back to the OpenAI path.
async function geminiTts(text, voice, model) {
  if (!process.env.GEMINI_API_KEY) throw new Error("no GEMINI_API_KEY");
  const { GoogleGenAI } = await import("@google/genai");
  const { convertToWav } = await import("../utils.mjs");
  const ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY });
  const resp = await ai.models.generateContent({
    model,
    contents: [{ role: "user", parts: [{ text: String(text || "") }] }],
    config: {
      temperature: 1.0, responseModalities: ["AUDIO"],
      speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: voice || "Enceladus" } } },
    },
  });
  const inline = resp?.candidates?.[0]?.content?.parts?.[0]?.inlineData;
  if (!inline?.data) throw new Error("gemini tts returned no audio");
  return convertToWav(Buffer.from(inline.data, "base64"), inline.mimeType || "audio/L16;rate=24000");
}

// ── HTTP (live Python backend) ────────────────────────────────────────────────
// Terminal Veo status strings (lowercased) seen across the upstream provider.
const VEO_DONE = new Set(["succeed", "succeeded", "success", "completed", "complete", "done", "finished", "ready"]);
const VEO_FAIL = new Set(["failed", "error", "canceled", "cancelled"]);

// Map the segmenter/decide model alias → the real upstream model id the provider
// expects. The bare alias is rejected by the provider. Veo variants + Kling go
// through the /veo/* routes; Sora through the parallel /sora/* routes.
const CLIP_MODEL_IDS = {
  veo3: "veo-3.1-generate-preview", veo3_fast: "veo-3.1-fast", veo3_pro: "veo-3.1",
  kling3: "kling-v1.6", sora: "sora-2",
};
// which /{provider}/{submit,status,stream} route family an alias uses
function clipProvider(alias) { return String(alias || "").toLowerCase().startsWith("sora") ? "sora" : "veo"; }

export function httpGenerationClient(opts = {}) {
  const PYTHON_API = opts.pythonApi || process.env.PYTHON_API_URL || "http://127.0.0.1:8000";
  const ttsAudioPath = opts.ttsAudioPath || process.env.VIDEO_TTS_AUDIO_PATH || "/video/tts/scene";
  const internalSecret = opts.internalSecret || process.env.INTERNAL_SERVICE_SECRET || "";
  const clipTimeoutMs = opts.clipTimeoutMs || 240000;

  // Worker→Python auth: the worker has no user JWT. When INTERNAL_SERVICE_SECRET
  // is set it authenticates as the tenant via trusted internal headers (Python
  // accepts these only when the secret matches), so per-scene metering + RLS work.
  function authHeaders(scene) {
    // X-Video-Job-Id tags every per-scene charge with the assembly job, so a
    // failed video can refund exactly what it consumed (Python /video/credits/refund).
    const vj = scene.jobId ? { "X-Video-Job-Id": String(scene.jobId) } : {};
    if (internalSecret) {
      return {
        "X-Internal-Secret": internalSecret,
        "X-Internal-Tenant-Id": scene.tenantId || "",
        "X-Internal-User-Id": scene.userId || scene.tenantId || "",
        ...vj,
      };
    }
    return { ...(opts.authToken ? { Authorization: `Bearer ${opts.authToken}` } : {}), ...vj };
  }

  async function getBytes(url, headers, out) {
    const r = await fetch(url, { headers });
    if (!r.ok) throw new Error(`fetch ${r.status} ${url}`);
    await writeFile(out, Buffer.from(await r.arrayBuffer()));
    return out;
  }

  return {
    // Single-shot per-scene narration. Targets the Python /video/tts/scene route
    // (configurable via VIDEO_TTS_AUDIO_PATH) → { audio_b64 } | { audio_url }.
    async synthesizeAudio(scene, tmpDir) {
      const out = join(tmpDir, `audio_${scene.sceneIndex}.wav`);
      const model = scene.ttsModel || "";
      // Gemini TTS: generate in-worker via Google's SDK, then meter the chars in
      // Python (audio bytes never leave the worker). Any failure → OpenAI path below.
      if (/gemini/i.test(model) && /tts/i.test(model) && process.env.GEMINI_API_KEY) {
        try {
          await writeFile(out, await geminiTts(scene.text, scene.voice, model));
          await fetch(`${PYTHON_API}${ttsAudioPath}`, {
            method: "POST",
            headers: { "Content-Type": "application/json", ...authHeaders(scene) },
            body: JSON.stringify({ text: scene.text, model, meter_only: true, scene_index: scene.sceneIndex }),
          }).catch(() => {});  // best-effort metering; never fail the scene on it
          return { path: out };
        } catch { /* fall through to the OpenAI-compatible path */ }
      }
      const r = await fetch(`${PYTHON_API}${ttsAudioPath}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders(scene) },
        body: JSON.stringify({ text: scene.text, voice: scene.voice, model: model || undefined, scene_index: scene.sceneIndex }),
      });
      if (!r.ok) throw new Error(`tts ${r.status}: ${(await r.text()).slice(0, 200)}`);
      const data = await r.json();
      if (data.audio_b64) await writeFile(out, Buffer.from(data.audio_b64, "base64"));
      else if (data.audio_url) await getBytes(data.audio_url, {}, out);
      else throw new Error("tts response had no audio_b64/audio_url");
      return { path: out, durationSeconds: data.duration_seconds };
    },

    async generateVisual(scene, tmpDir) {
      const h = authHeaders(scene);
      if (scene.kind !== "clip") {
        const out = join(tmpDir, `img_${scene.sceneIndex}.png`);
        const r = await fetch(`${PYTHON_API}/generate-image`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...h },
          body: JSON.stringify({ model: scene.imageModel || "nano-banana-hd", prompt: scene.visualPrompt, aspect_ratio: scene.aspectRatio || "16:9", seed: scene.seed || 0, ref_image: scene.refImage || "" }),
        });
        if (!r.ok) throw new Error(`image ${r.status}: ${(await r.text()).slice(0, 150)}`);
        const data = await r.json();
        const b64 = data.image_b64 || data.b64 || data.data?.[0]?.b64_json;
        if (b64) await writeFile(out, Buffer.from(b64, "base64"));
        else if (data.url || data.image_url) await getBytes(data.url || data.image_url, {}, out);
        else throw new Error("image response had no image_b64/url");
        return { path: out, kind: "image" };
      }
      // clip: submit → poll status → pull the MP4 from /{provider}/stream (the real
      // contract: the bytes come from the stream route, NOT a status field).
      const out = join(tmpDir, `clip_${scene.sceneIndex}.mp4`);
      // the worker sends the chosen model alias (veo3/veo3_fast/sora/kling3) →
      // translate to the real upstream id + pick the /veo or /sora route family.
      const modelId = CLIP_MODEL_IDS[scene.clipModel] || scene.clipModelId || "veo-3.1-generate-preview";
      const prov = clipProvider(scene.clipModel);
      const submit = await fetch(`${PYTHON_API}/${prov}/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...h },
        body: JSON.stringify({ prompt: scene.visualPrompt, model: modelId, aspect: scene.aspectRatio || "16:9", seed: scene.seed ? String(scene.seed) : "" }),
      });
      if (!submit.ok) throw new Error(`${prov} submit ${submit.status}`);
      const { task_id } = await submit.json();
      if (!task_id) throw new Error(`${prov} submit returned no task_id`);
      const deadline = Date.now() + clipTimeoutMs;
      // poll loop runs INSIDE the visual worker (separate process) → never blocks the API loop
      for (;;) {
        if (Date.now() > deadline) throw new Error(`${prov} timeout`);
        await new Promise((s) => setTimeout(s, 5000));
        const st = await fetch(`${PYTHON_API}/${prov}/status/${task_id}`, { headers: h });
        if (!st.ok) continue;
        const sd = await st.json();
        const status = String(sd.status || "").toLowerCase();
        if (VEO_FAIL.has(status)) throw new Error(`${prov} ${status}`);
        if (VEO_DONE.has(status) || Number(sd.progress) >= 100) break;
      }
      // /{provider}/stream returns the MP4 bytes directly (and self-retries if still encoding)
      await getBytes(`${PYTHON_API}/${prov}/stream/${task_id}`, h, out);
      return { path: out, kind: "clip" };
    },

    // Last-resort local still — the worker calls this when both the real image and
    // (for clips) the clip→image fallback are unavailable, so the video completes.
    async placeholderImage(scene, tmpDir) {
      return makePlaceholder(tmpDir, scene.sceneIndex, opts.width, opts.height);
    },

    // Refund a FAILED assembly's actual spend. Internal-auth only; idempotent on the
    // Python side (op_id=video-refund:<job>), so the orchestrator can call it freely.
    async refundVideoJob(tenantId, jobId) {
      if (!internalSecret) return { skipped: "no-internal-secret" };
      const r = await fetch(`${PYTHON_API}/video/credits/refund`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Internal-Secret": internalSecret,
          "X-Internal-Tenant-Id": tenantId || "",
          "X-Internal-User-Id": tenantId || "",
        },
        body: JSON.stringify({ job_id: jobId }),
      });
      if (!r.ok) throw new Error(`refund ${r.status}: ${(await r.text()).slice(0, 150)}`);
      return r.json();
    },

    // Charge a meter for an asset the worker generated ITSELF (whiteboard Recraft
    // images + the flat render fee) via the internal /video/meter endpoint. ctx
    // carries { jobId, tenantId, userId } for the internal-auth + video-job tag.
    // Best-effort: a metering hiccup must never fail the render (balance was
    // pre-checked at /assemble).
    async meterUsage(ctx, operation, model, units) {
      try {
        const r = await fetch(`${PYTHON_API}/video/meter`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders(ctx || {}) },
          body: JSON.stringify({ operation, model, units: units || {} }),
        });
        if (!r.ok) { console.warn(`[meter] ${operation}/${model} ${r.status}`); return { credits: 0 }; }
        return await r.json();
      } catch (e) {
        console.warn(`[meter] ${operation}/${model} failed: ${e.message}`);
        return { credits: 0 };
      }
    },
  };
}
