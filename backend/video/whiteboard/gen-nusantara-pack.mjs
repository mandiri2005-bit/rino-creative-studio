// Nusantara curated icon pack — generate-ONCE the core Indonesian culture/history concepts that
// global icon libraries miss (the moat), as clean vector line icons via Recraft, then save them
// into the curated manifest so they're FREE + consistent forever (no per-job Recraft).
//
// PROMPTS ARE DESCRIPTIVE ON PURPOSE: Recraft often doesn't know the Indonesian NAME, so each
// prompt SPELLS OUT the visual so the icon is recognizable. Tags carry both Indonesian + English
// synonyms so the (English) asset_query from the plan LLM matches.
//
// RUN (needs a Recraft key — not available locally):
//   RECRAFT_API_KEY=xxx node backend/video/whiteboard/gen-nusantara-pack.mjs            # all
//   RECRAFT_API_KEY=xxx node backend/video/whiteboard/gen-nusantara-pack.mjs wayang keris  # only these (re-do bad ones)
// Then review out SVGs + `git add` the new icons + manifest.json.  Cost ≈ count × $0.08 (recraft-v3-vector).
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ICON_DIR = join(HERE, "assets", "whiteboard", "icons", "nusantara");
const MANIFEST = join(HERE, "assets", "whiteboard", "manifest.json");
const RECRAFT = "https://external.api.recraft.ai/v1";
const KEY = process.env.RECRAFT_API_KEY || process.env.RECRAFT_API_TOKEN;

const STYLE = "Flat single-colour black ink line icon, bold clean uniform strokes, minimal, centered, plain white background, no text, no words, no shading.";

// id | tags (ID + EN synonyms the plan LLM might emit) | STRONG visual prompt
const CONCEPTS = [
  ["wayang_kulit", ["wayang", "wayang kulit", "shadow puppet", "puppet", "shadow play", "indonesian puppet"],
    "A traditional Indonesian wayang kulit shadow puppet: a tall slender mythological figure shown in side profile, with an elaborate tall pointed headdress (crown), a long thin articulated arm holding a stick, and intricate openwork carved silhouette body."],
  ["wayang_golek", ["wayang golek", "rod puppet", "wooden puppet", "sundanese puppet"],
    "A traditional Sundanese wayang golek wooden rod puppet: a small carved three-dimensional doll, front-facing, with a round painted face, an ornate pointed headdress, and a patterned batik robe, standing on a wooden rod."],
  ["keris", ["keris", "kris", "dagger", "wavy dagger", "indonesian dagger", "blade"],
    "A keris, traditional Indonesian ceremonial dagger: an asymmetric blade with a distinctive wavy serpentine luk shape, joined to a carved curved ornate hilt, shown upright in side view."],
  ["batik", ["batik", "pattern", "textile pattern", "indonesian fabric", "motif"],
    "A batik motif: an intricate decorative repeating Indonesian textile pattern of swirling parang diagonal stripes and floral kawung circles, ornamental seamless tile."],
  ["gamelan", ["gamelan", "metallophone", "indonesian instrument", "bronze instrument", "music"],
    "A gamelan instrument: a traditional Indonesian bronze metallophone, a horizontal row of tuned metal bars resting on an ornate carved wooden frame, with a round-headed mallet beside it, front view."],
  ["angklung", ["angklung", "bamboo instrument", "bamboo shaker", "indonesian instrument"],
    "An angklung: a traditional Indonesian musical instrument made of vertical bamboo tubes of graduated length hanging in a horizontal bamboo frame, front view."],
  ["garuda", ["garuda", "eagle emblem", "mythical bird", "indonesian eagle", "national emblem"],
    "A Garuda: a heraldic mythical eagle emblem with a fierce head crest, symmetric outstretched feathered wings spread wide, fanned tail, front-facing crest emblem."],
  ["borobudur", ["borobudur", "candi", "buddhist temple", "stupa", "indonesian temple", "monument"],
    "Borobudur temple: a massive stepped pyramidal Buddhist monument, square terraced base topped by circular terraces of small bell-shaped perforated stupas and one large central stupa, front silhouette."],
  ["prambanan", ["prambanan", "candi", "hindu temple", "temple spire", "indonesian temple"],
    "Prambanan temple: a tall slender pointed Hindu temple tower (candi) with steeply stepped tapering tiers narrowing to a sharp finial, ornate carved body, front silhouette."],
  ["kapal_pinisi", ["pinisi", "kapal pinisi", "sailing ship", "schooner", "indonesian boat", "wooden ship"],
    "A pinisi: a traditional Indonesian wooden sailing schooner with a curved hull, two tall masts and several large triangular and gaff sails billowing, side view."],
  ["becak", ["becak", "rickshaw", "cycle rickshaw", "pedicab", "trishaw"],
    "A becak: a traditional Indonesian three-wheeled cycle rickshaw, a covered two-passenger seat cabin mounted at the FRONT with a bicycle and pedaling seat behind it, side view."],
  ["rumah_gadang", ["rumah gadang", "minangkabau house", "traditional house", "rumah adat"],
    "A Rumah Gadang: a traditional Minangkabau house with a dramatic roof of several sharply upward-curving pointed peaks resembling buffalo horns, raised on stilts, front view."],
  ["songket", ["songket", "brocade", "woven textile", "gold cloth", "indonesian fabric"],
    "A songket cloth: a folded length of traditional Indonesian hand-woven brocade textile decorated with rows of geometric diamond and floral gold-thread motifs."],
  ["reog", ["reog", "reog ponorogo", "lion mask", "peacock headdress", "indonesian mask"],
    "A Reog Ponorogo: a giant ceremonial Indonesian mask-headdress, a fierce tiger-lion face at the bottom crowned by an enormous towering fan-shaped spray of peacock feathers, front view."],
  ["barong", ["barong", "barong mask", "balinese lion", "mythical creature mask"],
    "A Barong: a Balinese mythological lion-like guardian mask with a fierce face, large round bulging eyes, a fanged open-mouth grin, and an ornate gilded curly mane, front view."],
  ["kujang", ["kujang", "sundanese blade", "ceremonial weapon", "curved blade"],
    "A kujang: a traditional Sundanese ceremonial blade with a short distinctive curved hooked asymmetric shape and small round holes along the spine, side view."],
  ["gunungan", ["gunungan", "kayon", "tree of life", "wayang symbol", "mountain symbol"],
    "A gunungan (kayon): a tall pointed leaf-shaped wayang ornament with a rounded base and sharp top, filled with intricate symmetrical carving of a gateway, a tree of life and mythical creatures."],
  ["sawah_terasering", ["sawah", "terraced rice", "rice terraces", "rice paddy", "terasering", "rice field"],
    "Terraced rice paddies: stepped curved agricultural terraces descending a hillside, each level a flooded rice field with rows of rice plants, landscape scene."],
  ["monas", ["monas", "national monument", "jakarta monument", "obelisk", "indonesian landmark"],
    "Monas, the Indonesian National Monument: a tall slender square obelisk tower on a wide base, topped with a pointed flame sculpture, front silhouette."],
  ["angkot", ["angkot", "minibus", "public minivan", "indonesian transport"],
    "An angkot: a small boxy Indonesian public-transport minivan with side windows and a sliding side door, side view."],
];

