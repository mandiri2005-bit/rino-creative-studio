// video/whiteboard/visuals.mjs — per-genre per-scene ASSET generation for whiteboard
// jobs, run IN the visual worker (off the API loop). Reuses the proven standalone
// logic: color → Recraft vector SVG; detail → Recraft raster + vectorized mask SVG;
// diagram → flowchart graph (fetched from Python via opts.diagramGraph, which reuses the
// existing narration LLM routing/failover + Model Narasi) → deterministic SVG here. The
// caller meters color/detail via /video/meter (Recraft cost); diagram's LLM cost is
// folded into the flat render fee. ONLY worker env key needed: RECRAFT_API_KEY.
import { writeFile } from "node:fs/promises";
import { join } from "node:path";

const RECRAFT = "https://external.api.recraft.ai/v1";

// Recraft sizes are a fixed set; map the aspect → the nearest supported size.
function sizeFor(aspect) {
  if (aspect === "9:16") return "1024x1365";
  if (aspect === "1:1") return "1024x1024";
  if (aspect === "4:5") return "1024x1280";
  return "1365x1024"; // 16:9
}

function recraftKey() {
  const k = process.env.RECRAFT_API_KEY || process.env.RECRAFT_API_TOKEN;
  if (!k) throw new Error("RECRAFT_API_KEY not set in the video-worker env");
  return k;
}

