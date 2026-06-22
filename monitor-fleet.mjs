// Fleet monitor: track active VI jobs — render progress, parallelism, per-scene, render wall-time.
import { sharedConnection } from "./backend/video/connection.mjs";
const r = sharedConnection();
const ACTIVE = ["queued", "running", "stitching"];
const mmss = (s) => `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;

async function fleet() {
  const keys = (await r.keys("vjob:*")).filter((k) => /^vjob:[^:]+$/.test(k));
  const jobs = [];
  for (const k of keys) {
    const raw = await r.get(k).catch(() => null); if (!raw) continue;
    try { jobs.push({ id: k.slice(5), ...JSON.parse(raw) }); } catch {}
  }
  return jobs.sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
}
async function prog(id, n) {
  if (!n) return { a: 0, v: 0 };
  const p = r.pipeline(); for (let i = 0; i < n; i++) p.hgetall(`vjob:${id}:scene:${i}`);
  const res = await p.exec(); let a = 0, v = 0;
  res.forEach(([, h]) => { h = h || {}; if (["done", "fallback"].includes(h.audioStatus)) a++; if (["done", "fallback"].includes(h.visualStatus)) v++; });
  return { a, v };
}

const t0 = Date.now();
const renderWall = {}; const doneSeen = new Set();
let seenActive = false, empties = 0;
console.log("▶ fleet monitor (5-min video) — generate sekarang…");

for (let tick = 0; tick < 160; tick++) {   // up to ~27 min
  const jobs = await fleet();
  const active = jobs.filter((j) => ACTIVE.includes(j.status));
  for (const j of jobs) {
    if (j.status === "done" && !doneSeen.has(j.id) && j.renderStartedAt) {
      renderWall[j.id] = Math.round((Date.now() - j.renderStartedAt) / 1000); doneSeen.add(j.id);
    }
  }
  if (active.length) { seenActive = true; empties = 0; } else empties++;
  const rendering = active.filter((j) => j.status === "stitching" && j.renderStartedAt).length;
  const qR = active.filter((j) => j.status === "stitching" && !j.renderStartedAt).length;
  const gen = active.filter((j) => j.status === "running" || j.status === "queued").length;
  const out = [`── [${mmss(Math.round((Date.now() - t0) / 1000))}] active=${active.length} 🎬render=${rendering} ⏳antre=${qR} 🧩gen=${gen} ──`];
  for (const j of active) {
    const { a, v } = await prog(j.id, j.sceneCount || 0);
    const age = Math.round((Date.now() - (j.createdAt || Date.now())) / 1000);
    let ph = j.status;
    if (j.status === "stitching") ph = j.renderStartedAt
      ? `🎬render ${j.renderScenesDone || 0}/${j.renderScenesTotal || j.sceneCount || "?"} ${j.renderProgress || 0}%`
      : "⏳antre-render";
    out.push(`   ${j.id} [${j.whiteboardGenre || j.visualMode} ${j.sceneCount}sc ${j.heroStyle || ""}] A${a}/${j.sceneCount || 0} V${v}/${j.sceneCount || 0} · ${ph} · ${mmss(age)}`);
  }
  console.log(out.join("\n"));
  if (seenActive && empties >= 3) { console.log("■ fleet idle — stop"); break; }
  await new Promise((res) => setTimeout(res, 10000));
}
console.log("\n=== RENDER WALL-TIME (renderStartedAt → done) ===");
const jobs = await fleet();
for (const j of jobs.filter((x) => x.status === "done").slice(0, 6))
  console.log(`  ${j.id}: ${j.sceneCount}sc, video=${Math.round(j.durationActual || 0)}s${renderWall[j.id] ? `, render=${mmss(renderWall[j.id])}` : ""}`);
process.exit(0);