async function recraftVector(prompt, seed) {
  const r = await fetch(`${RECRAFT}/images/generations`, {
    method: "POST",
    headers: { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: `${prompt} ${STYLE}`, model: "recraftv3", style: "vector_illustration", substyle: "line_art", size: "1024x1024", n: 1, response_format: "url", ...(seed ? { random_seed: seed } : {}) }),
  });
  if (!r.ok) throw new Error(`recraft ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const url = (await r.json())?.data?.[0]?.url;
  if (!url) throw new Error("no url in recraft response");
  return await (await fetch(url)).text();
}

async function main() {
  if (!KEY) { console.error("RECRAFT_API_KEY not set — get a key + re-run (this is a one-time staging/local job)."); process.exit(1); }
  mkdirSync(ICON_DIR, { recursive: true });
  const only = process.argv.slice(2);
  const todo = only.length ? CONCEPTS.filter((c) => only.includes(c[0]) || only.some((o) => c[1].includes(o))) : CONCEPTS;
  const manifest = existsSync(MANIFEST) ? JSON.parse(readFileSync(MANIFEST, "utf8")) : { version: 1, assets: [] };
  manifest.assets = manifest.assets || [];
  let ok = 0;
  for (const [id, tags, prompt] of todo) {
    try {
      const svg = await recraftVector(prompt, 4242);
      writeFileSync(join(ICON_DIR, `${id}.svg`), svg);
      const entry = { id, path: `icons/nusantara/${id}.svg`, tags, license: "recraft-v3-vector:provider-terms", source: "nusantara_pack" };
      const i = manifest.assets.findIndex((a) => a.id === id);
      if (i >= 0) manifest.assets[i] = entry; else manifest.assets.push(entry);
      ok++; console.log(`✓ ${id}  (${tags.slice(0, 3).join(", ")})`);
    } catch (e) { console.error(`✗ ${id}: ${e.message}`); }
  }
  writeFileSync(MANIFEST, JSON.stringify(manifest, null, 2));
  console.log(`\nDone: ${ok}/${todo.length} icons → ${ICON_DIR}\nReview the SVGs, regenerate any weak ones (pass their id), then commit icons/nusantara/ + manifest.json.`);
}
main();