// Recraft generation → SVG (vector_illustration) or PNG bytes (digital_illustration).
// `seed` varies the composition so near-identical per-scene prompts don't collapse to
// the same stock illustration; pass a per-scene value to keep each scene distinct.
async function recraftGenerate(prompt, { vector, substyle, size, seed } = {}) {
  const body = {
    prompt, model: "recraftv3",
    style: vector ? "vector_illustration" : "digital_illustration",
    ...(substyle ? { substyle } : {}),
    ...(Number.isFinite(seed) ? { random_seed: seed } : {}),
    size: size || "1365x1024", n: 1, response_format: "url",
  };
  const r = await fetch(`${RECRAFT}/images/generations`, {
    method: "POST",
    headers: { Authorization: `Bearer ${recraftKey()}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`recraft gen ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const url = (await r.json())?.data?.[0]?.url;
  if (!url) throw new Error("recraft gen: no url in response");
  const a = await fetch(url);
  return vector ? { text: await a.text() } : { buffer: Buffer.from(await a.arrayBuffer()) };
}

// Recraft raster → SVG mask (the reveal map for raster-reveal).
async function recraftVectorize(pngBuffer) {
  const fd = new FormData();
  fd.append("file", new Blob([pngBuffer]), "image.png");
  fd.append("response_format", "url");
  // Cap the mask at 350 shapes (was 800): the raster-reveal renders every shape as a
  // per-frame mask path, so 800×4 scenes overwhelmed Chromium → the "Merangkai" hang.
  // 350 keeps the reveal smooth while staying well inside the render budget.
  fd.append("limit_num_shapes", "on");
  fd.append("max_num_shapes", "350");
  const r = await fetch(`${RECRAFT}/images/vectorize`, {
    method: "POST", headers: { Authorization: `Bearer ${recraftKey()}` }, body: fd,
  });
  if (!r.ok) throw new Error(`recraft vectorize ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const url = (await r.json())?.image?.url;
  if (!url) throw new Error("recraft vectorize: no url in response");
  return await (await fetch(url)).text();
}

// ── diagram: LLM graph → deterministic flowchart SVG (ported from scripts/diagram.mjs) ──
const BLUE = "#2C6CA8", INK = "#1A1A1A", RED = "#D9534F";
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

function layered(nodes, edges) {
  const adj = {}, indeg = {};
  nodes.forEach((n) => { adj[n.id] = []; indeg[n.id] = 0; });
  edges.forEach((e) => { if (adj[e.from] && indeg[e.to] != null) { adj[e.from].push(e.to); indeg[e.to]++; } });
  const lvl = {}, q = [], din = { ...indeg };
  nodes.forEach((n) => { if (din[n.id] === 0) { q.push(n.id); lvl[n.id] = 0; } });
  for (let i = 0; i < q.length; i++) {
    for (const v of adj[q[i]]) { lvl[v] = Math.max(lvl[v] ?? 0, (lvl[q[i]] ?? 0) + 1); if (--din[v] === 0) q.push(v); }
  }
  nodes.forEach((n) => { if (lvl[n.id] == null) lvl[n.id] = 0; });
  return lvl;
}

function buildDiagramSvg(g) {
  const dir = g.direction === "right" ? "right" : "down";
  const lvl = layered(g.nodes, g.edges || []);
  const maxLvl = Math.max(0, ...g.nodes.map((n) => lvl[n.id]));
  const byLvl = [];
  for (let l = 0; l <= maxLvl; l++) byLvl[l] = g.nodes.filter((n) => lvl[n.id] === l);
  const H = 78, GAPX = 56, GAPY = 70, PADX = 60, TOP = 110;
  const wOf = (n) => Math.min(300, Math.max(150, n.label.length * 20 + 36));
  const pos = {};
  let W, Hgt;
  if (dir === "down") {
    W = 900;
    byLvl.forEach((row, l) => {
      const widths = row.map(wOf);
      const totalW = widths.reduce((a, b) => a + b, 0) + GAPX * (row.length - 1);
      let x = (W - totalW) / 2; const y = TOP + l * (H + GAPY);
      row.forEach((n, i) => { pos[n.id] = { x, y, w: widths[i], h: H }; x += widths[i] + GAPX; });
    });
    Hgt = TOP + (maxLvl + 1) * (H + GAPY);
  } else {
    const COLW = 230;
    const colH = byLvl.map((c) => c.length * H + (c.length - 1) * GAPY);
    const maxColH = Math.max(...colH);
    byLvl.forEach((col, l) => {
      const x = PADX + l * (COLW + 70); let y = TOP + (maxColH - colH[l]) / 2;
      col.forEach((n) => { pos[n.id] = { x, y, w: COLW, h: H }; y += H + GAPY; });
    });
    W = PADX * 2 + (maxLvl + 1) * COLW + maxLvl * 70; Hgt = TOP + maxColH + 40;
  }
  let s = `<svg viewBox="0 0 ${W} ${Hgt}" xmlns="http://www.w3.org/2000/svg" fill="none">\n`;
  if (g.title) s += `  <text x="${W / 2}" y="64" font-family="Caveat, sans-serif" font-size="52" fill="${BLUE}" text-anchor="middle">${esc(g.title)}</text>\n`;
  // Draw NODES (box + label) before EDGES so the hand sketches each box first and
  // only then connects them — an arrow never lands in empty space. (Edges paint on
  // top of the box borders, which is the natural flowchart look.)
  for (const n of g.nodes) {
    const p = pos[n.id];
    s += `  <rect x="${p.x}" y="${p.y}" width="${p.w}" height="${p.h}" rx="6" stroke="${BLUE}" stroke-width="4"/>\n`;
    s += `  <text x="${p.x + p.w / 2}" y="${p.y + p.h / 2 + 13}" font-family="Caveat, sans-serif" font-size="38" fill="${INK}" text-anchor="middle">${esc(n.label)}</text>\n`;
  }
  for (const e of g.edges || []) {
    const a = pos[e.from], b = pos[e.to]; if (!a || !b) continue;
    const col = e.emphasis ? RED : INK, wdt = e.emphasis ? 6 : 4;
    if (dir === "down") {
      const x1 = a.x + a.w / 2, y1 = a.y + a.h, x2 = b.x + b.w / 2, y2 = b.y, my = (y1 + y2) / 2;
      s += `  <path d="M${x1} ${y1} C${x1} ${my} ${x2} ${my} ${x2} ${y2 - 12}" stroke="${col}" stroke-width="${wdt}"/>\n`;
      s += `  <polygon points="${x2},${y2} ${x2 - 9},${y2 - 14} ${x2 + 9},${y2 - 14}" fill="${col}"/>\n`;
    } else {
      const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2, mx = (x1 + x2) / 2;
      s += `  <path d="M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2 - 12} ${y2}" stroke="${col}" stroke-width="${wdt}"/>\n`;
      s += `  <polygon points="${x2},${y2} ${x2 - 14},${y2 - 9} ${x2 - 14},${y2 + 9}" fill="${col}"/>\n`;
    }
  }
  return s + "</svg>\n";
}

const _exampleGraph = () => ({
  title: "Proses", direction: "right",
  nodes: [{ id: "a", label: "Mulai" }, { id: "b", label: "Proses" }, { id: "c", label: "Hasil" }],
  edges: [{ from: "a", to: "b" }, { from: "b", to: "c", emphasis: true }],
});

// The diagram GRAPH is fetched from Python (/video/diagram → the SAME LLM routing/
// failover + Model Narasi as narration; NO new LLM key in the worker). The caller passes
// it in as opts.diagramGraph(description); this module only turns the graph into a clean
// SVG via buildDiagramSvg, falling back to _exampleGraph if the LLM is unavailable.

/**
 * Generate the per-scene whiteboard asset for a genre. Writes file(s) to tmpDir.
 * @returns { visualPath?, maskPath?, kind, meters:[{operation,model,units}] }
 *   meters = what the caller should charge via /video/meter (empty for lineart/diagram).
 */
export async function generateWhiteboardAsset(genre, { prompt, tmpDir, sceneIndex, aspect, diagramGraph }) {
  if (genre === "color") {
    // Drive a DISTINCT illustration per scene: keep the full per-scene visualPrompt as the
    // subject, frame it as a single standalone vector illustration, and vary the seed by
    // sceneIndex so near-identical prompts don't collapse to one stock vivid_shapes layout.
    const subject = String(prompt || "").trim();
    const scenePrompt =
      `${subject}. A distinct standalone illustration focused entirely on this specific subject, ` +
      `unique composition, flat vector style on a plain white background.`;
    const seed = 1000 + (Number.isFinite(sceneIndex) ? sceneIndex : 0) * 7919;
    const { text } = await recraftGenerate(scenePrompt, { vector: true, substyle: "vivid_shapes", size: sizeFor(aspect), seed });
    const visualPath = join(tmpDir, `wb_${sceneIndex}.svg`);
    await writeFile(visualPath, text);
    return { visualPath, kind: "whiteboard-color", meters: [{ operation: "image", model: "recraft-v3-vector", units: { count: 1 } }] };
  }
  if (genre === "detail") {
    const { buffer } = await recraftGenerate(prompt, { vector: false, size: sizeFor(aspect) });
    const visualPath = join(tmpDir, `wb_${sceneIndex}.png`);
    await writeFile(visualPath, buffer);
    const maskText = await recraftVectorize(buffer);
    const maskPath = join(tmpDir, `wb_${sceneIndex}-mask.svg`);
    await writeFile(maskPath, maskText);
    return { visualPath, maskPath, kind: "whiteboard-detail",
      meters: [{ operation: "image", model: "recraft-v3", units: { count: 1 } },
               { operation: "image", model: "recraft-vectorize", units: { count: 1 } }] };
  }
  if (genre === "diagram") {
    let graph = null;
    try { graph = await diagramGraph?.(prompt); } catch { /* fall back to the example graph */ }
    const visualPath = join(tmpDir, `wb_${sceneIndex}.svg`);
    await writeFile(visualPath, buildDiagramSvg(graph?.nodes?.length ? graph : _exampleGraph()));
    return { visualPath, kind: "whiteboard-diagram", meters: [] }; // LLM cost folded into render fee
  }
  return { kind: "whiteboard-lineart", meters: [] }; // lineart: handwriting only, no asset
}
