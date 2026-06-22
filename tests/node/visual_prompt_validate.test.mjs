// Tests for video/visualPrompt/validate.mjs
// Run: node --test tests/node/visual_prompt_validate.test.mjs
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const { validateVisualPromptOutput, extractProperNouns, buildVisualPromptFallback } =
  await import(join(ROOT, "backend", "video", "visualPrompt", "validate.mjs"));

// ── shared helpers ────────────────────────────────────────────────────────────

// A narration long enough that a 200-char visual_prompt passes the min-length check.
const SHORT_NARR = "Di tepi pantai Batavia, seorang nelayan bernama Ahmad menarik jaringnya.";

function makeOutput(overrides = {}) {
  return {
    visual_prompt: "A ".padEnd(50, "x").repeat(4).slice(0, 220),  // 220 chars — just over MIN_CHARS
    ...overrides,
  };
}

// ── extractProperNouns ────────────────────────────────────────────────────────

describe("extractProperNouns", () => {
  it("finds proper nouns from English text", () => {
    const nouns = extractProperNouns("On the banks of the Nile, Napoleon marched south.");
    assert.ok(nouns.has("nile"),     "Nile");
    assert.ok(nouns.has("napoleon"), "Napoleon");
    assert.ok(!nouns.has("the"),     "the is filtered");
    assert.ok(!nouns.has("on"),      "on is filtered");
  });

  it("finds proper nouns from Indonesian text", () => {
    const nouns = extractProperNouns("Sriwijaya berdiri di tepi Sungai Musi, Palembang.");
    assert.ok(nouns.has("sriwijaya"), "Sriwijaya");
    assert.ok(nouns.has("palembang"), "Palembang");
  });

  it("returns empty set for empty/null input", () => {
    assert.equal(extractProperNouns("").size, 0);
    assert.equal(extractProperNouns(null).size, 0);
    assert.equal(extractProperNouns(undefined).size, 0);
  });

  it("ignores honorifics (dr, van, de)", () => {
    const nouns = extractProperNouns("Dr. Johann van Riebeeck landed at the Cape.");
    assert.ok(!nouns.has("dr"),  "dr filtered");
    assert.ok(!nouns.has("van"), "van filtered");
    assert.ok(nouns.has("riebeeck"), "Riebeeck kept");
    assert.ok(nouns.has("cape"),     "Cape kept");
  });
});

// ── validateVisualPromptOutput ────────────────────────────────────────────────

describe("validateVisualPromptOutput — shape guards", () => {
  const scene = { narration_text: SHORT_NARR };

  it("rejects null", () => {
    const r = validateVisualPromptOutput(null, scene);
    assert.equal(r.ok, false);
    assert.ok(r.errors[0].includes("not an object"));
  });

  it("rejects array", () => {
    const r = validateVisualPromptOutput([], scene);
    assert.equal(r.ok, false);
  });

  it("rejects missing visual_prompt", () => {
    const r = validateVisualPromptOutput({}, scene);
    assert.equal(r.ok, false);
    assert.ok(r.errors.some((e) => e.includes("missing or empty")));
  });

  it("rejects empty-string visual_prompt", () => {
    const r = validateVisualPromptOutput({ visual_prompt: "" }, scene);
    assert.equal(r.ok, false);
  });
});

describe("validateVisualPromptOutput — visual_prompt length", () => {
  const scene = { narration_text: SHORT_NARR };

  it("rejects prompt shorter than 200 chars", () => {
    const r = validateVisualPromptOutput({ visual_prompt: "Short." }, scene);
    assert.equal(r.ok, false);
    assert.ok(r.errors.some((e) => e.includes("too short")));
  });

  it("accepts prompt exactly 200 chars", () => {
    const vp = "A".repeat(200);
    const r = validateVisualPromptOutput({ visual_prompt: vp }, scene);
    assert.ok(!r.errors.some((e) => e.includes("too short")));
  });

  it("warns (not errors) when prompt is over 600 chars", () => {
    const vp = "A".repeat(601);
    const r = validateVisualPromptOutput({ visual_prompt: vp }, scene);
    assert.ok(!r.errors.some((e) => e.includes("too long")), "no fatal error for over-long");
    assert.ok(r.warnings.some((w) => w.includes("too long")), "warning emitted");
  });
});

