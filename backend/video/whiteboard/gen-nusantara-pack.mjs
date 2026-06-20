// Nusantara curated icon pack — CORPUS-DRIVEN. Sources concepts + EXPERT visual prompts straight
// from the Nusantara corpus (python/data/nusantara_seed.json) — each entry already has rich
// `visual_facts` (the strong prompt), `tags` (ID+EN match), `category`, `rights_class`. We generate
// clean vector line icons via Recraft for the ICON-ABLE categories (discrete objects, not "suasana"
// atmospheres) and save them into the curated manifest → free + consistent forever.
//
// The corpus already enriches the TEXT layer (narration/brief via _corpus_enhance). This pack adds
// the matching ASSET layer so those Indonesian concepts also have free icons (no per-job Recraft).
//
// RUN (needs a Recraft key — one-time):
//   RECRAFT_API_KEY=xxx node backend/video/whiteboard/gen-nusantara-pack.mjs                 # default cats
//   RECRAFT_API_KEY=xxx node backend/video/whiteboard/gen-nusantara-pack.mjs --cat=wayang,kuliner --limit=30
//   RECRAFT_API_KEY=xxx node backend/video/whiteboard/gen-nusantara-pack.mjs --list          # dry-run, no API
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SEED = join(HERE, "..", "..", "..", "python", "data", "nusantara_seed.json");
const ICON_DIR = join(HERE, "assets", "whiteboard", "icons", "nusantara");
const MANIFEST = join(HERE, "assets", "whiteboard", "manifest.json");
const RECRAFT = "https://external.api.recraft.ai/v1";
const KEY = process.env.RECRAFT_API_KEY || process.env.RECRAFT_API_TOKEN;

// icon-able categories = discrete objects (NOT "suasana"/scene atmospheres, which aren't single icons)
const DEFAULT_CATS = ["wayang", "busana-adat", "rumah-adat", "kendaraan", "tarian", "makhluk", "arsitektur", "kuliner"];
const STYLE = "Simple minimal flat single-colour black line icon, bold clean strokes, centered, plain white background, no text, no words, no shading.";

const args = process.argv.slice(2);
const list = args.includes("--list");
const catArg = (args.find((a) => a.startsWith("--cat=")) || "").slice(6);
const cats = catArg ? catArg.split(",") : DEFAULT_CATS;
const limit = Number((args.find((a) => a.startsWith("--limit=")) || "").slice(8)) || Infinity;
const ids = args.filter((a) => !a.startsWith("--"));

const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 50);

function concepts() {
  const seed = JSON.parse(readFileSync(SEED, "utf8"));
  const all = Array.isArray(seed) ? seed : (seed.entries || Object.values(seed)[0]);
  let rows = all.filter((e) => e.visual_facts && (ids.length ? ids.includes(e.id) : cats.includes(e.category)));
  // de-dupe by subject slug; cap
  const seen = new Set(); const out = [];
  // English keywords from visual_facts so the (English) plan-LLM asset_query matches the Indonesian
  // corpus tags (e.g. "puppet"/"temple"/"dagger" from the visual description).
  const STOPF = new Set(["with", "flat", "very", "large", "small", "round", "dark", "gold", "white", "black", "ornament",
    "background", "traditional", "style", "detail", "effect", "front", "side", "view", "indonesian", "javanese", "balinese",
    "colour", "color", "shape", "body", "face", "long", "tall", "short", "from", "into", "that", "this", "have", "single"]);
  for (const e of rows) {
    const id = slug(e.id || e.subject);
    if (seen.has(id)) continue; seen.add(id);
    const eng = [...new Set((String(e.visual_facts || "").slice(0, 150).toLowerCase().match(/[a-z]{4,}/g) || []).filter((t) => !STOPF.has(t)))].slice(0, 8);
    const tags = Array.from(new Set([...(Array.isArray(e.tags) ? e.tags : []), e.subject, e.category, ...eng]
      .filter(Boolean).map((t) => String(t).toLowerCase())));
    out.push({ id, subject: e.subject, category: e.category, tags, prompt: `${e.subject}: ${e.visual_facts}`, rights: e.rights_class });
    if (out.length >= limit) break;
  }
  return out;
}

async function recraftVector(prompt, seed) {
  const r = await fetch(`${RECRAFT}/images/generations`, {
    method: "POST",
    headers: { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: `${prompt}. ${STYLE}`, model: "recraftv3", style: "vector_illustration", substyle: "line_art", size: "1024x1024", n: 1, response_format: "url", ...(seed ? { random_seed: seed } : {}) }),
  });
  if (!r.ok) throw new Error(`recraft ${r.status}: ${(await r.text()).slice(0, 160)}`);
  const url = (await r.json())?.data?.[0]?.url;
  if (!url) throw new Error("no url");
  return await (await fetch(url)).text();
}

async function main() {
  const todo = concepts();
  console.log(`corpus-driven pack: ${todo.length} concepts (cats: ${ids.length ? "by id" : cats.join(",")})`);
  if (list) { todo.forEach((c) => console.log(`  ${c.id.padEnd(24)} [${c.category}] tags: ${c.tags.slice(0, 4).join(", ")}`)); console.log(`\n(dry-run) est cost ≈ $${(todo.length * 0.08).toFixed(2)}. Re-run without --list (+ RECRAFT_API_KEY) to generate.`); return; }
  if (!KEY) { console.error("RECRAFT_API_KEY not set. Use --list to preview, or set the key to generate."); process.exit(1); }
  mkdirSync(ICON_DIR, { recursive: true });
  const manifest = existsSync(MANIFEST) ? JSON.parse(readFileSync(MANIFEST, "utf8")) : { version: 1, assets: [] };
  manifest.assets = manifest.assets || [];
  let ok = 0;
  for (const c of todo) {
    try {
      const svg = await recraftVector(c.prompt, 4242);
      writeFileSync(join(ICON_DIR, `${c.id}.svg`), svg);
      const entry = { id: c.id, path: `icons/nusantara/${c.id}.svg`, tags: c.tags, license: "recraft-v3-vector:provider-terms", source: "nusantara_pack" };
      const i = manifest.assets.findIndex((a) => a.id === c.id);
      if (i >= 0) manifest.assets[i] = entry; else manifest.assets.push(entry);
      ok++; console.log(`✓ ${c.id} [${c.category}]`);
    } catch (e) { console.error(`✗ ${c.id}: ${e.message}`); }
  }
  writeFileSync(MANIFEST, JSON.stringify(manifest, null, 2));
  console.log(`\nDone: ${ok}/${todo.length} → ${ICON_DIR}\nReview SVGs, regenerate weak ones (pass ids), then commit icons/nusantara/ + manifest.json.`);
}
main();
