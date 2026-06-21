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

// Per-request fetch timeout (Node 20+ global fetch + AbortSignal.timeout). The clip
// path has its own clipTimeoutMs poll budget, but TTS / image / Recraft / download
// calls had NONE — a hung upstream would hold a BullMQ audio/visual slot until the
// socket eventually died (slot leak under the same scarcity we're fixing on the CPU
// side). GEN_FETCH_TIMEOUT_MS=0 disables it = escape hatch to pre-fix behaviour.
const GEN_FETCH_TIMEOUT_MS = Number(process.env.GEN_FETCH_TIMEOUT_MS || 120000);
function fetchT(url, opts = {}) {
  if (!(GEN_FETCH_TIMEOUT_MS > 0)) return fetch(url, opts);
  return fetch(url, { ...opts, signal: opts.signal ?? AbortSignal.timeout(GEN_FETCH_TIMEOUT_MS) });
}

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

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// A SILENT WAV the length of the scene — the audio counterpart to makePlaceholder. When
// TTS fails after retries, the scene degrades to silence instead of failing the whole video
// (sceneComplete counts an audio "fallback", so planAdvance still completes the job). Pure
// ffmpeg/lavfi, no API/keys. Lets GEN_FETCH_TIMEOUT_MS go back to a safe value: a timed-out
// TTS no longer kills the job.
async function makeSilentAudio(tmpDir, sceneIndex, dur) {
  const out = join(tmpDir, `audio_${sceneIndex}.wav`);
  const d = Math.max(0.5, Number(dur) || 2);
  await run(FFMPEG, ["-v", "error", "-y", "-f", "lavfi", "-i",
    "anullsrc=channel_layout=stereo:sample_rate=48000", "-t", String(d), out]);
  return { path: out, durationSeconds: d, silent: true };
}

