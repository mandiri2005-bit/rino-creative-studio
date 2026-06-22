// Offline salvage: render a job's CACHED scenes locally (no re-pay), full quality (cap stays 70),
// resumable (skips scenes whose mp4 already exists), then concat → upload → signed URL.
// Run: railway run -s video-worker node salvage-render.mjs        (full run)
//      SALVAGE_ONLY=0 railway run -s video-worker node salvage-render.mjs   (test scene 0 only)
import { sharedConnection } from "./backend/video/connection.mjs";
import { renderWhiteboardPlanSvg } from "./backend/video/whiteboard/renderers/svgFfmpeg.mjs";
import { existsSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { spawn } from "node:child_process";

const JOB = process.env.SALVAGE_JOB || "vid_mqodfolr_wpzpzg";
const ONLY = process.env.SALVAGE_ONLY != null ? Number(process.env.SALVAGE_ONLY) : null;
const WORK = `/tmp/salvage-${JOB}`;
mkdirSync(WORK, { recursive: true });
const mmss = (s) => `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, "0")}s`;

const r = sharedConnection();
const storage = await import("./backend/storage.mjs");
const meta = JSON.parse(await r.get(`vjob:${JOB}`));
const n = meta.sceneCount;
console.log(`JOB ${JOB}: ${n} scenes · genre=${meta.whiteboardGenre} · aspect=${meta.aspectRatio} · tier=${meta.tier} · R2=${storage.isConfigured?.()}`);

const sceneMp4s = [];
const t0 = Date.now();
let rendered = 0;
for (let i = 0; i < n; i++) {
  if (ONLY != null && i !== ONLY) continue;
  const outI = `${WORK}/scene-${i}.mp4`;
  if (existsSync(outI)) { console.log(`scene ${i}: ✓ cached → skip`); sceneMp4s.push(outI); continue; }
  const h = await r.hgetall(`vjob:${JOB}:scene:${i}`);
  if (!h.planJson) { console.log(`scene ${i}: ⚠ no planJson → skip`); continue; }
  let audioPath = null;
  if (h.audioKey && storage.isConfigured?.()) {
    audioPath = `${WORK}/aud-${i}.wav`;
    try { writeFileSync(audioPath, await storage.downloadBytes(h.audioKey)); }
    catch (e) { console.warn(`  audio dl fail (${e.message}) → silent`); audioPath = null; }
  }
  const scene = { planJson: h.planJson, duration: Number(h.durationActual || h.estSeconds || 6),
    audioPath, number: i + 1, text: h.text || "", kind: h.kind || "image" };
  const st = Date.now();
  await renderWhiteboardPlanSvg([scene], { ...meta, jobId: JOB }, outI, { tmpDir: `${WORK}/t${i}` });
  sceneMp4s.push(outI); rendered++;
  const el = (Date.now() - st) / 1000;
  const totalDone = sceneMp4s.length;
  const avg = (Date.now() - t0) / 1000 / Math.max(1, rendered);
  console.log(`scene ${i}: ✅ ${mmss(el)}  (${totalDone}/${n} done, ~${mmss((n - totalDone) * avg)} left)`);
}
if (ONLY != null) { console.log(`\nTEST done → ${WORK}/scene-${ONLY}.mp4`); process.exit(0); }

console.log("concat 39 scenes…");
const inputs = sceneMp4s.flatMap((m) => ["-i", m]);
const fc = sceneMp4s.map((_, i) => `[${i}:v:0][${i}:a:0]`).join("") + `concat=n=${sceneMp4s.length}:v=1:a=1[v][a]`;
const finalOut = `${WORK}/final.mp4`;
await new Promise((res, rej) => {
  const p = spawn("ffmpeg", ["-y", ...inputs, "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-c:a", "aac", "-ar", "44100", "-ac", "2", finalOut]);
  let e = ""; p.stderr.on("data", (d) => (e += d));
  p.on("close", (c) => (c === 0 ? res() : rej(new Error("ffmpeg " + c + ": " + e.slice(-300)))));
});
const key = storage.videoKey(meta.tenantId, JOB);
await storage.uploadBytes(key, readFileSync(finalOut), "video/mp4");
const url = await storage.signedUrl(key, 6 * 3600);
console.log(`\n✅ DONE (${mmss((Date.now() - t0) / 1000)}) → ${url}`);
process.exit(0);
