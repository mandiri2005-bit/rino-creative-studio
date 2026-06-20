// Staging test: does the Visual Director translate ANY-language narration → ENGLISH asset_query
// that resolves to a free icon? Hits the live /video/whiteboard-plan, then resolves each asset_query
// locally. No full render — isolates the VD + resolver.
//
// RUN (point at staging):
//   PYTHON_API_URL=https://<py-service> INTERNAL_SERVICE_SECRET=xxx \
//     node backend/video/whiteboard/test-vd-multilang.mjs
import { resolveIcon } from "./plan/iconlibs.mjs";

const PY = process.env.PYTHON_API_URL || "http://127.0.0.1:8000";
const SECRET = process.env.INTERNAL_SERVICE_SECRET || "";

// short scenes in several languages — each clearly names a concrete object
const TESTS = [
  ["ES", "Un viejo tren atraviesa la niebla de la montaña hacia un río brillante."],
  ["FR", "Une voiture rouge passe devant une maison et un grand arbre en fleurs."],
  ["DE", "Ein Zug fährt über eine Brücke, vorbei an einem Baum und einem Boot."],
  ["AR", "قطار قديم يمر فوق جسر بجانب شجرة ونهر."],
  ["ZH", "一列火车驶过桥梁，旁边有一棵树和一条河。"],
  ["ID", "Kereta tua melintasi jembatan menuju sungai dan sawah yang hijau."],
];

const isAscii = (s) => /^[\x00-\x7f]*$/.test(String(s || ""));

async function plan(narration) {
  const r = await fetch(`${PY}/video/whiteboard-plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Internal-Secret": SECRET, "X-Internal-Tenant-Id": "test", "X-Internal-User-Id": "test" },
    body: JSON.stringify({ narration, duration: 6, genre: "lineart", model: "gemini-2.5-flash", language: "", scene_id: "t" }),
  });
  if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 120)}`);
  return (await r.json()).plan;
}

let total = 0, eng = 0, resolved = 0;
for (const [lang, narration] of TESTS) {
  console.log(`\n[${lang}] ${narration}`);
  try {
    const p = await plan(narration);
    if (!p) { console.log("  ✗ VD returned null (plan-gen failed)"); continue; }
    for (const el of p.elements || []) {
      const q = el.asset_query || "";
      const en = isAscii(q);
      const r = resolveIcon(q);
      total++; if (en) eng++; if (r) resolved++;
      console.log(`  "${q}"  ${en ? "EN✓" : "NON-EN✗"}  → ${r ? `${r.lib}:${r.name || ""}` : "MISS→Recraft"}   (label: ${el.label || ""})`);
    }
  } catch (e) { console.log(`  ✗ ${e.message}`); }
}
console.log(`\n== ${eng}/${total} asset_query in English | ${resolved}/${total} resolved to a free icon ==`);
console.log(eng === total ? "✓ VD translates every language → English." : "⚠ some asset_query NOT English — strengthen the VD prompt for those languages.");