// De-burst clip submits: a 20-scene job fires ~10 Veo /submit at once → laozhang's Veo group
// 429s ("当前分组上游负载已饱和" / upstream load saturated). Cap concurrent SUBMITS (poll/stream
// stay unbounded — they're spread over time anyway). Per-process; env VIDEO_CLIP_CONCURRENCY.
const CLIP_SUBMIT_RETRIES = Math.max(0, Number(process.env.VIDEO_CLIP_SUBMIT_RETRIES || 3));
const CLIP_SUBMIT_BACKOFF_MS = Math.max(500, Number(process.env.VIDEO_CLIP_SUBMIT_BACKOFF_MS || 6000));
let _clipMax = Math.max(1, Number(process.env.VIDEO_CLIP_CONCURRENCY || 3));
let _clipActive = 0;
const _clipWaiters = [];
function _clipAcquire() {
  if (_clipActive < _clipMax) { _clipActive++; return Promise.resolve(); }
  return new Promise((res) => _clipWaiters.push(res));
}
function _clipRelease() {
  const next = _clipWaiters.shift();
  if (next) next();
  else _clipActive = Math.max(0, _clipActive - 1);
}
// Run fn() holding one clip-submit slot (released in finally so a throw never leaks).
export async function withClipSlot(fn) {
  await _clipAcquire();
  try { return await fn(); } finally { _clipRelease(); }
}
export function _setClipSlotsForTest(n) { _clipMax = Math.max(1, Number(n)); _clipActive = 0; _clipWaiters.length = 0; }

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
    async silentAudio(scene, tmpDir) {
      return makeSilentAudio(tmpDir, scene.sceneIndex, scene.estSeconds);
    },
    async refundVideoJob() { return { skipped: "synthetic" }; },
    async meterUsage() { return { credits: 0, skipped: "synthetic" }; },
    async generateDiagramGraph() { return null; },  // → buildDiagramSvg uses its example graph
    async generateWhiteboardPlan() { return null; }, // → scene degrades to handwriting in synthetic mode
    async generateWhiteboardRaster() { return null; }, // → falls back to recraft/handwriting in synthetic mode
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
// laozhang Veo official-forward accepts ONLY the *-generate-preview names — the legacy
// "veo-3.1" / "veo-3.1-fast" / "veo-3.1-fl" are rejected with 503 "no available channels"
// (the Studio Veo page uses the -generate-preview names, which is why it works).
// docs: https://docs.laozhang.ai/en/api-capabilities/veo/official-forward
export const CLIP_MODEL_IDS = {
  veo3: "veo-3.1-generate-preview", veo3_fast: "veo-3.1-fast-generate-preview", veo3_pro: "veo-3.1-generate-preview",
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
    const r = await fetchT(url, { headers });
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
          await fetchT(`${PYTHON_API}${ttsAudioPath}`, {
            method: "POST",
            headers: { "Content-Type": "application/json", ...authHeaders(scene) },
            body: JSON.stringify({ text: scene.text, model, meter_only: true, scene_index: scene.sceneIndex }),
          }).catch(() => {});  // best-effort metering; never fail the scene on it
          return { path: out };
        } catch { /* fall through to the OpenAI-compatible path */ }
      }
      // Retry the OpenAI-compatible TTS on TRANSIENT failure (fetch timeout / 5xx) before
      // giving up — the worker then degrades to a silent track (never a job-kill). A 4xx is a
      // real caller error → no retry. env GEN_TTS_RETRIES (default 2).
      const ttsRetries = Math.max(0, Number(process.env.GEN_TTS_RETRIES || 2));
      let lastErr;
      for (let attempt = 0; attempt <= ttsRetries; attempt++) {
        try {
          const r = await fetchT(`${PYTHON_API}${ttsAudioPath}`, {
            method: "POST",
            headers: { "Content-Type": "application/json", ...authHeaders(scene) },
            body: JSON.stringify({ text: scene.text, voice: scene.voice, model: model || undefined, scene_index: scene.sceneIndex }),
          });
          if (!r.ok) {
            if (r.status >= 500 && attempt < ttsRetries) { lastErr = new Error(`tts ${r.status}`); await sleep(2000 * (attempt + 1)); continue; }
            throw new Error(`tts ${r.status}: ${(await r.text()).slice(0, 200)}`);
          }
          const data = await r.json();
          if (data.audio_b64) await writeFile(out, Buffer.from(data.audio_b64, "base64"));
          else if (data.audio_url) await getBytes(data.audio_url, {}, out);
          else throw new Error("tts response had no audio_b64/audio_url");
          return { path: out, durationSeconds: data.duration_seconds };
        } catch (e) {
          lastErr = e;
          const transient = /timeout|aborted|fetch failed|network|ECONN|ETIMEDOUT/i.test(e.message || "");
          if (transient && attempt < ttsRetries) { await sleep(2000 * (attempt + 1)); continue; }
          throw e;
        }
      }
      throw lastErr;
    },

    async generateVisual(scene, tmpDir) {
      const h = authHeaders(scene);
      if (scene.kind !== "clip") {
        const out = join(tmpDir, `img_${scene.sceneIndex}.png`);
        const r = await fetchT(`${PYTHON_API}/generate-image`, {
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
      // De-burst (withClipSlot caps concurrent submits) + retry on 429/503 ("upstream load
      // saturated, retry later") — both are transient under a multi-scene burst, so riding them
      // out yields a real CLIP instead of silently degrading to the clip→image fallback.
      let task_id;
      await withClipSlot(async () => {
        for (let attempt = 0; ; attempt++) {
          const submit = await fetch(`${PYTHON_API}/${prov}/submit`, {
            method: "POST",
            headers: { "Content-Type": "application/json", ...h },
            body: JSON.stringify({ prompt: scene.visualPrompt, model: modelId, aspect: scene.aspectRatio || "16:9", seed: scene.seed ? String(scene.seed) : "" }),
          });
          if (submit.ok) { ({ task_id } = await submit.json()); return; }
          if ((submit.status === 429 || submit.status === 503) && attempt < CLIP_SUBMIT_RETRIES) {
            await sleep(CLIP_SUBMIT_BACKOFF_MS * (attempt + 1));
            continue;
          }
          throw new Error(`${prov} submit ${submit.status}`);
        }
      });
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
    // Last-resort silent narration — the worker calls this when TTS fails after retries,
    // so ONE bad scene degrades to silence instead of failing the whole video.
    async silentAudio(scene, tmpDir) {
      return makeSilentAudio(tmpDir, scene.sceneIndex, scene.estSeconds);
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

    // Pre-check balance for a paid in-worker gen (e.g. Recraft icon) BEFORE generating, so we can
    // fall back to a free icon instead of debiting into the negative. Returns true if covered.
    // Fail-OPEN on a metering hiccup (don't block a render on a transient error) — the flux route +
    // /assemble pre-check are the other guards.
    async gateUsage(ctx, operation, model, units) {
      try {
        const r = await fetch(`${PYTHON_API}/video/meter`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders(ctx || {}) },
          body: JSON.stringify({ operation, model, units: units || {}, gate_only: true }),
        });
        if (!r.ok) return true;
        return (await r.json()).ok !== false;
      } catch { return true; }
    },

    // Flowchart graph for the whiteboard 'diagram' genre — from Python, which uses the
    // SAME LLM routing/failover + Model Narasi as narration (no new LLM key in the
    // worker). The worker turns the graph into a clean SVG (buildDiagramSvg).
    async generateDiagramGraph(ctx, { description, model, language } = {}) {
      const r = await fetch(`${PYTHON_API}/video/diagram`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders(ctx || {}) },
        body: JSON.stringify({ description: description || "", model: model || "deepseek-chat", language: language || "" }),
      });
      if (!r.ok) throw new Error(`diagram ${r.status}`);
      return (await r.json()).graph;
    },
    // Whiteboard plan-engine: narration scene → whiteboard_visual_plan JSON (Golpo-like),
    // via Python /video/whiteboard-plan (same LLM routing as narration). Returns the plan
    // object or null (Node then validates/resolves; null → handwriting fallback for the scene).
    async generateWhiteboardPlan(ctx, { narration, duration, genre, model, language, sceneId } = {}) {
      const r = await fetch(`${PYTHON_API}/video/whiteboard-plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders(ctx || {}) },
        body: JSON.stringify({
          narration: narration || "", duration: Number(duration) || 8,
          genre: genre || "lineart", model: model || "deepseek-chat",
          language: language || "", scene_id: sceneId || "scene",
        }),
      });
      if (!r.ok) throw new Error(`whiteboard-plan ${r.status}`);
      return (await r.json()).plan;
    },
    // Whiteboard raster supplier (Guide-2 §I): asset_query → ONE realistic raster (base64) via
    // Python /video/whiteboard-raster (laozhang flux-kontext-pro — Python owns the image key).
    // Returns base64 string or null; the worker vectorizes it (recraft) into the reveal mask and
    // meters flux-kontext-pro itself. null → caller falls back to Recraft / handwriting.
    async generateWhiteboardRaster(ctx, { query, provider, aspect, seed, mode, timeoutMs, heroStyle } = {}) {
      // timeoutMs: abort a STALLED hero image gen (the upstream POST is 180s internally; a hero that
      // takes that long is a stall) so the worker can retry fast instead of blocking the whole scene.
      const signal = timeoutMs ? AbortSignal.timeout(timeoutMs) : undefined;
      const r = await fetch(`${PYTHON_API}/video/whiteboard-raster`, {
        method: "POST", signal,
        headers: { "Content-Type": "application/json", ...authHeaders(ctx || {}) },
        body: JSON.stringify({
          query: query || "", provider: provider || "flux",
          aspect_ratio: aspect || "1:1", seed: Number(seed) || 0, mode: mode || "subject",
          hero_style: heroStyle || "",   // per-video hero look (UI dropdown); "" → server default
        }),
      });
      if (!r.ok) throw new Error(`whiteboard-raster ${r.status}`);
      return (await r.json()).raster_b64 || null;
    },
  };
}