describe("validateVisualPromptOutput — Chastelein check", () => {
  it("rejects proper noun in prompt that is absent from narration", () => {
    // 'Chastelein' appears only in the prompt, never in the narration
    const scene = {
      narration_text:
        "Seorang petani padi mengerjakan sawahnya di bawah terik matahari sore hari yang panjang.",
    };
    const vp =
      ("Chastelein stands in the paddy field surveying the vast landscape. " +
       "The golden light of the afternoon sun illuminates the scene. " +
       "Water buffalo wade through flooded fields. A farmer guides them.").padEnd(220, " x");
    const r = validateVisualPromptOutput({ visual_prompt: vp }, scene);
    assert.equal(r.ok, false, "should fail Chastelein check");
    assert.ok(r.errors.some((e) => e.includes("chastelein")), `errors: ${r.errors}`);
  });

  it("accepts proper noun that IS in the narration", () => {
    const scene = {
      narration_text:
        "Ahmad berlari melewati jalan sempit di pusat kota Batavia pada fajar, membawa pesan penting.",
    };
    // 'Ahmad' and 'Batavia' both appear in narration
    const vp = (
      "Ahmad runs through the narrow streets of Batavia at dawn carrying a message. " +
      "The colonial architecture looms on either side. Fog hangs in the air over the port. " +
      "Market vendors begin to open their stalls.").padEnd(220, " ");
    const r = validateVisualPromptOutput({ visual_prompt: vp }, scene);
    assert.ok(!r.errors.some((e) => e.toLowerCase().includes("ahmad")), `ahmad should be allowed: ${r.errors}`);
    assert.ok(!r.errors.some((e) => e.toLowerCase().includes("batavia")), `batavia should be allowed: ${r.errors}`);
  });

  it("setting nouns are added to the universe (not flagged)", () => {
    const scene = {
      narration_text: "The valley was lush and green in the rainy season, filled with birdsong.",
    };
    const vp = (
      "Lush Borneo valley viewed from above, dense tropical canopy stretching to the horizon. " +
      "Morning mist rises from the river below. A hornbill calls in the distance over the trees. " +
      "Golden light filters through the canopy.").padEnd(220, " ");
    const r = validateVisualPromptOutput(
      { visual_prompt: vp, setting: "Borneo rainforest" },
      scene,
    );
    // 'Borneo' is in setting → should not trigger Chastelein error
    assert.ok(!r.errors.some((e) => e.toLowerCase().includes("borneo")), `Borneo in setting OK: ${r.errors}`);
  });
});

describe("validateVisualPromptOutput — characters array", () => {
  const scene = {
    narration_text: "Sultan Agung memimpin pasukannya menyerang VOC di Batavia pada 1628.",
  };

  it("accepts characters that appear in narration_text", () => {
    const r = validateVisualPromptOutput(
      { visual_prompt: makeOutput().visual_prompt, characters: ["Sultan Agung"] },
      scene,
    );
    assert.ok(!r.errors.some((e) => e.includes("Sultan Agung")));
  });

  it("rejects character not in narration_text", () => {
    const r = validateVisualPromptOutput(
      { visual_prompt: makeOutput().visual_prompt, characters: ["Jan Pieterszoon Coen"] },
      scene,
    );
    assert.equal(r.ok, false);
    assert.ok(r.errors.some((e) => e.includes("Jan Pieterszoon Coen")));
  });

  it("warns (not errors) when characters is not an array", () => {
    const r = validateVisualPromptOutput(
      { visual_prompt: makeOutput().visual_prompt, characters: "Sultan Agung" },
      scene,
    );
    assert.ok(r.warnings.some((w) => w.includes("not an array")));
    // should not be a fatal error on its own
    assert.ok(!r.errors.some((e) => e.includes("not an array")));
  });

  it("skips non-string entries with a warning", () => {
    const r = validateVisualPromptOutput(
      { visual_prompt: makeOutput().visual_prompt, characters: [42, "Sultan Agung"] },
      scene,
    );
    assert.ok(r.warnings.some((w) => w.includes("non-string entry")));
  });
});

describe("validateVisualPromptOutput — optional fields + unknown keys", () => {
  const scene = { narration_text: SHORT_NARR };

  it("accepts valid setting and mood strings", () => {
    const r = validateVisualPromptOutput(
      { visual_prompt: makeOutput().visual_prompt, setting: "coastal village", mood: "melancholic" },
      scene,
    );
    assert.equal(r.ok, true);
    assert.equal(r.warnings.length, 0);
  });

  it("warns on non-string setting", () => {
    const r = validateVisualPromptOutput(
      { visual_prompt: makeOutput().visual_prompt, setting: 42 },
      scene,
    );
    assert.ok(r.warnings.some((w) => w.includes("setting is not a string")));
  });

  it("warns on unknown extra keys", () => {
    const r = validateVisualPromptOutput(
      { visual_prompt: makeOutput().visual_prompt, _internal: true },
      scene,
    );
    assert.ok(r.warnings.some((w) => w.includes("unknown fields")));
  });

  it("ok=true for a fully valid object", () => {
    const vp = "Wide establishing shot: Ahmad pulls his fishing net at dawn on a Batavia beach. " +
               "Warm amber light filters over the water. Fishing boats dot the horizon. " +
               "The smell of salt and seaweed hangs in the still morning air. Birds circle overhead.";
    const r = validateVisualPromptOutput(
      { visual_prompt: vp, characters: ["Ahmad"], setting: "Batavia beach", mood: "serene" },
      { narration_text: "Ahmad, seorang nelayan di Batavia, menarik jaringnya setiap pagi." },
    );
    assert.equal(r.ok, true, `expected ok=true, errors: ${r.errors}`);
  });
});

// ── buildVisualPromptFallback ─────────────────────────────────────────────────

describe("buildVisualPromptFallback", () => {
  it("returns scene.visualPrompt when LLM output is invalid", () => {
    const scene = { visualPrompt: "existing regex prompt from segmenter" };
    const fallback = buildVisualPromptFallback(scene, ["visual_prompt too short (5 chars, min 200)"], "s3");
    assert.equal(fallback, "existing regex prompt from segmenter");
  });

  it("returns empty string when scene has no fallback prompt", () => {
    const fallback = buildVisualPromptFallback({}, ["some error"]);
    assert.equal(fallback, "");
  });
});
