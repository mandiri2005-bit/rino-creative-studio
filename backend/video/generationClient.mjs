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
  };
}

// ── HTTP (live Python backend) ────────────────────────────────────────────────
// Terminal Veo status strings (lowercased) seen across the upstream provider.
const VEO_DONE = new Set(["succeed", "succeeded", "success", "completed", "complete", "done", "finished", "ready"]);
const VEO_FAIL = new Set(["failed", "error", "canceled", "cancelled"]);

// Map the segmenter/decide model alias → the real upstream model id the Veo
// endpoint expects. The bare alias ("veo3"/"kling3") is rejected by the provider.
const CLIP_MODEL_IDS = { veo3: "veo-3.1-generate-preview", kling3: "kling-v1.6" };

export function httpGenerationClient(opts = {}) {
  const PYTHON_API = opts.pythonApi || process.env.PYTHON_API_URL || "http://127.0.0.1:8000";
  const ttsAudioPath = opts.ttsAudioPath || process.env.VIDEO_TTS_AUDIO_PATH || "/video/tts/scene";
  const internalSecret = opts.internalSecret || process.env.INTERNAL_SERVICE_SECRET || "";
  const clipTimeoutMs = opts.clipTimeoutMs || 240000;

  // Worker→Python auth: the worker has no user JWT. When INTERNAL_SERVICE_SECRET
  // is set it authenticates as the tenant via trusted internal headers (Python
  // accepts these only when the secret matches), so per-scene metering + RLS work.
  function authHeaders(scene) {
    if (internalSecret) {
      return {
        "X-Internal-Secret": internalSecret,
        "X-Internal-Tenant-Id": scene.tenantId || "",
        "X-Internal-User-Id": scene.userId || scene.tenantId || "",
      };
    }
    return opts.authToken ? { Authorization: `Bearer ${opts.authToken}` } : {};
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
      const r = await fetch(`${PYTHON_API}${ttsAudioPath}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders(scene) },
        body: JSON.stringify({ text: scene.text, voice: scene.voice, scene_index: scene.sceneIndex }),
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
          body: JSON.stringify({ model: scene.imageModel || "nano-banana-hd", prompt: scene.visualPrompt }),
        });
        if (!r.ok) throw new Error(`image ${r.status}: ${(await r.text()).slice(0, 150)}`);
        const data = await r.json();
        const b64 = data.image_b64 || data.b64 || data.data?.[0]?.b64_json;
        if (b64) await writeFile(out, Buffer.from(b64, "base64"));
        else if (data.url || data.image_url) await getBytes(data.url || data.image_url, {}, out);
        else throw new Error("image response had no image_b64/url");
        return { path: out, kind: "image" };
      }
      // clip: submit → poll status → pull the MP4 from /veo/stream (the real
      // contract: the bytes come from /veo/stream/{id}, NOT a status field).
      const out = join(tmpDir, `clip_${scene.sceneIndex}.mp4`);
      // the worker sends the chosen model alias (veo3/kling3) — translate to the
      // real upstream id; never pass the bare alias (the provider rejects it).
      const modelId = CLIP_MODEL_IDS[scene.clipModel] || scene.clipModelId || "veo-3.1-generate-preview";
      const submit = await fetch(`${PYTHON_API}/veo/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...h },
        body: JSON.stringify({ prompt: scene.visualPrompt, model: modelId }),
      });
      if (!submit.ok) throw new Error(`veo submit ${submit.status}`);
      const { task_id } = await submit.json();
      if (!task_id) throw new Error("veo submit returned no task_id");
      const deadline = Date.now() + clipTimeoutMs;
      // poll loop runs INSIDE the visual worker (separate process) → never blocks the API loop
      for (;;) {
        if (Date.now() > deadline) throw new Error("veo timeout");
        await new Promise((s) => setTimeout(s, 5000));
        const st = await fetch(`${PYTHON_API}/veo/status/${task_id}`, { headers: h });
        if (!st.ok) continue;
        const sd = await st.json();
        const status = String(sd.status || "").toLowerCase();
        if (VEO_FAIL.has(status)) throw new Error(`veo ${status}`);
        if (VEO_DONE.has(status) || Number(sd.progress) >= 100) break;
      }
      // /veo/stream returns the MP4 bytes directly (and self-retries if still encoding)
      await getBytes(`${PYTHON_API}/veo/stream/${task_id}`, h, out);
      return { path: out, kind: "clip" };
    },
  };
}
